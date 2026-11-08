# Project brief: Job Application Agent

> Put this file at the root of a new repo and tell Claude Code:
> "Read BRIEF.md. Start with Phase 1. Follow the working agreement."

---

## 1. What we're building

A personal agent that runs my whole job-application flow:

**find jobs → apply → sort the inbox → prepare for interviews → debrief → learn**

I'm the only user for now. I'm an ML engineer job-hunting in the UK. I already apply through tools like Sorce, so my inbox fills with "we received your application" emails. The first goal is to **stop me reading those**. I only want to hear about interviews, assessments and real recruiter replies.

It's built in phases. **Each phase must be usable on its own before the next one starts.**

---

## 2. Working agreement (read this first, Claude Code)

- **Plan before coding.** At the start of each phase, write a short plan (files, functions, data changes) and wait for my OK.
- **Ask me design questions; don't decide them silently.** I want to understand this system well enough to explain it in an interview. Where the brief says **DECISION**, give me 2–3 options with trade-offs and a recommendation, then wait.
- **Explain as you go.** After each file, add 2–3 lines on what it does and why.
- **Keep the backend boring.** Python, SQLite, plain functions, a CLI. No microservices, queues or frameworks unless I ask.
- **Small commits** with clear messages. Tests for core logic (state machine, parsing, matching).
- **Never commit secrets or my emails.** `.env`, `data/` and `evals/*.jsonl` go in `.gitignore` from the first commit.
- If something in this brief looks wrong or risky, say so.

---

## 3. Tech stack (defaults; I can change them)

| Area | Choice |
|---|---|
| Language | Python 3.11+ |
| Storage | SQLite via SQLModel (or plain `sqlite3`) |
| LLM | **Ollama locally** for email classification and extraction. One wrapper in `llm/client.py` so a hosted model can be swapped in per task via config |
| Structured output | Ollama `format` with a JSON schema, validated with Pydantic |
| Email | Gmail API, **read-only scope** (`gmail.readonly`), OAuth token stored locally |
| CLI | Typer |
| Scheduling | Start with a manual CLI command; later cron or APScheduler |
| UI (later) | Streamlit, for the review queue and dashboard |
| Tests | pytest |

---

## 4. Architecture

```
OUTBOUND:  sources → feed builder → matcher → (review queue) → apply agent
INBOUND:   inbox agent → notifier → prep agent → debrief
SHARED:    profile + history store (read by every agent)
```

The debrief writes outcomes to the store, and the matcher reads them. That closes the loop.

### The centre of the design: the application state machine

```
discovered → matched → queued_for_review → submitted → acknowledged
                                                     ├→ rejected
                                                     ├→ assessment
                                                     └→ interview → debriefed → offer | rejected
```

- Only allowed transitions; anything else raises an error. Every transition is logged with a timestamp and its cause (e.g. the email id).
- Agents never edit status directly; they call `transition(application, new_status, reason)`.

### Data model (first pass)

- **Profile**: CV versions, master statements, preferences, **hard rules** (e.g. claims never to make)
- **Job**: source, source_job_id, company, title, location, description, url, found_at
- **Application**: job (nullable: see Phase 1), company, role, status, cv_version, cover_letter, screening_answers, match_confidence, created_at
- **EmailEvent**: gmail_message_id, thread_id, from, subject, received_at, category, extracted fields (JSON), model confidence, application_id (nullable), needs_review flag
- **StatusChange**: application_id, from, to, reason, at
- **Interview**: application_id, stage, datetime, interviewers, briefing (markdown), debrief notes

---

## 5. Repo structure

```
job-agent/
├── BRIEF.md
├── README.md
├── .env.example
├── core/
│   ├── models.py        # data model
│   ├── states.py        # state machine and transition()
│   └── store.py         # DB session and helpers
├── llm/
│   ├── client.py        # one interface: classify(), extract(), generate()
│   └── prompts/         # prompts as versioned .md files
├── agents/
│   ├── inbox.py         # Phase 1
│   ├── prep.py          # Phase 3
│   ├── matcher.py       # Phase 4
│   ├── applier.py       # Phase 5
│   └── debrief.py       # Phase 6
├── sources/             # Phase 4: greenhouse.py, lever.py, ashby.py, reed.py
├── notify/              # Phase 2
├── evals/
│   ├── inbox_labels.jsonl   # private, gitignored
│   └── run_inbox_eval.py
├── cli.py
└── tests/
```

---

## 6. Phases

### Phase 1: Inbox agent and eval *(start here)*

**Goal:** read my application emails, classify each one, extract key fields, and link them to applications.

