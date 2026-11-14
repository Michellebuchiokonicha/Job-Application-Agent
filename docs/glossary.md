# Glossary

Terms that came up while building jobagent, explained in plain words and tied to
where they appear in this project. Grouped by topic; newest additions at the end of each group.

## Software design

**State machine**: A system that is always in exactly one *state* and moves between states only through allowed *transitions*. Here: each application has one status (`submitted`, `interview`, …), and emails are events that move it. See `docs/state-machine.md`.

**Terminal state**: A state with no way out (`rejected`, `accepted`, …). Stops the agent from "un-rejecting" an application because of a late email.

**Single source of truth**: Each fact is defined in one place, and everything else derives from it. Here: the `ALLOWED` transition table generates the terminal set, the tests and the database `CHECK` constraints.

**Idempotency**: Doing something twice has the same effect as doing it once. Here: processing the same email twice never moves an application twice, and re-running `inbox fetch` downloads nothing it already has.

**Transaction**: A group of database writes that all succeed or all fail together. Here: changing a status and logging that change are one transaction, so the history can't disagree with the status.

**Append-only log**: Data is only ever added, never edited in place. Here: the status history, and the label file (`evals/inbox_labels.jsonl`). A crash can't corrupt earlier records.

**Tombstone**: A record that says "this was removed", used instead of deleting. Here: undoing a label appends a line with `label: null`.

**Last write wins**: When several records exist for the same key, the latest one counts. Here: how relabelling and undo work in the label file.

**Stack (LIFO)**: Last in, first out. Here: the undo stack in the labelling tool; each `u` undoes the most recent label.

**Dependency injection**: Passing a component in from outside instead of creating it inside, so tests can pass a fake. Here: the Gmail service, `sleep`, and the LLM chat client are all injected; tests use `FakeGmail` and `FakeChat`.

**Fail loudly**: When something is wrong, raise a clear error instead of silently continuing. Here: `zip(strict=True)` in the metrics, corrupt label lines report their line number, and invalid LLM output is counted and reported.

**Untrusted input**: Data from outside that could contain anything. Emails are untrusted: one malformed link crashed the labelling tool until text handling was made defensive. Related: treating email text as Rich markup (`[red]`) would be *markup injection*, the same family of bug as SQL injection.

## APIs and infrastructure

**OAuth**: How an app gets permission to act on your account without seeing your password. Here: Gmail access, approved once in the browser.

**Scope / least privilege**: The exact permission an app asks for, and the principle of asking for no more than needed. Here: `gmail.readonly` only; changing labels in Gmail would need `gmail.modify`, which requires your approval.

**Rate limit**: A cap on how many requests an API accepts per period. Gmail returned `403 rateLimitExceeded` after about 100 fast requests.

**Exponential backoff with jitter**: On a rate-limit error, wait, retry, and double the wait each time (2 s, 4 s, 8 s…), with some randomness ("jitter") so many clients don't retry at the same moment.

**Pagination**: An API returns results in pages, plus a token for the next page. Here: listing Gmail message IDs.

## Privacy and data

**Data minimisation**: Collect and keep only what you need. Here: sent mail is excluded, the label file stores IDs only (never email text), and eval output is numbers only.

**Local-first**: Data and models stay on your own machine. Here: the database is outside iCloud-synced folders, and classification uses a local model (Ollama), so email content never leaves the Mac.

**Retention policy**: Rules for when stored data gets deleted. Planned: delete the stored body of emails classified as `other`.

## Evaluation and ML

**Label / ground truth**: The correct answer for an example, decided by a human. Here: your labels in `evals/inbox_labels.jsonl` are what every classifier is measured against.

**Labelling guide**: The written rules for labelling, so labels stay consistent. `evals/LABELING_GUIDE.md`; edge cases are added as they come up.

**Dev / test split**: Labelled data divided into *dev* (used to improve the system) and *test* (used once, at the end, for an honest score). Here it's 70/30, decided by a hash of the email ID.

