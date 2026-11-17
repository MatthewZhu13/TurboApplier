"""Everything the bot is allowed to know about you, and the deterministic rules for turning it into form values.

Nothing in here calls an LLM. These functions are the "no invention" guard: a value reaches a form only if
  - it is rendered from Facts by a template containing nothing but {placeholders} and punctuation, or
  - it is an Answer/Suggestion you ticked Approved in the Questions database (or approved locally for EEO).
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import dataclass, field

from . import config
from . import notion_store as ns


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# ---------------- facts ----------------

@dataclass
class Knowledge:
    facts: dict[str, str]                  # key -> value ('' = not filled in)
    fact_desc: dict[str, str]
    private_keys: set[str]
    stories: list[dict]                    # approved Story Bank rows: {title, text, tags}
    questions: list[dict] = field(default_factory=list)   # all Questions rows (raw pages)

    def approved(self) -> list[dict]:
        return [q for q in self.questions if ns.read(q, "Approved") and ns.read(q, "Status") == "approved"]


def _load_private() -> dict:
    if config.PRIVATE_FACTS.exists():
        return json.loads(config.PRIVATE_FACTS.read_text())
    return {}


def save_private(d: dict) -> None:
    config.PRIVATE_FACTS.write_text(json.dumps(d, indent=2))


def load() -> Knowledge:
    facts, desc = {}, {}
    for p in ns.query(config.FACTS_DS):
        k = (ns.read(p, "Key") or "").strip()
        if k:
            facts[k] = (ns.read(p, "Value") or "").strip()
            desc[k] = ns.read(p, "Description") or ""
    priv = {k: v for k, v in _load_private().items() if k.startswith("eeo_") and isinstance(v, str)}
    facts.update(priv)
    for k in priv:
        desc[k] = "Voluntary self-identification answer (" + k[4:].replace("_", " ") + "); empty = decline"
    stories = [
        {"title": ns.read(p, "Title"), "text": ns.read(p, "Text"), "tags": ns.read(p, "Tags")}
        for p in ns.query(config.STORY_DS, {"property": "Approved", "checkbox": {"equals": True}})
    ]
    return Knowledge(facts, desc, set(priv), stories, ns.query(config.QUESTIONS_DS))


# ---------------- templates ----------------

_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)(?:\|([a-z_]+))?\}")
_ALLOWED_LITERALS = re.compile(r"^[\s,./()+\-–]*$")


class MissingFact(Exception):
    pass


def _filter(v: str, f: str | None) -> str:
    if not f:
        return v
    m = re.match(r"(\d{4})-(\d{2})", v)
    if f == "year":
        return m.group(1) if m else v[:4]
    if f == "month_name" and m:
        return calendar.month_name[int(m.group(2))]
    if f == "month_num" and m:
        return m.group(2)
    if f == "first":
        return v.split(",")[0].strip()
    raise ValueError(f"unknown filter {f}")


def render(template: str, facts: dict[str, str]) -> str:
    """Render {key} / {key|filter} placeholders. Rejects templates that add their own words."""
    if not _ALLOWED_LITERALS.match(_PLACEHOLDER.sub("", template)):
        raise ValueError(f"template contains literal text: {template!r}")
    if not _PLACEHOLDER.search(template):
        raise ValueError("template has no placeholders")

    def sub(m):
        k, f = m.group(1), m.group(2)
        if k not in facts:
            raise ValueError(f"unknown fact {k}")
        if not facts[k]:
            raise MissingFact(k)
        return _filter(facts[k], f)

    return _PLACEHOLDER.sub(sub, template).strip()


# ---------------- options ----------------

def choose_option(value: str, options: list[str]) -> str | None:
    """Map a value onto one of a field's options, deterministically. None if ambiguous/absent."""
    v = norm(value)
    if not v:
        return None
    exact = [o for o in options if norm(o) == v]
    if len(exact) == 1:
        return exact[0]
    pref = [o for o in options if norm(o).startswith(v + " ")]
    if len(pref) == 1:
        return pref[0]
    digits = [o for o in pref if re.fullmatch(r"[\d ]+", norm(o)[len(v):].strip() or "x")]
    if len(digits) == 1:  # "United States" -> "United States +1"
        return digits[0]
    return None


def options_sig(options: list[str]) -> str:
    return hashlib.sha1("\n".join(sorted(norm(o) for o in options)).encode()).hexdigest()[:10] if options else ""


# ---------------- approved answers ----------------

def answer_value(q: dict) -> str:
    return (ns.read(q, "Answer") or "").strip() or (ns.read(q, "Suggested") or "").strip()


def find_answer(k: Knowledge, pattern: str, scope: str, company: str, job_page_id: str, field: dict) -> tuple[str, str] | None:
    """Return (value, question_page_id) of an approved answer usable for this field, else None."""
    for q in k.approved():
        if ns.read(q, "Pattern") != pattern:
            continue
        qs = ns.read(q, "Scope")
        if qs == "company" and norm(ns.read(q, "Company")) != norm(company):
            continue
        if qs == "job" and job_page_id not in ns.read(q, "Jobs"):
            continue
        if scope == "universal" and qs != "universal":
            continue
        val = answer_value(q)
        if field["options"]:
            if field["kind"] == "multi_checkbox":
                picked = [choose_option(x, field["options"]) for x in val.split("\n")]
                if all(picked):
                    return "\n".join(picked), q["id"]
                continue
            opt = choose_option(val, field["options"])
            if opt:
                return opt, q["id"]
            continue
        if val:
            return val, q["id"]
    return None


def eeo_lookup(pattern: str, field: dict) -> str | None:
    """EEO answers are approved locally (never stored in Notion)."""
    approved = _load_private().get("_eeo_approved", {})
    return approved.get(f"{pattern}|{options_sig(field['options'])}")


def eeo_queue(pattern: str, field: dict, suggested: str, basis: str) -> None:
    d = _load_private()
    pend = d.setdefault("_eeo_pending", {})
    pend[f"{pattern}|{options_sig(field['options'])}"] = {
        "question": field["label"], "options": field["options"], "suggested": suggested, "basis": basis}
    save_private(d)
