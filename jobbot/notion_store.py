"""Thin wrapper over the Notion API. Notion is the database; this file is the only place that knows property shapes."""
from __future__ import annotations

import time
from typing import Any

from notion_client import Client
from notion_client.errors import APIResponseError

from . import config

_client: Client | None = None


def client() -> Client:
    global _client
    if _client is None:
        if not config.NOTION_TOKEN:
            raise SystemExit("NOTION_TOKEN missing. Copy .env.example to .env and fill it in.")
        _client = Client(auth=config.NOTION_TOKEN)
    return _client


def _retry(fn, *a, **kw):
    for attempt in range(5):
        try:
            return fn(*a, **kw)
        except APIResponseError as e:
            if e.status in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise


# ---------- reading properties ----------

def read(page: dict, name: str) -> Any:
    p = page["properties"].get(name)
    if p is None:
        return None
    t = p["type"]
    if t in ("title", "rich_text"):
        return "".join(x["plain_text"] for x in p[t])
    if t == "select":
        return p["select"]["name"] if p["select"] else None
    if t == "multi_select":
        return [o["name"] for o in p["multi_select"]]
    if t == "checkbox":
        return p["checkbox"]
    if t == "url":
        return p["url"]
    if t == "number":
        return p["number"]
    if t == "date":
        return p["date"]["start"] if p["date"] else None
    if t == "relation":
        return [r["id"] for r in p["relation"]]
    if t == "rollup":
        return p["rollup"].get("number")
    return None


# ---------- writing properties ----------

def text(s: str) -> dict:
    s = s or ""
    chunks = [s[i:i + 2000] for i in range(0, len(s), 2000)][:50] or [""]
    return {"rich_text": [{"type": "text", "text": {"content": c}} for c in chunks if c] if s else []}


def title(s: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": (s or "")[:2000]}}]}


def select(s: str | None) -> dict:
    return {"select": {"name": s} if s else None}


def checkbox(b: bool) -> dict:
    return {"checkbox": bool(b)}


def url(u: str | None) -> dict:
    return {"url": u or None}


def date(iso: str | None) -> dict:
    return {"date": {"start": iso} if iso else None}


def relation(ids: list[str]) -> dict:
    return {"relation": [{"id": i} for i in ids]}


# ---------- operations ----------

def query(ds: str, filter: dict | None = None) -> list[dict]:
    out, cursor = [], None
    while True:
        kw: dict[str, Any] = {"data_source_id": ds, "page_size": 100}
        if filter:
            kw["filter"] = filter
        if cursor:
            kw["start_cursor"] = cursor
        r = _retry(client().data_sources.query, **kw)
        out += r["results"]
        if not r.get("has_more"):
            return out
        cursor = r["next_cursor"]


def create(ds: str, props: dict) -> dict:
    return _retry(client().pages.create, parent={"type": "data_source_id", "data_source_id": ds}, properties=props)


def update(page_id: str, props: dict) -> dict:
    return _retry(client().pages.update, page_id=page_id, properties=props)


def page(page_id: str) -> dict:
    return _retry(client().pages.retrieve, page_id=page_id)
