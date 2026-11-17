"""All tunables in one place. Secrets come from .env (see .env.example)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
AUTO_SUBMIT = os.environ.get("AUTO_SUBMIT", "false").lower() == "true"
MAX_SUBMITS_PER_DAY = int(os.environ.get("MAX_SUBMITS_PER_DAY", "15"))

# Notion data sources (created in the "Jobbot" page)
FACTS_DS = "41b49669-ca85-81ac-bf20-000bbfb25585"
QUESTIONS_DS = "41b49669-ca85-81f5-a26d-000b5f56061d"
JOBS_DS = "41b49669-ca85-8144-8f4b-000bd62d296c"
STORY_DS = "41b49669-ca85-8100-9355-000b27677eab"

# Claude
MODEL = "claude-opus-5-5"

# Job discovery
SOURCES = [
    ("internship", "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"),
]
CATEGORIES = {"Software", "Software Engineering", "AI/ML/Data", "Data Science, AI & Machine Learning"}
TERMS = {"Summer 2027", "N/A"}  # listing "terms"; empty list also accepted
US_ONLY = True
MAX_AGE_DAYS = 30
SUPPORTED_ATS = {"greenhouse", "lever", "ashby"}
# Unsupported ATS (Workday, iCIMS, ...) are added straight to manual_apply when True.
INCLUDE_UNSUPPORTED = False
COMPANY_BLOCKLIST: set[str] = set()

# Browser
BROWSER_PROFILE = DATA / "browser-profile"
PLANS = DATA / "plans"
SCREENS = DATA / "screens"
LOG = DATA / "log.jsonl"
PRIVATE_FACTS = DATA / "private_facts.json"
for p in (PLANS, SCREENS):
    p.mkdir(exist_ok=True)