**Data leakage / tuning on test**: Improving a system using the test set makes the test score over-optimistic, because the exam has been studied. After the first look at test, all tuning moves to dev.

**Deterministic hash split**: Using a stable hash (SHA-256) of an ID to assign its split, so the same email is always in the same split. Python's built-in `hash()` is unsuitable because it changes between runs (hash randomisation).

**Class imbalance**: Some classes are far rarer than others. Here: about 82% of emails are `other`, and interviews are about 1%.

**Stratified / oversampled labelling**: Showing likely-rare examples more often while labelling, so you collect enough of them. Here: about 60% of the queue comes from keyword-hinted emails. Side effect: the labelled set has more rare emails than real life does.

**Baseline**: A simple method any real model must beat. Here: `always-other` (predicts the majority class) and `keyword` (regex rules).

**Confusion matrix**: A table of true label (rows) against prediction (columns). The diagonal holds the correct answers; everything else shows which classes get confused with which.

**TP / FP / FN / TN**: For one class: true positives (correctly flagged), false positives (false alarms), false negatives (missed), true negatives (correctly ignored).

**Precision**: TP / (TP + FP). "When it says interview, how often is it right?" Low precision means alert fatigue.

**Recall**: TP / (TP + FN). "Of the real interviews, how many did it catch?" The most important metric here, because a missed interview is the costly mistake. Target: ≥ 0.95 for interviews and assessments.

**F1**: The harmonic mean of precision and recall, 2PR / (P + R). High only when both are high.

**Macro F1**: The average F1 across classes, each counting equally, so rare classes can't hide behind common ones. Here, classes with no examples are left out.

**Support** (classification): How many true examples of a class exist. It tells you how much to trust that row's numbers.

**Accuracy trap / base rate**: With class imbalance, accuracy misleads. `always-other` is 82% accurate yet catches 0 interviews. Always compare against what happens anyway (the base rate).

