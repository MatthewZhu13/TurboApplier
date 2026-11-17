"""Discover postings from the SimplifyJobs listing files and add new matches to the Notion Jobs database."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import httpx

from . import config
from . import notion_store as ns
from .log import log

_US = re.compile(r", [A-Z]{2}$|remote|united states|\busa?\b", re.I)


def detect_ats(u: str) -> str:
    host = re.sub(r"^https?://", "", u).split("/")[0].lower()
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "myworkdayjobs.com" in host:
        return "workday"
    return "other"


def apply_url(u: str, ats: str) -> str:
    u = u.split("?")[0].rstrip("/")
    if ats == "lever" and not u.endswith("/apply"):
        u += "/apply"
    if ats == "ashby" and not u.endswith("/application"):
        u += "/application"
    return u


def _matches(j: dict) -> bool:
    if not j.get("active") or not j.get("is_visible", True):
        return False
    if j.get("category") not in config.CATEGORIES:
        return False
    terms = set(j.get("terms") or [])
    if terms and not terms & config.TERMS:
        return False
    if config.US_ONLY and not any(_US.search(l) for l in (j.get("locations") or ["Remote"])):
        return False
    if time.time() - j.get("date_posted", 0) > config.MAX_AGE_DAYS * 86400:
        return False
    if j.get("company_name", "").lower() in {c.lower() for c in config.COMPANY_BLOCKLIST}:
        return False
    return True


def scrape() -> int:
    existing = {ns.read(p, "Job Key") for p in ns.query(config.JOBS_DS)}
    added = 0
    for kind, src in config.SOURCES:
        listings = httpx.get(src, timeout=60, follow_redirects=True).json()
        for j in listings:
            if not _matches(j):
                continue
            key = f"simplify:{j['id']}"
            if key in existing:
                continue
            ats = detect_ats(j["url"])
            supported = ats in config.SUPPORTED_ATS
            if not supported and not config.INCLUDE_UNSUPPORTED:
                continue
            posted = datetime.fromtimestamp(j["date_posted"], timezone.utc).date().isoformat()
            ns.create(config.JOBS_DS, {
                "Title": ns.title(j["title"]),
                "Company": ns.text(j["company_name"]),
                "URL": ns.url(apply_url(j["url"], ats)),
                "ATS": ns.select(ats if ats != "other" else "other"),
                "Job Key": ns.text(key),
                "Location": ns.text(", ".join(j.get("locations") or [])),
                "Kind": ns.select(kind),
                "Posted": ns.date(posted),
                "Status": ns.select("queued" if supported else "manual_apply"),
                "Reason": ns.text("" if supported else f"No adapter for {ats}"),
            })
            existing.add(key)
            added += 1
    log("scrape", added=added)
    return added
