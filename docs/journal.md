# Build journal

A running record of what was built, why, what went wrong, and what the numbers said.
Raw material for a write-up later. Rule for this file: it's public, so no email
content, no company names, and no personal outcomes. Numbers and lessons only.

Terms in *italics* are explained in [glossary.md](glossary.md).

---

## 2026-11-08 · The plan

**Goal:** a personal agent for the job hunt: find jobs → apply → sort the inbox → prepare for interviews → debrief → learn. First milestone: stop reading "we received your application" emails and only hear about interviews, assessments, offers and real replies.

**Decisions**
- Build order chosen for speed to something useful: Phase 1 (inbox + eval) → a lite job feed → a lite applier → notifier, prep and the learning loop.
- Approval for applying will be a single switch: auto or manual.
- A third-party job app is still used daily in parallel; the agent never touches it and only reads its emails.
- Stack: Python 3.13, uv, pytest, ruff, Typer + Rich CLI, SQLite, Pydantic, Ollama for local models.

**Design: the state machine first.** The first draft mixed up *email categories* (what an email is) with *application statuses* (where an application stands). Separating them was the key design move: emails are events, statuses are state. See `docs/state-machine.md`. Rules: strict core, forgiving edges; never move backwards; process each email once (*idempotency*); every change logged, with manual overrides marked.

## 2026-11-09 · Core and storage

- `states.py`: statuses, the allowed-transition table, terminal states derived from the table (*single source of truth*). Tests are generated from the table, so every allowed and forbidden pair is covered.
- `store.py`: SQLite with `CHECK` constraints generated from the enums, partial unique indexes for idempotency, and an explicit `BEGIN IMMEDIATE` *transaction* around status changes.
- Lesson: relational (SQLite) over a document store, because applications, status history and emails are related records that must stay consistent.

## 2026-11-10/11 · Getting the email

- Gmail API with *OAuth*, read-only *scope* (*least privilege*).
- Snags: OAuth "access blocked" until I added myself as a test user; the first full fetch hit Gmail's *rate limit* after about 100 emails → added *exponential backoff with jitter*, a progress bar and resume. The second run fetched about 2,000 emails with 185 retries and no failures.
- **The privacy eye-opener.** Seeing about 2,000 personal emails land in a local database made the data question real. Changes:
  - sent mail excluded
  - database and secrets moved out of the repo and out of iCloud-synced folders, with file permissions locked down
  - a *retention policy* planned
  - the AI assistant helping me build this is not allowed to read email content: only aggregate numbers
  - the label file stores IDs only (*data minimisation*)
- Discovery: a large share of application emails arrive through one relay address with an empty sender name. Company and role must come from the email body, and the relay itself reveals the application source.

## 2026-11-11 · Labelling tool

- `jobagent label`: one key per email, saved immediately (*append-only log*), resumable, undo (*stack*, *tombstone*).
- *Dev/test split* by SHA-256 of the email ID (*deterministic hash split*), 70/30.
- Labelling queue *oversampled*: about 60% keyword-hinted emails, so rare classes show up.
- UX fixes after the first real use:
  - no feedback after a key press → added a status line
  - undo didn't work across sessions → fixed
  - unknown keys were silent → now explained
  - tracking links filled the screen → collapsed to their domain
- Bug: a malformed link in one email crashed the tool ("Invalid IPv6 URL" from a stray `[`). Lesson: emails are *untrusted input*.
- `evals/LABELING_GUIDE.md` grew as edge cases came up: replies in a thread, single-job "job match" emails, and the "could this go to 1,000 people unchanged?" test.

## 2026-11-12 · Eval harness and first scores

- Metrics written from scratch: *confusion matrix*, per-class *precision*, *recall*, *F1*, *macro F1*, safe *zero_division*.
- **585 test emails labelled blind.** About 82% `other`, about 14% confirmations, and the rare classes (interviews, assessments, rejections, offers, recruiter messages) together about 4% (*class imbalance*). Interviews + assessments: 12.

**Baselines on test**

| Classifier | Macro F1 | Interview precision | Interview+assessment recall |
|---|---|---|---|
| always-other | 0.13 | n/a | 0/11 |
| keyword (regex) | 0.54 | 0.12 | 10/11 |

- The *accuracy trap*, live: always-other is about 82% accurate and catches nothing that matters.
- Keyword catches almost every interview but raises about 4 false alarms per real one, because job alerts and confirmations contain the word "interview" or "assessment".

