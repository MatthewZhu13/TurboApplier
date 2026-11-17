# Jobbot

Finds internship postings, fills applications from **your** facts, audits them, and submits only when every
value traces back to something you entered or approved. Notion is the database and dashboard.

## How it flows

```
SimplifyJobs listings ─▶ scrape ─▶ Jobs (queued)
                                     │
                       prepare (headless): read the form, Claude maps each field
                                     │
          ┌──────────────────────────┼─────────────────────────────┐
     all answerable            needs your input                  can't operate form
       ▼ ready          ▼ pending_question (Questions DB)          ▼ manual_apply
          │                     you answer + tick Approved
          │                     sync ─▶ every job waiting on it unblocks
          ▼
 apply (visible browser): fill ─▶ read back the DOM ─▶ deterministic audit ─▶ Claude audit
          │
   AUTO_SUBMIT=false ─▶ dry_run_ok (stops before submit)
   AUTO_SUBMIT=true  ─▶ submit ─▶ confirmation page ─▶ submitted
                                 └▶ captcha / no confirmation ─▶ needs_action (window stays open 5 min)
```

**The "no invention" rule is enforced in code** (`jobbot/knowledge.py`), not in a prompt:
- Template values may contain only `{fact_key}` placeholders and punctuation. `"Yes"` or `"I am {first_name}"` are rejected.
- Everything else (yes/no, choices, consents, essays) must be an answer you **Approved** in Notion.
- Option fields only accept an exact option (or one unique prefix match like "No" → "No, I will not require sponsorship").
- Before submit, every field is read back from the page and compared, then an independent Claude call checks meaning.

## Notion (your dashboard)

Page **Jobbot** contains:
| Database | You do |
|---|---|
| **Facts** | Fill in every Value once. `resume_file` = absolute path to your PDF. |
| **Questions** | *To answer* view: pick/enter an Answer (or keep Suggested), tick **Approved**. *Essays to review*: edit the draft, tick Approved. |
| **Jobs** | *Needs you* view: tick **Open Browser** to have it opened and prefilled (unfilled fields outlined red); tick **Manually Done** after submitting yourself. |
| **Story Bank** | Paragraphs about you (projects, motivations). Essays may only make claims found here or in Facts. Tick Approved. |

EEO answers never go to Notion: they live in `data/private_facts.json` and are approved in the terminal with `./jobbot.sh eeo`.

## Setup

1. Create a Notion integration at https://www.notion.so/profile/integrations (internal), copy its secret.
   On the **Jobbot** page: `•••` → *Connections* → add the integration.
2. `cp .env.example .env` and fill in `NOTION_TOKEN` and `ANTHROPIC_API_KEY`.
3. `cp private_facts.example.json data/private_facts.json` and fill in EEO answers (empty = decline).
4. Fill in **Facts** in Notion and add a few **Story Bank** entries.

## Commands

```
./jobbot.sh scrape            # new postings -> Notion
./jobbot.sh prepare           # read forms, raise questions (10 at a time; --limit N)
./jobbot.sh sync              # pick up your approvals
./jobbot.sh apply --limit 3   # fill + audit in a visible browser
./jobbot.sh apply --job simplify:<id> --keep-open   # review one by eye
./jobbot.sh assist            # open jobs you ticked "Open Browser" on
./jobbot.sh eeo               # approve EEO answers locally
./jobbot.sh run               # all of the above + notification
./jobbot.sh status
```

Schedule it: see `com.jobbot.run.plist` (every 3 h via launchd).

## Safety switches (.env)
- `AUTO_SUBMIT=false` (default): fills and audits, never clicks submit. Flip only after reviewing dry runs.
- `MAX_SUBMITS_PER_DAY=15`.

## Files
- `data/plans/*.json` – per-job form snapshot, Claude's mapping, resolved values, read-back
- `data/log.jsonl` – append-only audit trail of everything filled/submitted
- `data/screens/*.png` – screenshot of every filled form
- `data/browser-profile/` – the browser profile used for applying (keeps cookies, e.g. ATS logins)

## Coverage today
Greenhouse, Lever, Ashby (≈20% of matching postings). Workday/iCIMS/other are skipped
(`INCLUDE_UNSUPPORTED=True` in `config.py` adds them to the manual queue).
