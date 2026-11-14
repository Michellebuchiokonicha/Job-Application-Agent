# Plan: Phase 4 lite: find and match jobs

**Goal:** every day, a ranked list of jobs I can genuinely apply for, each with the reasons it
fits and what's missing. I accept or reject each one; accepted jobs go to the applier (Phase 5).

## What I'm looking for (config, `profile.yaml`, git-ignored)

- **Roles:** ML Engineer, AI Engineer, MLOps Engineer, Applied Scientist, Forward Deployed
  Engineer, AI Forward Deployed Engineer, AI Solutions Architect, Data Scientist
  (including AI Data Scientist), Automation Engineer (AI / intelligent automation; not
  software-testing or factory automation)
- **Location:** England only. Ranked: London first, then the South of England, then the rest of
  England. UK-remote is fine. Scotland, Wales, Northern Ireland and abroad are excluded.
- **Salary:** no minimum
- **Companies:** any
- **Sponsorship:** jobs saying "no sponsorship" are **not** filtered out (from January the plan
  is a Graduate or Global Talent visa). The screening answer to give is a separate decision,
  for Phase 5.

## Sources (legal, free, no scraping)

Most company career pages are hosted on an applicant tracking system (ATS), and most of those
publish a free public list of open jobs per company. Covering the ATS APIs is how we cover
"company sites" without scraping.

**Tier 1: company ATS boards (direct from the company, full descriptions, fillable forms for Phase 5)**

| ATS | Public job list | Typical users |
|---|---|---|
| Greenhouse | Job Board API, per company | Many AI labs and scale-ups |
| Lever | Postings API, per company (separate EU host) | Startups and scale-ups |
| Ashby | Public job board API, per company | Newer AI startups |
| Workable | Public jobs endpoint, per company | UK SMEs |
| SmartRecruiters | Public Posting API, per company | Larger companies |
| Recruitee, Personio | Public JSON / XML feed, per company | European companies |

Each needs a company slug in `sources/companies.yaml`, checked against the API (a wrong slug = 404).

**Tier 2: search APIs (wide coverage, discover companies we don't know)**

| Source | Notes |
|---|---|
| Reed Jobseeker API | Free key; full UK search by keyword, location and distance |
| Adzuna API | Free key; the largest UK aggregator; description is a snippet only |
| Jooble API | Free key; another aggregator, different coverage |
| Hacker News "Who is hiring?" (official API) | Monthly thread; many AI startups, often London or remote; posts link straight to ATS pages |

**Tier 3: needs a decision first**

| Source | Issue |
|---|---|
| Company career pages with structured job data (schema.org `JobPosting`, published for Google Jobs) | Public pages, but reading them is closer to scraping: only with robots.txt and the site's terms respected, and only with your OK |
| Workday career sites | No official public API; their internal endpoints aren't meant for others. Skip unless an official route exists |

**Ruled out:** LinkedIn, Indeed, Glassdoor, Otta / Welcome to the Jungle, Totaljobs (no
public API; scraping is against their terms and the brief).

**The flywheel:** when a Reed, Adzuna or Hacker News job links to a Greenhouse, Lever, Ashby or
other ATS page, the agent reads the company slug from the link and offers to add it to
`companies.yaml`. Company coverage grows by itself, and every new company gets checked daily
for *all* its jobs, not just the one that was found.

Every source sends an honest user agent, is cached, and keeps only the fields we need. Recruiter
names and emails are stripped.

## How matching works: a funnel, cheap to expensive

1. **Filters (code, instant):** title matches a target role, location is in England, posted in
   the last 14 days, not seen before. The same job from several sources is merged (dedupe on
   company + normalised title + location).
2. **Fit score (local LLM, about 2 s per job):** the model compares the job with the CV and
   returns a 0–100 score, the requirements I meet, the ones I don't, and an evidence quote from
   the job ad. The evidence check from Phase 1 is reused.
3. **Review queue (me):** `jobs review` shows the best first. I press accept or reject, and an
   optional reason. **Every decision is logged**, which becomes the labels for the matcher's
   own eval (how often does my top 20 contain jobs I'd accept?).

## Data

- `jobs`: source, external_id, company, title, location, url, description, salary, posted_at, fetched_at
- `job_matches`: job_id, score, met / missing requirements (JSON), model, prompt_version,
  decision (accepted / rejected / null), reason, decided_at
- `applications.job_id` links an application to its job once Phase 5 applies.

## Files

- `src/jobagent/sources/`: `base.py` (one `Job` shape), `reed.py`, `adzuna.py`, `greenhouse.py`, `lever.py`, `ashby.py`
- `sources/companies.yaml`
- `src/jobagent/match/`: `filters.py`, `score.py`
- CLI: `jobs fetch`, `jobs review`, `jobs list`
- Tests with recorded fake API responses (no network in tests)

## Order of work (about 4–6 sessions)

1. Reed source + `jobs fetch` + filters → first real list of jobs (1–2 sessions)
2. Fit score with the local model + `jobs review` (1–2 sessions)
3. Greenhouse, Lever, Ashby + the slug flywheel (1 session)
4. Adzuna, Jooble, Hacker News (1 session)
5. Workable, SmartRecruiters, Recruitee, Personio (1 session; each is small once the shape exists)
6. Matcher eval from review decisions (1 session)

## Decisions needed

1. **Sources beyond the brief** (Adzuna, Jooble, Hacker News, Workable, SmartRecruiters, Recruitee, Personio): OK? (Recommended: yes)
2. **Tier 3** (structured data on company career pages): yes, no, or later? (Recommended: later, once tiers 1–2 are running)
3. **Which CV** the matcher reads (the copy without contact details stays local; only skills and experience are used)
