"""Job lifecycle: prepare (read form + map) -> resolve (needs info?) -> apply (fill, audit, submit or stop).

Plans live in data/plans/<job>.json so we never re-read the form or re-ask Claude unless the form changes."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path


from . import config, form, knowledge as kn, llm
from . import notion_store as ns
from .log import log

CLOSED_RE = re.compile(r"no longer (accepting|available|open)|job (not found|has been filled|is closed)|position has been filled|page (not found|you.re looking for)", re.I)


def _plan_path(job_key: str) -> Path:
    return config.PLANS / (re.sub(r"[^a-zA-Z0-9_-]", "_", job_key) + ".json")


def load_plan(job_key: str) -> dict | None:
    p = _plan_path(job_key)
    return json.loads(p.read_text()) if p.exists() else None


def save_plan(plan: dict) -> None:
    _plan_path(plan["job"]["job_key"]).write_text(json.dumps(plan, indent=1))


def job_info(page: dict) -> dict:
    return {"page_id": page["id"], "job_key": ns.read(page, "Job Key"), "company": ns.read(page, "Company"),
            "title": ns.read(page, "Title"), "url": ns.read(page, "URL"), "ats": ns.read(page, "ATS")}


def set_status(job_page_id: str, status: str, reason: str = "", **extra) -> None:
    props = {"Status": ns.select(status), "Reason": ns.text(reason[:1900])}
    props.update(extra)
    ns.update(job_page_id, props)


# ---------------- Questions rows ----------------

def _question_row(k: kn.Knowledge, pattern: str, scope: str, company: str, job: dict, field: dict) -> dict | None:
    opts = "\n".join(field["options"])
    for q in k.questions:
        if ns.read(q, "Pattern") != pattern or ns.read(q, "Scope") != scope or (ns.read(q, "Options") or "") != opts:
            continue
        if scope == "company" and kn.norm(ns.read(q, "Company")) != kn.norm(company):
            continue
        if scope == "job" and job["page_id"] not in ns.read(q, "Jobs"):
            continue
        return q
    return None


def ensure_question(k: kn.Knowledge, job: dict, field: dict, d: dict, status: str, suggested: str, basis: str) -> None:
    q = _question_row(k, d["pattern"], d["scope"], job["company"], job, field)
    if q:
        jobs = ns.read(q, "Jobs")
        if job["page_id"] not in jobs:
            ns.update(q["id"], {"Jobs": ns.relation(jobs + [job["page_id"]])})
        return
    ftype = {"combobox": "select", "autocomplete": "text", "radio": "select", "yesno": "select",
             "multi_checkbox": "multi_select"}.get(field["kind"], field["kind"])
    q = ns.create(config.QUESTIONS_DS, {
        "Question": ns.title(field["label"]),
        "Pattern": ns.text(d["pattern"]),
        "Field Type": ns.select(ftype if ftype in ("text", "textarea", "select", "multi_select", "checkbox", "date", "file") else "text"),
        "Options": ns.text("\n".join(field["options"])),
        "Scope": ns.select(d["scope"]),
        "Company": ns.text(job["company"] if d["scope"] != "universal" else ""),
        "Suggested": ns.text(suggested),
        "Basis": ns.text(basis),
        "Status": ns.select(status),
        "Approved": ns.checkbox(False),
        "Jobs": ns.relation([job["page_id"]]),
    })
    k.questions.append(q)


# ---------------- prepare ----------------

async def prepare(job_page: dict, k: kn.Knowledge, browser) -> None:
    job = job_info(job_page)
    page = await browser.new_page()
    try:
        resp = await page.goto(job["url"], wait_until="networkidle", timeout=60000)
        text = await form.page_text(page)
        if (resp and resp.status >= 400) or CLOSED_RE.search(text[:3000]):
            set_status(job["page_id"], "filtered_out", "Posting closed")
            return
        if job["ats"] == "lever" and "/apply" not in page.url:
            await page.goto(job["url"].rstrip("/") + "/apply", wait_until="networkidle")
        fields = await form.extract_fields(page)
    finally:
        await page.close()
    if not fields:
        set_status(job["page_id"], "manual_apply", "No application form found on the page")
        return

    approved_patterns = {}
    for q in k.questions:
        approved_patterns.setdefault(ns.read(q, "Pattern"), {"pattern": ns.read(q, "Pattern"), "example": ns.read(q, "Question"), "scope": ns.read(q, "Scope")})
    decisions = llm.map_fields({"company": job["company"], "title": job["title"]}, fields, k.facts, k.fact_desc,
                               list(approved_patterns.values()))
    dmap = {d["id"]: d for d in decisions}
    plan = {"job": job, "page_text": text, "fields": fields, "decisions": dmap,
            "prepared_at": datetime.now(timezone.utc).isoformat()}
    save_plan(plan)
    log("prepared", job=job["job_key"], fields=len(fields))

    for f in fields:
        d = dmap.get(f["id"])
        if not d:
            continue
        if d["action"] == "essay":
            d["pattern"] = d["pattern"] or "essay_" + kn.norm(f["label"])[:40].replace(" ", "_")
            if kn.find_answer(k, d["pattern"], d["scope"], job["company"], job["page_id"], f):
                continue
            if not _question_row(k, d["pattern"], d["scope"], job["company"], job, f):
                e = llm.draft_essay(f["label"], {"company": job["company"], "title": job["title"]}, text, k.facts, k.stories)
                basis = e["basis"] + (f"\nGaps: {e['gaps']}" if e["gaps"] else "")
                ensure_question(k, job, f, d, "essay_draft", e["text"], basis)
            else:
                ensure_question(k, job, f, d, "essay_draft", "", "")
        elif d["action"] == "question" and not d["pattern"].startswith("eeo_"):
            if not kn.find_answer(k, d["pattern"], d["scope"], job["company"], job["page_id"], f):
                ensure_question(k, job, f, d, "suggested" if d["suggested"] else "needs_answer", d["suggested"], d["basis"])
        elif d["action"] == "question":
            if not kn.eeo_lookup(d["pattern"], f):
                kn.eeo_queue(d["pattern"], f, d["suggested"], d["basis"])
    save_plan(plan)
    resolve_and_update(job_page, plan, k)


# ---------------- resolve ----------------

def resolve(plan: dict, k: kn.Knowledge) -> tuple[dict, list[str], list[str]]:
    """-> (values {field_id: {value, source}}, blockers needing info, hard failures needing manual apply)."""
    job = plan["job"]
    values, blockers, hard = {}, [], []
    for f in plan["fields"]:
        d = plan["decisions"].get(f["id"])
        req = f["required"]
        if not d:
            if req:
                blockers.append(f"Unmapped field: {f['label']}")
            continue
        a = d["action"]
        if a == "skip" or (a == "unsupported" and not req):
            if req:
                blockers.append(f"Required field was marked skip: {f['label']}")
            continue
        if a == "unsupported":
            hard.append(f"Can't fill: {f['label']}")
            continue
        if a == "template":
            try:
                v = kn.render(d["template"], k.facts)
            except kn.MissingFact as e:
                if req:
                    blockers.append(f"Fill fact '{e.args[0]}' in Facts")
                continue
            except ValueError as e:
                blockers.append(f"Bad template for '{f['label']}': {e}")
                continue
            if f["kind"] == "file":
                if not Path(v).expanduser().exists():
                    blockers.append(f"File not found: {v}")
                    continue
                v = str(Path(v).expanduser())
            elif f["options"]:
                opt = kn.choose_option(v, f["options"])
                if not opt:
                    if req:
                        blockers.append(f"'{v}' isn't an option for '{f['label']}'")
                    continue
                v = opt
            values[f["id"]] = {"value": v, "source": f"facts:{d['template']}"}
            continue
        # question / essay
        if d["pattern"].startswith("eeo_"):
            v = kn.eeo_lookup(d["pattern"], f)
            if v:
                values[f["id"]] = {"value": v, "source": "local EEO approval"}
            else:
                blockers.append(f"EEO answer needs local approval ({f['label']}): run `jobbot eeo`")
            continue
        hit = kn.find_answer(k, d["pattern"], d["scope"], job["company"], job["page_id"], f)
        if hit:
            values[f["id"]] = {"value": hit[0], "source": f"question:{hit[1]}"}
        elif req or a == "essay" or d.get("suggested"):
            blockers.append(f"Waiting on answer: {f['label']}")
    return values, blockers, hard


def resolve_and_update(job_page: dict, plan: dict, k: kn.Knowledge) -> str:
    values, blockers, hard = resolve(plan, k)
    plan["values"] = values
    save_plan(plan)
    if hard:
        status, reason = "manual_apply", "; ".join(hard)
    elif blockers:
        status, reason = "pending_question", "\n".join(blockers[:12])
    else:
        status, reason = "ready", f"{len(values)} fields resolved"
    set_status(job_page["id"], status, reason)
    return status


# ---------------- sync (Notion -> pipeline) ----------------

def sync_questions(k: kn.Knowledge) -> int:
    """Validate newly approved Questions rows. Returns how many changed."""
    n = 0
    for q in k.questions:
        if not ns.read(q, "Approved") or ns.read(q, "Status") == "approved":
            continue
        val = kn.answer_value(q)
        opts = [o for o in (ns.read(q, "Options") or "").split("\n") if o]
        err = ""
        if not val:
            err = "Approved but Answer and Suggested are both empty"
        elif opts and ns.read(q, "Field Type") == "multi_select":
            bad = [x for x in val.split("\n") if not kn.choose_option(x, opts)]
            err = f"Not an option: {bad}" if bad else ""
        elif opts and not kn.choose_option(val, opts):
            err = f"'{val}' doesn't match any option exactly. Copy one of the options into Answer."
        props = {"Status": ns.select("invalid" if err else "approved"), "Note": ns.text(err)}
        if err:
            props["Approved"] = ns.checkbox(False)
        q["properties"]["Status"] = {"type": "select", "select": {"name": "invalid" if err else "approved"}}
        if err:
            q["properties"]["Approved"] = {"type": "checkbox", "checkbox": False}
        ns.update(q["id"], props)
        n += 1
    return n


def sync(k: kn.Knowledge | None = None) -> dict:
    k = k or kn.load()
    changed = sync_questions(k)
    unblocked = 0
    for p in ns.query(config.JOBS_DS, {"property": "Status", "select": {"equals": "pending_question"}}):
        plan = load_plan(ns.read(p, "Job Key"))
        if plan and resolve_and_update(p, plan, k) == "ready":
            unblocked += 1
    log("sync", questions_changed=changed, unblocked=unblocked)
    return {"questions_changed": changed, "unblocked": unblocked}


# ---------------- apply ----------------

def submitted_today() -> int:
    today = date.today().isoformat()
    jobs = ns.query(config.JOBS_DS, {"property": "Submitted At", "date": {"equals": today}})
    return len(jobs)


async def _fill(page, plan: dict) -> list[str]:
    errs = []
    fields = {f["id"]: f for f in plan["fields"]}
    order = sorted(list(plan["values"]), key=lambda i: fields[i]["kind"] != "file")  # files first: some ATSs autofill from the resume
    plan["chosen"] = {}
    for fid in order:
        f = fields[fid]
        try:
            plan["chosen"][fid] = await form.fill_field(page, f, plan["values"][fid]["value"])
        except Exception as e:
            if f["required"]:
                errs.append(f"fill '{f['label']}': {str(e).splitlines()[0][:150]}")
            else:  # optional field we couldn't operate: leave it blank rather than half-filled
                plan["values"].pop(fid)
                try:
                    if f["kind"] in ("text", "textarea", "autocomplete", "combobox"):
                        await page.locator(f["selector"]).first.fill("")
                except Exception:
                    pass
    return errs


async def _audit(page, plan: dict) -> list[str]:
    problems = []
    fields = {f["id"]: f for f in plan["fields"]}
    readback = {}
    for fid, v in plan["values"].items():
        actual = await form.read_back(page, fields[fid])
        readback[fid] = actual
        if not form.matches(fields[fid], v["value"], actual, plan.get("chosen", {}).get(fid)):
            problems.append(f"'{fields[fid]['label']}' reads back {actual[:60]!r}, planned {v['value'][:60]!r}")
    for f in plan["fields"]:
        if f["required"] and f["id"] not in plan["values"]:
            problems.append(f"Required field not filled: {f['label']}")
    now = {f["label"] for f in await form.extract_fields(page) if f["required"]}
    new = now - {f["label"] for f in plan["fields"]}
    if new:
        problems.append(f"New required fields appeared: {sorted(new)[:5]}")
    errors = await form.visible_errors(page)
    if errors:
        problems.append(f"Page shows errors: {errors[:5]}")
    plan["readback"] = readback
    if problems:
        return problems
    rows = [{"id": fid, "question": fields[fid]["label"], "options": fields[fid]["options"][:30],
             "value": v["value"] if fields[fid]["kind"] != "file" else f"<file {Path(v['value']).name}>", "source": v["source"]}
            for fid, v in plan["values"].items()]
    for r in llm.audit({"company": plan["job"]["company"], "title": plan["job"]["title"]}, rows):
        if not r["ok"]:
            problems.append(f"Auditor: '{fields.get(r['id'], {'label': r['id']})['label']}': {r['issue']}")
    return problems


async def apply(job_page: dict, ctx, keep_open: bool = False) -> str:
    job = job_info(job_page)
    plan = load_plan(job["job_key"])
    if not plan or "values" not in plan:
        return "skipped"
    page = await ctx.new_page()
    await page.goto(job["url"], wait_until="networkidle", timeout=60000)
    errs = await _fill(page, plan)
    problems = errs + (await _audit(page, plan) if not errs else [])
    shot = config.SCREENS / f"{_plan_path(job['job_key']).stem}.png"
    await page.screenshot(path=str(shot), full_page=True)
    log("filled", job=job["job_key"], problems=problems, values={k: v["value"] for k, v in plan["values"].items()})
    save_plan(plan)
    if problems:
        set_status(job["page_id"], "audit_failed", "\n".join(problems[:10]))
        status = "audit_failed"
    elif not config.AUTO_SUBMIT or submitted_today() >= config.MAX_SUBMITS_PER_DAY:
        set_status(job["page_id"], "dry_run_ok", "Filled and audited; stopped before submit" +
                   ("" if not config.AUTO_SUBMIT else " (daily limit reached)"))
        status = "dry_run_ok"
    else:
        btn = await form.submit_button(page)
        if not btn:
            set_status(job["page_id"], "manual_apply", "No submit button found")
            status = "manual_apply"
        else:
            await btn.click()
            ok = await form.wait_for_confirmation(page, 25)
            if not ok:
                set_status(job["page_id"], "needs_action", "Submitted but no confirmation (captcha/validation?). Finish in the open browser window.")
                from .notify import notify
                notify("Jobbot needs you", f"{job['company']}: finish the submit in the browser (5 min)")
                ok = await form.wait_for_confirmation(page, 300)
            if ok:
                today = datetime.now(timezone.utc).isoformat()
                set_status(job["page_id"], "submitted", "Confirmation page detected", **{"Submitted At": ns.date(today[:10])})
                log("submitted", job=job["job_key"], url=job["url"])
                status = "submitted"
            else:
                status = "needs_action"
    if not keep_open:
        await page.close()
    return status


# ---------------- assisted mode (Open Browser checkbox) ----------------

async def assist(job_page: dict, ctx) -> None:
    """Open the job in a visible window, prefill what we can verify, outline the rest in red, wait for you."""
    job = job_info(job_page)
    ns.update(job["page_id"], {"Open Browser": ns.checkbox(False)})
    plan = load_plan(job["job_key"]) or {}
    page = await ctx.new_page()
    await page.goto(job["url"], wait_until="networkidle", timeout=60000)
    if plan.get("values"):
        await _fill(page, plan)
    todo = [f["selector"] for f in plan.get("fields", []) if f["id"] not in plan.get("values", {})]
    await page.evaluate("""(sels) => sels.forEach(s => { const e = document.querySelector(s);
        if (e) { const c = e.closest('div, li, fieldset') || e; c.style.outline = '3px solid #e5484d'; c.style.outlineOffset = '4px'; } })""", todo)
    log("assist_opened", job=job["job_key"])
    await page.wait_for_event("close", timeout=0)