**zero_division**: What to report when a metric divides by zero (for example, precision for a class that's never predicted). Here: 0.0, so doing nothing never looks perfect.

**Confidence interval**: The range a true value probably lies in, given limited data. 9 out of 12 caught could really be anywhere from about 47% to 91%. Few examples means a wide interval.

**Statistical significance (informal)**: Whether a difference is bigger than chance. 10/11 versus 9/12 isn't: the intervals overlap heavily.

**Association rules: support, confidence, lift, conviction**: Measures for "if A then B" patterns. *Support* = how often A and B occur together; *confidence* = P(B | A); *lift* = confidence ÷ P(B), where 1 means no relationship; *conviction* = how much more often the rule would fail if A and B were unrelated. Planned use: honest rejection-pattern analysis in Phase 6 (minimum support, lift clearly above 1). Precision is the confidence of "predicted interview → real interview".

**LLM classifier**: Using a language model to choose a category. Here: `qwen3:8b` running locally in Ollama, prompted with the labelling guide's rules.

**LLM score**: The eval result for the LLM classifier: precision, recall, F1, the confusion matrix and the rest, measured exactly like the baselines so the comparison is fair.

**Prompt version**: A version tag on the prompt (`v1`, `v2`…), so every score can be traced to the exact prompt that produced it.

**Structured output / constrained decoding**: Forcing the model to reply in a given JSON schema. Here: a Pydantic `EmailLabel` schema; any category outside the 7 is rejected.

**Temperature**: How random a model's output is. 0 = same input, same output, which is needed for reproducible evals.

**Grounding / evidence quote**: Making the model support its answer with an exact quote from the input, which code then checks. Unverifiable quotes are counted (48 of 585 in the first run) and never shown to the user as evidence.

**Hallucination**: A model stating something not supported by its input. The evidence check is a guard against it.

**Latency**: Time per prediction. Keyword: 0.09 ms. Local 8B model: about 1.3 s per email.

**Cascade**: Combining a cheap, high-recall check with a precise one. Here (planned): if *either* the keyword rules or the LLM thinks an email is an interview or assessment, it's never silently filed as `other`.

**Review queue**: A place for uncertain cases to be checked by a human instead of guessed. Here (planned): emails where keyword and LLM disagree on something important. Implements the brief's rule: no silent failures.

**Anchoring bias**: People tend to accept a suggestion they're shown, even when it's wrong. That's why the test split is labelled blind and model suggestions are allowed only for dev.

**Model-assisted labelling**: A model pre-fills a guess and the human accepts or corrects it. Much faster; acceptable for dev, never for test.

**Error analysis**: Looking at the examples a model gets wrong to find patterns ("it misses one-way video interviews") and fix them. Done on dev only.

## Concurrency

**Thread / background worker**: A second line of execution running alongside the main program. Here: one worker thread asks the model for suggestions while you read.

**Prefetching**: Starting work before it's needed, so it's ready when it is. Here: the next email's suggestion is computed while you label the current one.

**Future**: A placeholder for a result that isn't ready yet; `.result()` waits for it. Here: each email's pending suggestion.

**Thread pool (`ThreadPoolExecutor`)**: A managed set of worker threads that run submitted tasks. Here: one worker, so requests to the model run in order and never pile up.

**Thread safety (SQLite)**: A SQLite connection belongs to the thread that created it. Data is read on the main thread and only plain text is handed to the worker.

**Blind except**: `except Exception` catches everything, including bugs you'd want to see. Acceptable only when you deliberately record and surface the error, as the suggester does.

## Evaluation pitfalls

**Circular evaluation / label leakage**: Grading a model against labels it helped produce. Here: dev was labelled with qwen3:8b's suggestions, so qwen3:8b scores 0.97 on dev but 0.68 on the blind test split. The score measures agreement with yourself-plus-model, not correctness.

**Override**: When a human rejects a model's suggestion during assisted labelling. Each override is a confirmed model mistake, which makes it gold for error analysis.

**Inter-annotator agreement**: How often two labellers (or the same person under two conditions, such as blind vs assisted) give the same label. A planned optional experiment: re-label about 50 dev emails blind to measure the anchoring effect directly.

**Label noise**: Mistakes in the human labels themselves. Here: a review of 10 model/human disagreements on dev found 4 where the human label was wrong.

**One-sided relabelling (bias)**: Fixing labels only where the model disagrees. Every correction can only raise the model's score, so the score becomes inflated. Acceptable on dev for error analysis; never on test.

**Regression**: A change that fixes one thing but breaks something that used to work. Here: prompt v2 fixed all 6 targeted errors but created 9 new ones (7 `other` → confirmation, 2 assessment → confirmation). That's why the whole eval is rerun after every change.

**Precision/recall trade-off**: Making a class easier to predict catches more of it (recall up) but also produces more false alarms (precision down). Prompt v2 took confirmation recall from 0.88 to 1.00 and its precision from 0.97 to 0.82.

## Job sources and matching

**ATS (applicant tracking system)**: The software companies use to post jobs and receive applications: Greenhouse, Lever, Ashby, Workable and others. Most publish a free, public list of each company's open jobs, which is how the agent reads "company sites" without scraping.

**Aggregator**: A site that collects job ads from many places into one search (Adzuna, Jooble). Wide coverage, but often only a snippet of each ad.

**Funnel (filtering)**: Running cheap checks first and expensive ones only on what survives. Here: regex filters on title and location (instant), then an LLM fit score (about 2 s per job), then human review.

**Upsert**: "Insert, or update if it already exists." Here: `ON CONFLICT (source, external_id) DO UPDATE`, so re-fetching a job never duplicates it.

**Test double / mock transport**: A stand-in for a real dependency during tests. Here: `httpx.MockTransport` answers like the Reed API, so tests need no key and no internet.

**Enum alias**: In Python, two `Enum` members with the same value are the same member; the second name is just an alias. Caught here when "Remote" and "South" shared a rank.