**First LLM score** (qwen3:8b, local, prompt v1 = the labelling guide's rules, *structured output*, *temperature* 0)

| | keyword | LLM v1 |
|---|---|---|
| Macro F1 | 0.54 | **0.68** |
| Interview precision | 0.12 | **0.42** |
| False "interview" alerts | 43 | **7** |
| Rejection F1 | 0.62 | **0.86** |
| Confirmation F1 | 0.76 | **0.87** |
| Interview+assessment recall | **10/11** | 9/12 |
| Latency | 0.09 ms | 1.3 s |
| Evidence quote not found | n/a | 48 / 585 |

**Reading it**
- The LLM is far more precise but misses more: 3 interviews/assessments were filed as `other`.
- With 12 positives, the *confidence intervals* overlap heavily, so recall 10/11 versus 9/12 is not a real difference yet.
- The two methods fail differently. Next design: a *cascade* plus a *review queue*, so disagreements on important classes go to a human instead of being dropped.
- 48 unverifiable evidence quotes need investigating (*grounding*).
- Test has now been looked at once. From here on, all tuning happens on dev (*data leakage*).

**Next:** *model-assisted labelling* for dev (suggestions allowed there, never on test, because of *anchoring bias*), then *error analysis* on dev, prompt v2, the cascade, and comparing qwen3:14b. Then one final test run.

### Later, 2026-11-12 · Assisted labelling for dev

- `jobagent label --split dev --assist`: the local model's guess is shown with its evidence quote, and **Enter accepts it**; any label key overrides it.
- Refuses to run on the test split, so the final score stays free of *anchoring bias*.
- Each assisted label also records what the model *suggested*, so we can later measure how often suggestions were accepted, and how often the human overrode the model (a rough check for anchoring).
- Speed trick (*prefetching*): while you read email N, a *background thread* asks the model about email N+1, so the ~1 s model time hides behind reading time.
- Gotcha: SQLite connections can't be shared across threads, so the main thread reads the email and passes plain text to the worker.
- A linter rule (*blind except*) flagged catching every exception. Kept on purpose, because labelling must carry on if the model is down, but the error is now stored and shown on screen instead of being swallowed.

## 2026-11-13 · Dev split labelled (assisted)

- About 350 dev emails labelled with model suggestions, over two short sessions. Interviews + assessments in dev: 40, more than three times the test split's 12, thanks to the oversampled queue.
- UX: a press of the Down arrow arrived as an escape sequence (`\x1b[B`) and showed as "unknown key". Down arrow now means "show more"; other arrows get a hint.
- New guide rule: "thanks for *completing* the assessment, we'll review it" → confirmation. The label answers "do I need to act?", not "is the word assessment in it?".
- **Anchoring check:** I accepted the model's suggestion 98% of the time on dev; on the blind test split the same model matched my labels about 93% of the time. Either dev is easier, or the suggestions pulled my labels towards the model (*anchoring bias*). We can't tell which, which is exactly why the test split was labelled blind.

### Dev scores, and the circularity trap

| On dev (351) | keyword | LLM v1 |
|---|---|---|
| Macro F1 | 0.36 | 0.97 |
| Interview+assessment recall | 32/40 | 39/40 |
| Interview precision | 0.18 | 1.00 |

- The LLM's dev score looks excellent, but it's **circular**: dev was labelled with this same model's suggestions (98% accepted), and at temperature 0 it gives identical answers again. Wherever I accepted, it's guaranteed "correct"; the only errors it can show are my ~8 overrides, and the matrix shows ~10 off-diagonal cells.
- So the dev score measures *agreement*, not *correctness*. The honest LLM v1 score remains the blind test result: macro F1 0.68, recall 9/12.
- Dev is still useful: keyword scores are fair (it played no part in the labels), the overrides are confirmed real mistakes for *error analysis*, and new methods can be developed there before one final blind test run.
- Error pattern visible even here: the model's mistakes lean towards `other` (confirmations filed as other, one interview filed as other). That's the dangerous direction for an alerting system, so it motivates the *cascade*.

### Keeping records

- Every eval run is now saved automatically as its own timestamped JSON file (`evals/runs/`): classifier, model, prompt version, split, per-class scores, confusion matrix, latency, invalid outputs, unverified quotes. Git-ignored because it contains personal counts.
- Full tables for every run so far are kept in a private, git-ignored `notes/results.md`; this journal keeps the public story and summary numbers.
- A test accidentally wrote a fake run into the real runs folder. Fixed with an autouse pytest fixture that redirects runs to a temp folder. Lesson: tests must never touch real data locations.

### 2026-11-13: Error review: the labels were wrong too

I stepped through every disagreement between the model and my dev labels. **4 of my 10 labels
were wrong: in 3 the model was right, and in 1 we were both wrong.** The one interview the model "missed" on dev wasn't an
interview at all.

Lessons:
- **Error analysis checks the labels as well as the model.** Ground truth is made by a tired
  human, and some of it is wrong (label noise).
- **You can't do this on the test split.** If I corrected test labels only where the model
  disagrees, every fix would move the score up and none would move it down. That would be
  grading the exam with the student's answers, so test labels stay as they are.
- Even after the fixes, the dev score is still circular. The remaining disagreements are the
  real model errors, and they're the input for prompt v2.

Rerunning after the fixes: dev macro F1 went from 0.97 to 0.98, and interview + assessment recall
from 39/40 to 39/39. **That isn't a better model.** Nothing about the model changed; I fixed
labels only where it disagreed, so the score could only go up. 7 real errors remain, mostly
confirmations it files as `other`. Those are what prompt v2 targets. This was also the first
run saved automatically to `evals/runs/`.

**What the remaining errors were.** Most were plain "Thank you for applying / for your interest"
acknowledgements that the model filed as `other`: the prompt defined a confirmation too narrowly.
One was a "we've received your completed assessment" email, for which the labelling guide
*already had a rule*, but the rule had never been copied into the prompt. That's **drift**
between two documents that should say the same thing. One was a non-job email that used the
word "applicant" and declined something, which the model read as a rejection.

### 2026-11-13: Prompt v2

Three changes, one for each kind of error found in the review, all written as general rules
rather than copies of specific emails (to avoid overfitting to dev):

1. **Wider confirmation definition**, with example wordings ("thank you for applying", "thank
   you for your interest in the role") and the test "nothing is decided and no action is needed".
2. **The assessment-completed rule**, copied from the labelling guide into the prompt to fix
   the drift.
3. **A rejection must be about the candidate's own application.** Declining any other request
   is `other`.

The labelling guide was updated to match, so the two documents say the same thing again.

**Prompt v2 result on dev: every targeted error was fixed, and total errors still went up (7 → 9).**
Widening the definition of "confirmation" made the model see confirmations everywhere: 7
`other` emails and, worse, 2 real assessments became confirmations. A fix that breaks something
nearby is a **regression**, and this is why you always rerun the whole eval, not just the cases
you meant to fix. A small trap: 37/39 = 0.949 was printed rounded as "0.95", which *looks* like it
meets a ≥ 0.95 target but doesn't.

## 2026-11-14: Change of direction: finding jobs comes first

The inbox classifier is paused. It works, but it solves a problem I don't have yet: a sorted
inbox doesn't get me a job. New priority order: **find and apply for jobs → interview prep →
inbox sorting → notifications**. The classifier work isn't wasted: later it tells the agent
which applications got replies, which feeds the learning loop.

Lesson: re-check the plan when the facts change. The original order assumed another tool was
doing the applying; once that stopped, sorting the inbox stopped being the bottleneck.

### Phase 4, step 1: Reed source, filters, storage

- **One `Job` shape for every source.** Reed now, Greenhouse/Lever/Ashby and others later;
  the filters and the database never need to know where a job came from.
- **A funnel, cheapest first.** Rule-based filters (role title, England only, posted in the
  last 14 days) run before anything expensive. Later, the local LLM scores only what survives.
- **Nothing dropped silently.** `jobs fetch` reports how many jobs were dropped and why.
- **Idempotent storage.** `UNIQUE (source, external_id)` plus an *upsert*: fetching the same
  job twice stores it once and just updates `last_seen_at`.
- **Untrusted input again.** Job ads are shown with `Text(...)`, never parsed as Rich markup.
- **No network in tests.** `httpx.MockTransport` plays the Reed API, so tests check paging,
  auth headers and error handling without a key or internet.
- A subtle bug caught while writing: in a Python `Enum`, two members with the same value
  become one (an *alias*). Giving "Remote" and "South" the same rank made every remote job
  display as "South".

### First real fetch from Reed

| | Count |
|---|---|
| Results from 6 searches | 841 |
| Distinct jobs (the same job matches several searches) | 688 |
| Dropped: title not a target role | 416 |
| Dropped: older than 14 days | 183 |
| Dropped: outside England | 5 |
| **Kept** | **84** |

Looking at the 84 by eye found four kinds of mistakes, each now fixed with a test:
- **Cork** (Republic of Ireland) was ranked as England: the excluded list had "Ireland" but no
  Irish towns.
- **Postcode-only locations** ("CM20 1WG") fell through to "rest of England". The letters at
  the start of a postcode (the *area*) now map to London, the South, or excluded.
- **"Lead AI Data Scientist"** got in through the AI-word rule, although Data Scientist was
  ruled out. Exclusions now win over inclusions.
- **The same job posted twice** by an agency under two IDs. Same company + title + location
  now counts as one job.

Lesson: the first run on real data is a test you can't write in advance. Read the output.

**Scope widened (2026-11-14):** Data Scientist (including AI Data Scientist) and Automation
Engineer roles are back in, with two new search terms. "Automation engineer" is ambiguous: it
also means software test automation and factory (PLC) automation, which are different careers.
Those are dropped unless the title also has an AI/ML word ("AI Test Automation Engineer" stays).
A second run after the location fixes kept 78 of the 84: the 6 gone were the Irish job, the
Data Scientist role (now back), and 2 repeat postings of jobs already listed.

**Third run, wider roles:** 1,241 results → 931 distinct → **102 kept** (24 new). The data
scientist and automation searches added real roles, and they exposed one big source of
noise: a single agency had posted the same two "trainee/junior AI" ads in **40+ different
postcodes**, filling nearly half the list. The duplicate rule now ignores location: same
company + same title = one job to apply for, wherever it's listed. Also fixed: Aberystwyth
(Wales) was counted as England, and Home Counties towns like Welwyn Garden City and Abingdon
now rank with the South.

**After collapsing repeat ads:** 1,241 results → 931 distinct → **47 kept**. 56 were the same
ad posted again (mostly one agency's ads repeated across dozens of postcodes). The list went
from 102 rows to 47 without losing a single distinct job.

### Phase 4, step 2: fit scoring

The local model (qwen3:8b) now compares each job ad with the right CV (a client-facing CV for
Forward Deployed and Solutions roles, an AI Engineer CV for the rest) and returns a 0–100 fit
score, requirements met (each quoting the ad), requirements missing (essential or not), and
dealbreakers (clearance, PhD essential, far more years than the CV shows, fees).

Same habits as the inbox classifier: a Pydantic schema, temperature 0, and quotes checked
against the ad. New guards, all learned from one smoke test on a made-up ad:
- **Years of experience are computed in code**, from the date ranges in the CV, and given to
  the model as a fact. The 8B model, left to count, called five years "under three".
- **Contradictions removed in code.** The model listed "strong Python" as missing while also
  listing Python as met.
- **The context window is set explicitly** (`num_ctx`). Ollama's default can be too small for a
  CV plus a full job ad, and the overflow is cut off without any error.
- **Constrained decoding truncates at the schema's length limit**, mid-word. A summary limit
  that was too tight produced "…which she is close to meeting (she has". The limit is now
  generous and the prompt asks for two sentences.
- **Dealbreakers cap the score at 30 in code**, not just in the prompt.

Smoke test: about 11–18 s per job on a laptop (the first call includes loading the model).

### Fit scoring v3: the first real scores were too generous

The first 3 real jobs scored 95, 85 and 85, all "strong". Reading the details showed why:
the model filed nearly every requirement as "met", with unrelated evidence ("integrate AI
with ERP and CRM systems" was "met" by an edtech app; "hybrid, central London" was "met" by
"available to start in January"). One requirement was filed under "met" with the evidence
text "Missing". The "missing" list was empty for both jobs.

This is a **calibration** problem: a score that's always high can't rank anything.

Redesign (prompt v3):
- The model makes **one small judgement per requirement**: met or not, with an exact quote
  from the ad and from the CV.
- **Code verifies both quotes** and computes the score: 80 points for essential requirements
  met, 20 for nice-to-haves. A "met" claim whose CV quote isn't in the CV counts as missing.
- **"Essential" vs "nice to have" is read from the ad's own wording** ("Nice to have:",
  "ideally", "bonus"), because the model's own labels were close to random.
- Light paraphrases of CV lines are accepted (85% of the meaningful words must appear in the
  CV); invented claims use words that aren't there.
- Location, hybrid and contract type are no longer "requirements". Contract terms and start
  dates go into a separate **"check before applying"** list.

Smoke test on two made-up ads: a good fit scored 100, a poor fit (Scala, Spark, SC clearance)
scored 0 with the clearance flagged as a dealbreaker. Before, both would likely have been "strong".

Lesson: **small models are better at many narrow yes/no judgements than one holistic
number.** Let them judge; let code count.

### Fit scoring v4: a second check that the evidence proves the claim

v3 fixed one job (AI Automation Engineer: 83, with Power Automate/n8n correctly missing and
the contract flagged) but not the other: the AI Engineer job still scored 100. Every CV quote
was **real**, but some proved nothing: "Built FastAPI services" was the evidence for
"integration with ERP, CRM and finance systems"; Docker for "infrastructure as code"; giving
talks for "using AI-assisted engineering tools". Checking that a quote *exists* doesn't check
that it's *relevant*.

v4 adds a **verifier pass**: a second, short call that sees only the pairs (requirement, CV
line) and answers, for each, "does this line directly show this specific requirement?" Strict
rules: named tools and domains must actually appear; general engineering doesn't prove a
specific tool; teaching doesn't prove hands-on use. Rejected claims become "missing", and
the score is recomputed. If the verifier fails, that's reported, never hidden.

On a made-up ad with the same kinds of requirements: RAG, agents and stakeholder work stayed
met; ERP/CRM integration and infrastructure-as-code were rejected. Score 60, "worth applying",
which is honest.

This is the **generate, then verify** pattern: one pass proposes, a second, narrower pass checks.

**First real run of v4** (3 jobs): the AI Engineer job dropped from 100 to 52; the verifier
rejected 10 weak claims, and no quote was invented. Two new problems showed up:

- **The summary contradicted the score.** "Candidate meets all essential requirements" sat
  next to six missing essentials. The model wrote the summary in the first pass, *before*
  the verifier removed claims. The summary described a result that no longer existed.
- **Bundled requirements were judged wrongly in both directions.** "Full-stack AI apps, from
  data pipelines and APIs to agent services and web front ends" is four skills in one line;
  missing one failed all four (too strict). "CI/CD, observability, infrastructure as code"
  passed on a Docker line (too generous). One yes/no can't be right about four things.

The run also showed real **CV gaps**: front-end work, Terraform and GitHub Actions were true
but not on the CV. Those are now added. A scorer that explains itself is also a CV
reviewer.

### Fit scoring v5: one skill per requirement, summary written by code

- The prompt now asks for **one skill or tool per requirement**: lists in the ad are split,
  each with the shortest quote that names it. The verifier then answers one narrow question
  at a time.
- The model no longer writes a summary. Code builds it from the verified results ("Meets 4 of
  10 essentials and 1 of 1 nice-to-haves. Main gaps: …"). It can't disagree with the
  score because it's computed from the same data.

Lesson: **anything derived must be derived after the last step that changes the data.**
Otherwise, it reports a state that no longer exists, like a cache that's never invalidated.

**v5 real run:** the AI Engineer job went *down*, 52 → 32, though the CV had improved. A
debug run of the raw model output showed why:

- The model copied the **ad's own text** as "CV quotes" (data governance, AI tools,
  greenfield). The quote check caught every one: the guard worked.
- The verifier rejected "AI applications" against a line that plainly proves it. The v4
  rule "named things must appear" was applied even to *general* requirements.

### Fit scoring v6: a fairer verifier, and requirements must come from the ad

- The verifier now sees the ad's wording too, and its rule scales with specificity:
  a named tool or system (ERP, Terraform) must appear in the CV line; a general
  requirement ("AI applications") passes on one clear, concrete example.
- After that prompt change, one run listed **~70 "requirements" copied from the CV**
  (every framework, "Google Developer Expert"), all "met": score 66. The code had been
  *counting* requirement quotes not found in the ad, but still scoring them. Now they're
  **dropped**: a requirement must be traceable to the ad. Duplicates count once.
- `jobs show` now says when the verifier rejected a claim, and shows the CV line.

Result on the same job: 15 requirements kept, 40 dropped, score 29 ("poor fit"). It misses
ERP integration, governance and the agent lifecycle, and it's a 6-month contract. That's
honest.

Two lessons:
- **A small prompt change can change behaviour a lot.** One sentence turned a 10-item list
  into 70 items. Guards in code (the quote checks) are what make that survivable; prompt
  wording alone isn't.
- **Stop tuning on one example.** Three rounds on the same job risks fitting the scorer to
  that ad. The next check is the spread across all jobs, plus a few hand-labelled ones.
  That's an eval set, as with the inbox classifier.

**v6 real run:** 48 made-up requirements dropped across 3 jobs; "AI applications" now passes.
One new problem: **splitting changed the weighting.** Job 16 had 18 essentials, and 8 of
them came from two ad bullets ("ERP, CRM, finance and document platforms"; "governance,
security, auditability and human oversight"). One bullet weighed four times as much as
"build RAG capabilities".

**Fix: judge per skill, weight per ad line.** Each line of the ad counts once, worth the
fraction of its skills met ("CI/CD, observability, IaC, code review": 2 of 4 → half a line).
Same data, different counting; no model call needed. Job 16: 27 → 40 ("stretch"), which
matches how a human would read it: strong on the AI core, missing the enterprise side.

Lesson: **the unit you judge and the unit you weight don't have to be the same.** Fine
units make judgements accurate; the weighting should follow the structure the author gave
(the ad's bullets).

### First full run (46 jobs) and the first human review

Spread: strong 5 · worth applying 7 · stretch 17 · poor fit 17. v1 put nearly everything at
85–95; now the scorer separates jobs. The code guards did a lot of work: 327 requirements
not found in the ad were dropped, 54 fake CV quotes and 155 unproven claims rejected.

I reviewed the two top scores by hand and both were wrong, for reasons the model can't see:

- **97 for "AI Automation Engineer (Python, R, Shiny)"**, missing R/Shiny. Per-line weighting
  made it one gap among 15, but it's **in the job title**: the core of the job. New rule: a
  missing skill that the title names caps the score at 55 ("stretch").
- **80 for an ML Engineer job "in Abingdon"** that is "USA based (Fully Remote)", paid in
  dollars. Reed listed it under the recruiter's UK office. New check in code, at fetch and
  at scoring: "USA based", "within the USA" or a dollar salary → dropped / dealbreaker.
  Case-sensitive, so "join us based in London" doesn't trigger it.

Also new: **`jobs recount`** re-applies the current scoring rules to the saved judgements,
with no model call. Rules changed three times without the model's answers changing; a full
rescore takes 20 minutes, a recount takes seconds. Keeping *judgements* (slow, model) apart
from *counting* (fast, code) is what makes that possible.

Lesson: **a human review of the top of the list is the cheapest eval there is.** Two jobs
checked, two rule bugs found. The errors that matter most sit at the top, because those are
the jobs you'd apply to.

**Recount results:** 4 scores changed. Two correct: both "Abingdon" jobs were in the US (one
"USA based", one "New York City, NY"); Reed lists them under the recruiter's UK office.
#46 (R/Shiny) 97 → 55, correct. One wrong: a Senior ML Engineer job capped at 55 because
"Machine Learning" counted as a missing title skill. The model had cited a CV section
heading as proof, the verifier rightly rejected it, and the new rule then turned one bad
quote into a cap. Fix: the title rule ignores the role's own words (machine learning, AI,
engineer…) and only fires on specific skills (R, Shiny, TypeScript).

Lesson: **a rule that amplifies a judgement also amplifies its errors.** Caps and
dealbreakers should rest on facts code can check, or on specific skills where a "missing"
is likely real.

### Review queue: `jobs review`

The first step where a human decides. Best first, one job at a time with the full fit
assessment: **a**ccept, **r**eject, **s**kip, **o**pen the ad in the browser, **q**uit.
An optional reason for each decision.

Design choices:

- Decisions live in their **own table, keyed by Reed's job ID**, with a copy of title,
  company and the score at the time. Rescoring and `jobs clear` would otherwise wipe them,
  and these are the matcher's eval labels: the one dataset that has to outlast everything.
- Each decision is **saved immediately**, so quitting halfway loses nothing.
- `jobs list` hides rejected jobs and marks accepted ones ✓. `jobs list --accepted` is the
  shortlist.

With decisions stored next to the score, the matcher's eval becomes a query: of the jobs
scored 80+, how many did I accept? Of the ones I accepted, how many did the scorer rank in
the top 20?

### A five-hour hang: every model call needs a limit

After the CV update I started a full rescore. Three jobs scored in about 20 seconds each,
then the progress bar stopped. Five hours later nothing more had been saved, and Ollama's
model process was still running at full CPU on job four.

The cause: our calls to Ollama had **no time limit and no cap on answer length**. The
Ollama Python client defaults to `timeout=None` (wait forever), and without `num_predict`
the model can keep generating indefinitely. A small model asked for structured output
sometimes falls into a loop (repeated text or endless whitespace), and nothing stopped it.
It wasn't the laptop: a faster machine would loop the same way, just faster.

Fixes:

- **Output cap**: `num_predict = 4096`. A real assessment is well under 2k tokens, so
  hitting the cap means a loop. Ollama then returns `done_reason: "length"`.
- **Time limit**: 300 seconds per call on the client.
- **No retry after a loop or timeout.** Invalid JSON gets one retry, but at temperature 0
  the same prompt loops the same way again, so retrying would only double the wait.
- **Named failures**: the run summary lists each job it couldn't score with the reason
  ("answer cut off at 4096 tokens"), and keeps any old score. If the verifier loops, the
  job is kept but flagged as not double-checked, as before.

Lesson: **"no silent failures" includes hangs.** A call that never returns is the quietest
failure there is: no error, no log, just a frozen progress bar. Every call to something
you don't control (a model, an API) needs a time limit, and an output limit if it
generates.

### Rescoring after a CV rewrite: a second look for proof (v7)

I rewrote both CVs: cleaner, a new "Software Engineering" skills row, a broader profile.
For readers it's better. The rescore said otherwise for some jobs: an ML Engineer role
fell 73 → 32 and an AI Engineer role 71 → 55, with "Python engineering experience" marked
*not proven*.

The cause was in the scorer, not the CV. With more summary lines on the page, the first
pass started citing them as proof: "Python, SQL, TypeScript, …". The verifier correctly
said a skills list doesn't show anything. But nothing then looked for the bullet that
*does* show it ("async Python FastAPI model services in Docker"), so a skill I use every
day was scored as missing.

Fix, v7: a **second look**. For each claim the verifier rejects, one extra call asks for a
line that proves it, and that call only sees the CV from the experience heading down. The
profile and skills list aren't in it, so the model can't cite them again. Code then checks
the new line is really in that part of the CV and isn't the rejected one, and the verifier
judges it with the same rules as everything else. If any of this fails, the rejection
stands: it's a bonus step, never a way to break scoring.

Two lessons:

- **A score that moves 40 points when you reword your CV is measuring the wording.** One
  run is a noisy measurement; the review decisions are what tell me whether the scorer is
  right.
- **Every skill in a skills list needs a bullet that shows it.** That's what the scorer
  needed, and it's what a human reader needs too. Two CV edits followed: name the stack I
  built on Velarius, and say "Python" in a job bullet.

## Phase 5 lite: from scored jobs to sent applications

The scorer was good enough to stop tuning: its job is to rank jobs for me to decide on, and
it does. The bottleneck had become sending applications. Plan: [plan-phase5.md](plan-phase5.md).

**Tiers, not a top 10.** Every job scoring 70+ (tier A) gets a tailored cover letter; 50–69
(tier B) gets a shorter one. Only jobs under 50 or with a hard blocker (US-based, clearance,
or one I rejected) are skipped. Volume matters in a job search; effort scales with fit.

**Screening answers are copied, never composed.** My answers to right to work, sponsorship,
notice period and so on live in a local file I approved. A question without an answer there
comes to me. The model never answers a screening question on my behalf.

**The model writes; code checks.** Cover letters come from the local model, then plain code
checks them:

- every number must appear in my CV or the ad ("cut latency by 60%" when the CV says 35% is
  caught),
- every capitalised name (tool, employer, project) must appear in my CV; one only the ad
  mentions is allowed but flagged, in case the letter claims it as my experience,
- no em dashes or spaced en dashes between clauses (they read as machine-written), no
  placeholders like "[Company]", and a word limit.

A failing letter is redrafted once with the problems spelled out, then shown to me flagged.
Sending a flagged letter needs an explicit "send anyway", so a false alarm can't block me.

**I press submit.** Reed's apply flow is behind a login, which the brief rules out for
automation, so `apply review` copies the letter, opens the page, and asks "Did you submit
it?". Only a yes records the application, with exactly what was sent: letter, CV file and
screening answers. It's stored as a normal application (`own_agent`, `submitted`), so the
inbox side can link the "we received your application" email to it later.

Two bugs the tests caught before any real run:

- `def load_answers(path=DEFAULT_PATH)`: Python evaluates a default argument **once, when
  the function is defined**. Overriding the module constant later had no effect, so the
  tests were quietly reading my real answers file. Fix: `path=None`, then look the default
  up inside the function.
- `typer.edit` doesn't exist. Recent Typer versions stopped depending on click and only
  vendor a private copy, so I wrote a small `$EDITOR` helper instead of importing a private
  module that could vanish on any upgrade.

### The first three letters went out with false claims

I prepared three letters and sent them. Reading them afterwards: one put my client work at
the wrong job, one presented YOLO (computer vision) as "generative AI and LLMs" work, and
one claimed microservices, Terraform-built CI/CD pipelines, cloud migrations and mentoring,
none of which my CV says. The code checks had passed all three.

Why: the checks were word-level. Every number and name in those letters *was* in my CV;
the falsehood was in how real words were combined ("delivered scalable data pipelines",
"collaborated with clients at PLATE"). A word-level check can't see a false sentence made of
true words.

Fixes, in layers:

1. **Prompt**: name the role and company; say where each piece of work happened and never
   move a fact between jobs; never upgrade a bullet ("enterprise-scale", "led",
   "mentored"); leave out what the CV doesn't show; a skills-list entry isn't hands-on
   experience.
2. **More code checks**: filler phrases ("aligns with", "meets the requirements"), repeated
   "I have" openings, and the role or company must be named.
3. **Sentence-by-sentence check by the model**: for each sentence, "does the CV back every
   claim in it, including which job it happened at?" The same generate-then-verify pattern
   as the scorer. Unbacked sentences force a redraft; if they survive it, the letter is
   flagged.

Lessons: **a check passing is not the same as the output being right**, only that it didn't
fail the things the check looks at. And the "try 3 first" step only works if those 3 are
read before anything is sent: the review queue should make that the default, not rely on
me remembering.

## More jobs: company boards on Greenhouse, Lever and Ashby

47 jobs from one source wasn't enough to apply to 50 a day. Most tech and AI companies host
their careers page on an ATS (applicant tracking system), and Greenhouse, Lever and Ashby
each publish a free, public, no-login API per company that lists **every open job with the
full ad**. A list of companies therefore covers "company websites" without scraping.

I checked about 120 likely UK AI employers against all three APIs and kept the 53 that
answered and had UK jobs (`sources/companies.toml`). A dry run on the real APIs: 7,727 open
jobs, 140 of them target roles in England or UK-remote, 3x what Reed gave, all direct from
the company.

What had to change, because these boards are worldwide and Reed is UK-only:

- **Location needs a positive UK signal.** Reed lists only UK jobs, so an unknown town
  defaulted to England. On a worldwide board, "Remote" or "San Francisco" must not. Each
  place in a multi-location string ("San Francisco; London") is judged separately.
  English names abroad ("Cambridge, MA", "Richmond, VA", "London, ON") are ruled out by
  the state or province code; and "New York" had matched "York" until a test caught it.
- **No age limit on board jobs.** A job still on a company's own board is still open,
  however long ago it was posted.
- **The full ad is saved at fetch time**, so scoring never asks Reed for a non-Reed job.
- **Title filter**: managers, sales, internships and placements are dropped even when a
  team name matches ("Technical Program Manager, Forward Deployed Engineering").
- **One failing board is named, not hidden**, and doesn't stop the rest.

A testing lesson: when `jobs fetch` started reading the boards by default, the existing
fetch tests began calling 53 real APIs and the suite hung. A shared autouse fixture now
swaps in a fake boards client for every test: tests never touch the network.

## The 14 cut-off scores weren't loops: the model was listing the CV

After adding company boards, 14 of 115 jobs failed to score with "answer cut off at 4096
tokens (model looping)". That message was a guess, and the text that would have proved it
was thrown away.

**First hypothesis: the ad was truncated.** The scorer only shows the model the first 8,000
characters of an ad (to keep ad, CV and answer inside the context window). A SQL anti-join
(jobs with details but no score) checked it: only 2 of the 14 ads were over 8,000
characters, and one of those by 44. Truncation couldn't explain the other 12.

**Then: keep the answer and read it.** A diagnostic script (`scripts/diagnose_cutoff.py`)
repeats the scorer's exact call for one job but keeps the raw reply. For one job: 76
"requirements", 69 of them distinct, so not a loop. But YOLO, Mask R-CNN, U-Net and
Terraform aren't in the ad at all; they're in the CV, and their `job_quote` was a CV
heading or a single loose word ("Evaluation"). The model listed the ad's real requirements,
ran out, and carried on through the CV's skills until it hit the cap.

**Fix (prompt v8):** the schema now allows at most 40 requirements (`maxItems`, enforced by
Ollama while generating, not checked afterwards), and the prompt says to list only what the
ad asks for. Same job afterwards: 10 requirements, all quoted from the ad, 980 tokens,
finished normally.

**Still open:** the existing guard drops requirements whose quote isn't in the ad, but a
one-word quote like "Evaluation" passes it, so v7 scores may include a few CV items. The
lesson: an error message is a hypothesis. Keep the evidence it was based on.

## The score orders the queue; it no longer decides who gets a letter

Letters went only to jobs scoring 50 or more. Of 162 jobs that passed the title and
location filter, that blocked 90. But at 50 to 80 applications a day all 162 take two or
three days, so the floor saved nothing, and the score it relied on can be wrong (one job
was "poor fit" from 4 of its ~15 requirements, after the model drifted into the CV).

Now every job that passes the filter gets a letter: 70+ gets the longer tier A letter,
anything else tier B. The score still sets the order (best first) and the letter length.
Still blocked, on purpose: dealbreakers (security clearance, PhD essential), which no
letter can get past, and jobs rejected in review.

When a rule changes on purpose, the failing tests are the to-do list: each one marks a
place the old rule was written down. They were updated to state the new rule, including
one that checks the old "scored under 50" skip never comes back.

## Letters: one heading, one opening, one name

The first full batch (135 letters) flagged 30, and reading them showed three causes that
had nothing to do with false claims:

- **The model added a heading** ("Applied AI Engineer, Startups at Anthropic") above 23
  letters. The prompt itself had a line in exactly that shape (`JOB: {title} at
  {company}`), so the model was copying the prompt. The sentence checker joined the
  heading onto the next line, producing "Applied AI Engineer at Anthropic I built...",
  which reads as a claim to have worked there, and flagged it. Fixed three ways: the
  checker splits each line on its own, code removes the heading (a line containing the
  job title with no end punctuation) the moment the model replies, and the prompt label
  is now `JOB AD (role: ...; company: ...)`.
- **Every letter opened with the same project**, the one with the most numbers, because
  the prompt said to restate bullets "with their real numbers". The prompt now fixes the
  opening ("I'm applying for the ... role at ...") and asks for the work that best fits
  this ad's top requirement, not the most numeric.
- **One letter was in the third person.** A prompt rule asks for "I", and a code check
  flags your name anywhere but under the sign-off.

The pattern: small models copy whatever shape the prompt shows them, so prompt examples
and labels are part of the instructions. And anything that must hold is enforced in code.

## Forms: read ahead, ask once, fill in a real browser, never submit

Approving a job used to mean typing every field of its form by hand. Lever's apply page
turns out to be friendly to a program: standard fields are plain inputs, and every custom
question carries its own definition (text, type, options) as JSON in the page. So the
agent reads the whole form with one HTTP request, before any browser opens.

Each question then gets an answer from one of three sources, all approved by me: my
profile (contact details, outside the repo), my screening answers, or my answer to the
same question on an earlier form. Anything else is asked once in the terminal, picked by
number for choice questions (so it's always a real option), and saved. A choice question
never gets "the nearest" option: if my answer isn't exactly one of them, it comes to me.
On a real Palantir form: 8 of 21 questions filled from the start; Palantir's second role
asks almost nothing, because the questions are the same.

Playwright then opens the form in my own Chrome, fills it, attaches the CV and stops.
It never clicks Submit: I check the page and submit it. A field it can't fill is named,
not skipped. If Chrome can't start, the answers are printed to copy instead.

Tests never open a browser: the filler takes the page as a parameter, so tests pass a
fake page that records each action, and a shared fixture blocks the real browser. (One
early run took 12 seconds instead of 1: the tests were launching Chrome.)

## The prompt's own words, back in 93 letters

A prompt line asked for "one sentence on the piece of CV work that best matches the most
important requirement in THIS ad". The model obliged literally: "...directly matches the
need for hands-on experience", "...directly addresses the need for rigorous evaluation".
The filler check only knew "aligns with", so 60 letters with this sentence passed as
clean, on top of 33 flagged. A rule that names what to avoid ("don't say 'aligns
with'") had the same effect: the model used the phrase.

Fixes: the prompt now describes what to write (what you did and where) without the
words "match", "requirement" or "need"; the filler check catches "the requirement",
"the need for", "directly addresses" and similar; the fixed opening sentence is no longer
sent to the model check (it kept being flagged as unsupported); a dash inside the job
title no longer counts as the letter's dash. And `apply prepare --redo-flagged` redrafts
only flagged letters, for next time.

Lesson, third time today: a small model copies the prompt's wording, including the words
in "don't" rules. Before trusting a check, run it over the outputs it already passed.

After the redo, 30 of 137 letters were still flagged, nearly all for one false sentence
each (often an ad requirement written up as my experience). Now, if a sentence is still
unbacked after the redraft and nothing else is wrong, code removes it instead of
flagging the letter, as long as the opening and two more sentences remain. The removed
sentence is shown as a warning in review. Shorter but true beats flagged.
