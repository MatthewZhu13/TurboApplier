"""jobbot <command>

  scrape                 find new postings -> Notion Jobs (queued)
  prepare [--limit N]    read forms headlessly, map fields, raise questions
  sync                   pick up your approvals in Notion, unblock jobs
  apply [--limit N] [--job KEY] [--keep-open]
                         fill + audit ready jobs in a visible browser; submit only if AUTO_SUBMIT=true
  assist                 open jobs whose "Open Browser" box you ticked
  eeo                    approve pending EEO answers locally (never sent to Notion)
  run                    scrape -> prepare -> sync -> apply, then notify
  status                 counts by status
"""
from __future__ import annotations

import argparse
import asyncio
import collections

from playwright.async_api import async_playwright

from . import config, knowledge as kn, pipeline, sources
from . import notion_store as ns
from .notify import notify


def _jobs(status: str) -> list[dict]:
    return ns.query(config.JOBS_DS, {"property": "Status", "select": {"equals": status}})


async def cmd_prepare(limit: int) -> int:
    k = kn.load()
    jobs = _jobs("queued")[:limit]
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for j in jobs:
            try:
                await pipeline.prepare(j, k, browser)
            except Exception as e:  # one bad posting shouldn't stop the batch
                pipeline.set_status(j["id"], "manual_apply", f"prepare error: {str(e).splitlines()[0][:300]}")
        await browser.close()
    return len(jobs)


async def _ctx(p):
    return await p.chromium.launch_persistent_context(str(config.BROWSER_PROFILE), headless=False, viewport={"width": 1280, "height": 900})


async def cmd_apply(limit: int, job_key: str | None, keep_open: bool) -> collections.Counter:
    if job_key:
        jobs = ns.query(config.JOBS_DS, {"property": "Job Key", "rich_text": {"equals": job_key}})
    else:
        jobs = _jobs("ready")[:limit]
    out = collections.Counter()
    async with async_playwright() as p:
        ctx = await _ctx(p)
        for j in jobs:
            try:
                out[await pipeline.apply(j, ctx, keep_open)] += 1
            except Exception as e:
                pipeline.set_status(j["id"], "manual_apply", f"apply error: {str(e).splitlines()[0][:300]}")
                out["error"] += 1
        if keep_open:
            print("Browser left open for review. Close the window when done.")
            await ctx.wait_for_event("close", timeout=0)
        await ctx.close()
    return out


async def cmd_assist() -> None:
    jobs = ns.query(config.JOBS_DS, {"property": "Open Browser", "checkbox": {"equals": True}})
    if not jobs:
        print("No jobs have 'Open Browser' ticked.")
        return
    async with async_playwright() as p:
        ctx = await _ctx(p)
        await asyncio.gather(*(pipeline.assist(j, ctx) for j in jobs))
        await ctx.close()


def cmd_eeo() -> None:
    d = kn._load_private()
    pend = d.get("_eeo_pending", {})
    if not pend:
        print("Nothing pending.")
        return
    approved = d.setdefault("_eeo_approved", {})
    for key, q in list(pend.items()):
        print(f"\n{q['question']}\n  options: {q['options']}\n  suggested: {q['suggested']!r}  ({q['basis']})")
        ans = input("  Enter = accept suggestion, or type an option exactly, or 's' to skip: ").strip()
        if ans.lower() == "s":
            continue
        val = ans or q["suggested"]
        opt = kn.choose_option(val, q["options"])
        if not opt:
            print("  Not an option; skipped.")
            continue
        approved[key] = opt
        del pend[key]
    kn.save_private(d)
    print("Saved. Run `jobbot sync` to unblock jobs.")


def cmd_status() -> None:
    c = collections.Counter(ns.read(p, "Status") for p in ns.query(config.JOBS_DS))
    for s, n in c.most_common():
        print(f"{s:18} {n}")
    q = collections.Counter(ns.read(p, "Status") for p in ns.query(config.QUESTIONS_DS) if not ns.read(p, "Approved"))
    print("\nopen questions:", dict(q))


def main() -> None:
    ap = argparse.ArgumentParser(prog="jobbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["scrape", "prepare", "sync", "apply", "assist", "eeo", "run", "status"])
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--job")
    ap.add_argument("--keep-open", action="store_true")
    a = ap.parse_args()
    if a.cmd == "scrape":
        print("added", sources.scrape())
    elif a.cmd == "prepare":
        print("prepared", asyncio.run(cmd_prepare(a.limit)))
    elif a.cmd == "sync":
        print(pipeline.sync())
    elif a.cmd == "apply":
        print(dict(asyncio.run(cmd_apply(a.limit, a.job, a.keep_open))))
    elif a.cmd == "assist":
        asyncio.run(cmd_assist())
    elif a.cmd == "eeo":
        cmd_eeo()
    elif a.cmd == "status":
        cmd_status()
    elif a.cmd == "run":
        added = sources.scrape()
        asyncio.run(cmd_prepare(a.limit))
        s = pipeline.sync()
        res = asyncio.run(cmd_apply(a.limit, None, False))
        waiting = len(_jobs("pending_question"))
        needs = len(_jobs("needs_action")) + len(_jobs("audit_failed"))
        if waiting or needs or res:
            notify("Jobbot", f"{added} new · {dict(res)} · {waiting} waiting on answers · {needs} need you")
        print({"added": added, "sync": s, "apply": dict(res), "waiting": waiting, "needs_you": needs})


if __name__ == "__main__":
    main()
