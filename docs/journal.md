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
