"""The only three places Claude is used. Claude returns JSON; it never clicks, types, submits or reads email.
Every output is re-validated by code (knowledge.py) before anything reaches a form."""
from __future__ import annotations

import json

import anthropic

from . import config

_client: anthropic.Anthropic | None = None

MAP_SYSTEM = """You map job-application form fields to an applicant's stored facts. You must never invent facts about the applicant.

For each field choose exactly one action:

- "template": the field directly asks for stored facts. Give a template using only {fact_key} placeholders, optional
  filters {fact_key|year} {fact_key|month_name} {fact_key|month_num} {fact_key|first}, and punctuation/spaces
  (e.g. "{first_name} {last_name}", "{city}, {state}", "{graduation_date|year}"). Never put words in a template.
  For fields with options, only use a template if the rendered value equals one option (or an option starts with it).
  Use a fact even if its value is currently empty — the applicant will be asked to fill it in.
  Files: resume -> "{resume_file}", transcript -> "{transcript_file}".
  EEO / voluntary self-identification fields map to the eeo_* facts the same way.
- "question": anything else that needs a short answer or a choice (yes/no, a pick from options, a consent or
  acknowledgement, a value that must be derived/inferred from facts such as sponsorship or graduation term).
  Give a snake_case "pattern" naming the meaning of the question. If an existing pattern has the IDENTICAL meaning
  (including the direction of a yes/no), reuse it exactly; otherwise create a new one. EEO questions use patterns
  starting with "eeo_". Set "suggested" to the answer that follows from the facts (for option fields, the exact
  option text) and "basis" to the fact keys used and why. If the facts don't settle it — legal attestations,
  consents, opinions, preferences not in the facts — leave "suggested" empty and say so in "basis".
- "essay": free-text writing (why this company, describe a project, cover-letter style prompts). No suggestion here.
- "skip": optional field with no matching fact that is fine to leave blank (optional social URLs, optional
  "if other, please specify", optional cover letter). Never skip a required field.
- "unsupported": the field cannot be filled with text/choices at all.

Scope: "universal" = the same answer applies at any company; "company" = specific to this company (mentions it, its
products, offices, people); "job" = specific to this exact role/team. Essays about the company are "company".

Fill every output key; use "" when a key doesn't apply."""

MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": ["template", "question", "essay", "skip", "unsupported"]},
                    "template": {"type": "string"},
                    "pattern": {"type": "string"},
                    "scope": {"type": "string", "enum": ["universal", "company", "job"]},
                    "suggested": {"type": "string"},
                    "basis": {"type": "string"},
                },
                "required": ["id", "action", "template", "pattern", "scope", "suggested", "basis"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fields"],
    "additionalProperties": False,
}

ESSAY_SYSTEM = """You draft short answers to job-application essay questions for the applicant.
Hard rule: every claim about the applicant must come from the provided Facts or Story Bank entries. Do not invent
experiences, skills, numbers, feelings or motivations that aren't there. Claims about the company may only come from
the provided job page text. If the material is too thin to answer honestly, write the best honest short answer and
say what's missing in "gaps". Respect any length limit in the question; default to 80-150 words. Plain, specific,
no clichés. "basis" lists the story titles and fact keys you used."""

ESSAY_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "basis": {"type": "string"}, "gaps": {"type": "string"}},
    "required": ["text", "basis", "gaps"],
    "additionalProperties": False,
}

AUDIT_SYSTEM = """You are an independent auditor checking a filled job application before it is submitted.
For each row you get the question, its options, the value currently in the form, and where the value came from.
Flag a row (ok=false) if the value does not correctly answer the question given its source — e.g. inverted yes/no,
an answer to a different question, the wrong fact (email in a phone field), a consent/attestation answered from an
unrelated source, or an essay containing claims that its source doesn't support. Otherwise ok=true with issue "".
Be strict about meaning, not about formatting."""

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "ok": {"type": "boolean"}, "issue": {"type": "string"}},
                "required": ["id", "ok", "issue"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rows"],
    "additionalProperties": False,
}


class LLMError(Exception):
    pass


def _call(system: str, user: str, schema: dict, effort: str) -> dict:
    global _client
    _client = _client or anthropic.Anthropic()
    resp = _client.beta.messages.create(
        model=config.MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        raise LLMError(f"refused: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        raise LLMError("output truncated")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


def map_fields(job: dict, fields: list[dict], facts: dict[str, str], fact_desc: dict[str, str], patterns: list[dict]) -> list[dict]:
    payload = {
        "job": job,
        "facts": [{"key": k, "description": fact_desc.get(k, ""), "value": v} for k, v in sorted(facts.items())],
        "existing_patterns": patterns,
        "fields": [{**{k: f[k] for k in ("id", "label", "kind", "required")}, "options": f["options"][:60]} for f in fields],
    }
    out = _call(MAP_SYSTEM, json.dumps(payload, ensure_ascii=False), MAP_SCHEMA, "medium")["fields"]
    return out


def draft_essay(question: str, job: dict, page_text: str, facts: dict[str, str], stories: list[dict]) -> dict:
    payload = {"question": question, "job": job, "job_page_text": page_text,
               "facts": {k: v for k, v in facts.items() if v and not k.startswith("eeo_")}, "story_bank": stories}
    return _call(ESSAY_SYSTEM, json.dumps(payload, ensure_ascii=False), ESSAY_SCHEMA, "high")


def audit(job: dict, rows: list[dict]) -> list[dict]:
    return _call(AUDIT_SYSTEM, json.dumps({"job": job, "rows": rows}, ensure_ascii=False), AUDIT_SCHEMA, "high")["rows"]
