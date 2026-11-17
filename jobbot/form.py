"""Generic form reading/filling with Playwright. One code path for Greenhouse, Lever and Ashby.

A Field is a plain dict:
  id        stable id within the page (element id / name / generated)
  selector  CSS selector for the control (or its container for radio/yesno groups)
  label     question text
  kind      text | textarea | select | combobox | autocomplete | radio | checkbox | multi_checkbox | yesno | file
  required  bool
  options   list[str] (for select/combobox/radio/multi_checkbox/yesno)
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from playwright.async_api import Page

# Runs in the page. Returns raw field descriptors; Python post-processes comboboxes.
_EXTRACT_JS = r"""
() => {
  const clean = s => (s || '').replace(/[✱*]/g, ' ').replace(/\b(attach|upload)( resume\/cv| file)?\b.*$/i, '')
      .replace(/No location found.*$/i, '').replace(/\s+/g, ' ').trim();
  const visible = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return (r.width > 0 && r.height > 0 && st.visibility !== 'hidden') || e.type === 'file'; };
  const sel = e => e.id ? '#' + CSS.escape(e.id) : (e.name ? `${e.tagName.toLowerCase()}[name="${CSS.escape(e.name)}"]` : null);
  const CTRL = 'input:not([type=hidden]):not([type=search]), textarea, select';
  const LAB = 'legend, .application-label, label, [class*="label"], [class*="Label"]';
  // Nearest ancestor (<=6 levels) that has a label-ish element and doesn't swallow other questions.
  // Text of a label element without the text of any controls/buttons it wraps.
  const labText = l => { const c = l.cloneNode(true); c.querySelectorAll('select, option, input, textarea, button, ul, [role=listbox]').forEach(x => x.remove()); return clean(c.textContent); };
  // A usable question label: has text, isn't a button, doesn't wrap a control (those are option labels).
  const forOption = l => { const t = l.htmlFor && document.getElementById(l.htmlFor); return !!t && ['radio', 'checkbox'].includes(t.type); };
  const goodLab = (l, e) => !l.contains(e) && !l.closest('button') && !l.querySelector('input, select, textarea') && !forOption(l) && labText(l);
  const container = (e, groupSize = 1) => {
    let a = e.parentElement;
    for (let d = 0; a && d < 7; d++, a = a.parentElement) {
      if (a.querySelectorAll(CTRL).length > groupSize + 2) break;
      if ([...a.querySelectorAll(LAB)].some(l => goodLab(l, e))) return a;
    }
    return e.parentElement;
  };
  const labelOf = e => {
    if (e.labels && e.labels.length) { const t = labText(e.labels[0]); if (t) return t; }
    for (const attr of ['aria-labelledby']) {
      const lb = e.getAttribute(attr);
      if (lb) { const t = clean(lb.split(' ').map(id => (document.getElementById(id)||{}).innerText||'').join(' ')); if (t) return t; }
    }
    const up = e.id && document.getElementById('upload-label-' + e.id);  // Greenhouse file inputs
    if (up) return labText(up);
    const c = container(e);
    if (c) {
      const lg = [...c.querySelectorAll(LAB)].find(l => goodLab(l, e));
      if (lg) return labText(lg);
    }
    return clean(e.getAttribute('aria-label') || e.placeholder || e.name || e.id);
  };
  const isReq = (e, label, groupSize = 1) => {
     if (e.required || e.getAttribute('aria-required') === 'true') return true;
     if (/optional/i.test(label)) return false;
     const c = container(e, groupSize);
     const up = e.id && document.getElementById('upload-label-' + e.id);
     const lt = (up ? up.innerText : '') + (e.labels && e.labels[0] ? e.labels[0].innerText : '') +
                (c ? [...c.querySelectorAll(LAB)].filter(l => goodLab(l, e)).map(l => l.innerText).join(' ') : '');
     return /[*✱]/.test(lt) || !!(c && c.querySelector('.required, [class*="required"]:not(input), [class*="Required"]:not(input)'));
  };
  const out = []; const seenGroups = new Set(); let n = 0;
  const els = document.querySelectorAll('input, textarea, select');
  for (const e of els) {
    const t = (e.type || '').toLowerCase();
    if (['hidden','submit','button','image','reset','search'].includes(t)) continue;
    if (e.name === 'g-recaptcha-response' || /recaptcha|captcha/i.test(e.id + e.name)) continue;
    if (!e.id && !e.name && !(e.labels && e.labels.length) && !e.getAttribute('aria-label')) continue; // decoy/required-shim inputs
    if (!visible(e) && t !== 'file') continue;
    if (t === 'radio' || t === 'checkbox') {
      const key = e.name || (container(e) && container(e).id) || ('grp' + n);
      const group = e.name ? [...document.querySelectorAll(`input[name="${CSS.escape(e.name)}"]`)] : [e];
      if (seenGroups.has(key)) continue; seenGroups.add(key);
      const opts = group.map(g => clean((g.labels && g.labels[0] && g.labels[0].innerText) || g.value));
      const c = container(e, group.length);
      const lg = c && [...c.querySelectorAll(LAB)].find(l => goodLab(l, e) && !group.some(g => l.contains(g)));
      const label = lg ? labText(lg) : (group.length === 1 ? opts[0] : labelOf(e));
      const kind = t === 'radio' ? 'radio' : (group.length > 1 ? 'multi_checkbox' : 'checkbox');
      out.push({id: e.name || e.id || ('f' + n++), selector: e.name ? `input[name="${CSS.escape(e.name)}"]` : sel(e), label, kind,
                required: group.some(g => g.required) || isReq(e, label, group.length), options: kind === 'checkbox' ? [] : opts});
      continue;
    }
    const label = labelOf(e);
    let kind = 'text', options = [];
    if (e.tagName === 'TEXTAREA') kind = 'textarea';
    else if (e.tagName === 'SELECT') { kind = 'select'; options = [...e.options].map(o => clean(o.text)).filter(o => o && !/^(select|choose|--|please select)/i.test(o)); }
    else if (t === 'file') kind = 'file';
    else if (e.getAttribute('role') === 'combobox' || e.getAttribute('aria-autocomplete')) kind = 'combobox';
    else if (e.id === 'location-input') kind = 'autocomplete';  // Lever location picker
    const s = sel(e); if (!s) { e.setAttribute('data-jobbot-id', 'f' + n); }
    out.push({id: e.id || e.name || ('f' + n), selector: s || `[data-jobbot-id="f${n}"]`, label, kind, required: isReq(e, label), options});
    n++;
  }
  // Ashby-style Yes/No button pairs
  for (const c of document.querySelectorAll('[class*="yesno"], [class*="YesNo"], [class*="_yesno"]')) {
    const btns = [...c.querySelectorAll('button')].filter(b => /^(yes|no)$/i.test(clean(b.innerText)));
    if (btns.length !== 2) continue;
    const box = c.closest('[class*="field-entry"], [class*="_fieldEntry"], fieldset, [class*="question"]') || c.parentElement;
    const lab = box.querySelector('label, legend, [class*="label"]');
    if (!c.id) c.setAttribute('data-jobbot-id', 'yn' + n);
    out.push({id: c.id || ('yn' + n), selector: c.id ? '#' + CSS.escape(c.id) : `[data-jobbot-id="yn${n}"]`, label: clean(lab ? lab.innerText : ''), kind: 'yesno',
              required: !!box.querySelector('[class*="required"], [class*="Required"]') || /\*\s*$/.test(lab ? lab.innerText : ''), options: ['Yes', 'No']});
    n++;
  }
  return out;
}
"""


async def _combobox_options(page: Page, selector: str) -> list[str]:
    el = page.locator(selector).first
    try:
        await el.click(timeout=3000)
        await page.wait_for_timeout(400)
        opts = await page.evaluate(
            """(sel) => { const e = document.querySelector(sel); const lb = e && e.getAttribute('aria-controls');
                 const root = (lb && document.getElementById(lb)) || document;
                 return [...root.querySelectorAll('[role=option]')].map(o => o.innerText.replace(/\\s+/g,' ').trim()).filter(Boolean).slice(0, 400); }""",
            selector,
        )
        await page.keyboard.press("Escape")
        return opts
    except Exception:
        return []


async def extract_fields(page: Page) -> list[dict]:
    fields = await page.evaluate(_EXTRACT_JS)
    for f in fields:
        if f["kind"] == "combobox":
            opts = await _combobox_options(page, f["selector"])
            # long/async lists (countries, schools, cities) are searched by typing, not enumerated
            if not opts or len(opts) >= 100 or any(re.search(r"type to search|no options", o, re.I) for o in opts):
                f["kind"], f["options"] = "autocomplete", []
            else:
                f["options"] = opts
    # de-duplicate ids
    seen: dict[str, int] = {}
    for f in fields:
        if f["id"] in seen:
            seen[f["id"]] += 1
            f["id"] = f"{f['id']}__{seen[f['id']]}"
        else:
            seen[f["id"]] = 0
    return fields


async def page_text(page: Page, limit: int = 6000) -> str:
    t = await page.evaluate("() => document.body.innerText")
    return re.sub(r"\n{3,}", "\n\n", t)[:limit]


# ---------------- filling ----------------

def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


async def _pick_option(page: Page, value: str, contains_ok: bool) -> str | None:
    opts = page.locator("[role=option]:visible, .dropdown-results > *:visible")  # last one: Lever location results
    await page.wait_for_timeout(1200)
    count = await opts.count()
    texts = [await opts.nth(i).inner_text() for i in range(min(count, 400))]
    target = norm(value)
    idx = next((i for i, t in enumerate(texts) if norm(t) == target), None)
    if idx is None and contains_ok:
        toks = target.split()
        idx = next((i for i, t in enumerate(texts) if all(tok in norm(t) for tok in toks)), None)
    if idx is None:
        return None
    await opts.nth(idx).click()
    return texts[idx].strip()


async def fill_field(page: Page, f: dict, value: str) -> str | None:
    """Fill one field. Returns the option text actually clicked, for dropdown-style fields."""
    loc = page.locator(f["selector"]).first
    k = f["kind"]
    if k in ("text", "textarea"):
        await loc.fill(value)
    elif k == "file":
        await loc.set_input_files(value)
        await page.wait_for_timeout(2500)  # some ATSs parse the resume and autofill; we overwrite afterwards
    elif k == "select":
        await loc.select_option(label=next(o for o in await loc.locator("option").all_inner_texts() if norm(o) == norm(value)))
    elif k == "combobox":
        await loc.click()
        await loc.fill(value)
        picked = await _pick_option(page, value, contains_ok=False)
        if not picked:
            raise ValueError(f"no matching option for {value!r}")
        return picked
    elif k == "autocomplete":
        words = value.replace(",", " ").split()
        queries = dict.fromkeys([value.split(",")[0].strip(), " ".join(words[:3]), words[0] if words else value])
        for q in queries:
            await loc.click()
            await loc.fill("")
            await loc.press_sequentially(q, delay=40)  # search boxes listen for keystrokes
            picked = await _pick_option(page, value, contains_ok=True)
            if picked:
                return picked
        raise ValueError(f"no matching option for {value!r}")
    elif k == "radio" or k == "multi_checkbox":
        wanted = [v.strip() for v in (value.split("\n") if k == "multi_checkbox" else [value])]
        group = page.locator(f["selector"])
        for i in range(await group.count()):
            g = group.nth(i)
            lab = await g.evaluate("e => (e.labels && e.labels[0] ? e.labels[0].innerText : e.value)")
            want = any(norm(lab) == norm(w) for w in wanted)
            if want != await g.is_checked():
                await g.evaluate("e => e.click()")
    elif k == "checkbox":
        if norm(value) in ("yes", "true", "checked"):
            await loc.check(force=True)
        else:
            await loc.uncheck(force=True)
    elif k == "yesno":
        await loc.locator("button", has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)).first.click()
    else:
        raise ValueError(f"unsupported kind {k}")


async def read_back(page: Page, f: dict) -> str:
    """What the page actually holds now for this field (the auditor compares this, not what we typed)."""
    k = f["kind"]
    return await page.evaluate(
        r"""([sel, k]) => {
          const clean = s => (s||'').replace(/\s+/g,' ').trim();
          const e = document.querySelector(sel);
          if (!e) { const up = sel.startsWith('#') && document.getElementById('upload-label-' + sel.slice(1).replace(/\\/g, ''));
                    return up ? clean(up.parentElement.innerText) : ''; }
          if (k === 'file') { if (e.files && e.files.length) return e.files[0].name;
             const up = document.getElementById('upload-label-' + e.id); if (up) return clean(up.parentElement.innerText);
             const c = e.closest('[class*="upload"], [class*="file"], .field, fieldset') || e.parentElement; return clean(c.innerText); }
          if (k === 'select') return clean(e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : '');
          if (k === 'combobox' || k === 'autocomplete') {
             const c = e.closest('.select__control, [class*="__control"], [class*="-control"]') || e.parentElement;
             const sv = c.querySelector('[class*="single-value"], [class*="singleValue"]');
             return clean(sv ? sv.innerText : (e.value || c.innerText)); }
          if (k === 'radio' || k === 'multi_checkbox') return [...document.querySelectorAll(sel)].filter(x => x.checked)
             .map(x => clean(x.labels && x.labels[0] ? x.labels[0].innerText : x.value)).join('\n');
          if (k === 'checkbox') return e.checked ? 'Yes' : 'No';
          if (k === 'yesno') { const b = [...e.querySelectorAll('button')].find(b => /active|selected|checked/i.test(b.className) || b.getAttribute('aria-pressed') === 'true');
             return b ? clean(b.innerText) : ''; }
          return e.value || '';
        }""",
        [f["selector"], k],
    )


def matches(f: dict, planned: str, actual: str, chosen: str | None = None) -> bool:
    if chosen and norm(actual) and norm(actual) in norm(chosen) and f["kind"] in ("combobox", "autocomplete"):
        return True  # widget displays a short form of the clicked option (e.g. phone country "+1")
    if f["kind"] == "file":
        return Path(planned).name.lower() in (actual or "").lower() or bool(actual) and Path(planned).stem.lower()[:15] in actual.lower()
    if f["kind"] == "autocomplete":
        return all(t in norm(actual) for t in norm(planned).split())
    if f["kind"] == "text" and re.search(r"phone|tel", f["id"] + f["label"], re.I):
        return re.sub(r"\D", "", planned)[-10:] == re.sub(r"\D", "", actual)[-10:]
    if f["kind"] == "multi_checkbox":
        return sorted(norm(x) for x in planned.split("\n")) == sorted(norm(x) for x in actual.split("\n"))
    return norm(planned) == norm(actual)


async def visible_errors(page: Page) -> list[str]:
    return await page.evaluate(
        r"""() => [...document.querySelectorAll('[class*="error"]:not(:empty), [role=alert], [aria-invalid="true"]')]
              .map(e => e.getAttribute('aria-invalid') === 'true' ? ('invalid: ' + (e.id || e.name)) : e.innerText.replace(/\s+/g,' ').trim())
              .filter(t => t && t.length < 200).slice(0, 20)"""
    )


async def submit_button(page: Page):
    for sel in ["button[type=submit]", "input[type=submit]", "button:has-text('Submit Application')", "button:has-text('Submit')"]:
        b = page.locator(sel)
        if await b.count():
            return b.first
    return None


CONFIRM_RE = re.compile(r"thank you for applying|application (has been )?(submitted|received)|thanks for applying|we('ve| have) received your application|application was submitted", re.I)


async def wait_for_confirmation(page: Page, timeout_s: int) -> bool:
    for _ in range(timeout_s):
        try:
            if CONFIRM_RE.search(await page.evaluate("() => document.body.innerText")) or re.search(r"confirmation|thank", page.url, re.I):
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False