- Gmail fetch: only emails since a given date, matching a search query (configurable, e.g. `application OR applying OR interview OR assessment`). Idempotent: never process the same message twice.
- Categories: `confirmation`, `rejection`, `interview_invite`, `assessment`, `recruiter_message`, `offer`, `other`.
- Extract: company, role, stage, any dates or deadlines, links (e.g. a scheduling link).
- **Bootstrap applications from emails.** I apply through other tools, so there are no records yet. A `confirmation` email for an unknown company and role **creates** an Application in `acknowledged`. Later emails get linked to it.
- **DECISION: email → application matching.** Senders are often generic (e.g. `no-reply@greenhouse.io`, "Talent Team"). Propose a strategy (thread id, company name normalisation, role similarity, LLM judgement) and a fallback for low confidence (`needs_review`).
- Low model confidence → `needs_review = true`. **Never silently drop an email.**
- CLI: `python cli.py inbox sync --since 2026-10-01`, `python cli.py inbox review`, `python cli.py apps list`.

**Eval (part of Phase 1, not optional):**
- `evals/inbox_labels.jsonl`: I label 50 emails to start (subject, from, body, label), growing to 150+.
- `run_inbox_eval.py` prints accuracy per category, a confusion matrix, and **recall on `interview_invite` and `assessment`**, which matters most.
- Store results per run (model name, prompt version, date) so I can compare models.
- **Target:** interview/assessment recall ≥ 0.95 before Phase 2 is considered done.
- **DECISION:** help me pick 2–3 Ollama models to compare (accuracy, speed per email, memory).

**Done when:** I can run one command, my last month of application emails is sorted, applications exist with correct statuses, and the eval shows the numbers.

### Phase 2: Notifier

- Modes, set in config: `all` / `important_only` (interview, assessment, offer, recruiter) / `digest` (daily summary).
- **DECISION:** delivery channel: email to myself, Telegram bot, or ntfy push. Recommend the simplest option that works on my phone.
- An instant alert on `interview_invite` includes company, role, stage, any date or link, and the original email link.

### Phase 3: Prep agent (briefing pack)

Fires when an application moves to `interview`. Builds a markdown briefing:
- the job description, and **exactly what was submitted** in my name (CV version, cover letter, screening answers) when known
- company research: what they build, recent news, tech stack
- the interview stage and format from the email; interviewers if named
- stage-specific prep: likely questions tied to my CV, questions for me to ask
- a mock-interview mode (text first, voice later)

The briefing must **never invent facts**. Everything is cited or marked as unknown. A hosted model is fine here.

### Phase 4: Sources, feed and matcher (review queue only)

- **Legal sources only:** Greenhouse, Lever and Ashby public job-board APIs; Reed's Jobseeker API (free key). **No LinkedIn or Indeed scraping, nothing behind a login.**
- Fetch on a schedule, cache, send an honest user-agent, keep only the fields we need, strip recruiter personal data.
- A curated company list (`sources/companies.yaml`) with ATS type and slug, and EU Lever host handling.
- Dedupe the same job across sources.
- Matcher: score jobs against my profile; I accept or reject in a review queue; **log every decision**. That becomes training data for "as good as my swipe".

### Phase 5: Apply agent *(last; hardest)*

- Browser automation (Playwright) on public application forms.
- Approval levels: `approve_every` (default) → `approve_uncertain` → `auto` only above a confidence threshold I set.
- Store exactly what was submitted. **Never make claims that break my hard rules.**
- Handle failures loudly: if a form can't be completed, tell me. Never fail silently.

### Phase 6: Debrief and learning loop

- After an interview: I give a short voice or text note; it records the questions asked, my weak spots, and drafts a thank-you note.
- Outcomes feed back into the matcher (which roles turn into interviews and offers) and into prep (my recurring weak spots).

---

## 7. Hard constraints

- **Privacy:** emails stay local. Only send email content to a hosted model if I've enabled that for the task in config.
- **Least privilege:** Gmail read-only until a phase genuinely needs more, and ask me first.
- **No silent failures:** anything uncertain goes to a review queue.
- **Honesty:** generated content (cover letters, answers, briefings) must trace back to my profile and documents. No invented experience.
- **Secrets** in `.env` only. `data/` and real email files are never committed.

---

## 8. What I want to learn from this

- Designing an agent around a state machine
- Structured LLM output and validation
- Building and using evals to make model and prompt decisions
- When to use a local model and when to use a hosted one

Point me to a paper, doc or video when a concept comes up (e.g. structured output, eval design, ReAct).
