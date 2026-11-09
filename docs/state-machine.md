# Application state machine

The core of the agent. Every application has exactly one status at a time.
Emails (and later, the applier and you) are **events** that cause **transitions**
between statuses. Nothing edits a status directly. Everything goes through
`transition(application, new_status, reason)`, which validates the move and logs it.

> Email category ≠ application status. An email is something that *happened once*
> ("Company X sent an interview invite"). A status is where the application
> *currently stands* (`interview`). Events drive transitions.

---

## 1. Email categories (input to the state machine)

| Category | Meaning |
|---|---|
| `confirmation` | "We received your application" |
| `rejection` | Not moving forward, at any stage |
| `assessment` | A test to complete: coding challenge, take-home, online test (often with a deadline) |
| `interview_invite` | An invitation to talk: scheduling link or proposed times |
| `offer` | A job offer, verbal or written |
| `recruiter_message` | A real person writing: agency or internal recruiter, follow-ups, questions |
| `other` | Everything else: newsletters, job alerts, marketing |

**Priority when an email fits several categories** (most expensive to miss first):
`offer > interview_invite > assessment > rejection > recruiter_message > confirmation > other`

---

## 2. Application statuses

| Group | Statuses |
|---|---|
| Phase 4 (outbound pipeline, unused in Phase 1) | `discovered`, `matched`, `queued_for_review` |
| Active | `submitted`, `acknowledged`, `assessment`, `interview`, `offer` |
| Terminal | `rejected`, `withdrawn`, `accepted`, `declined`, `skipped` |

- `skipped`: I turned the job down in the review queue (Phase 4).
- `withdrawn`: I pulled out. Only I can set this.
- `accepted` / `declined`: what happened to an offer. The goal is a job, so the system records when I get one.
- **`debriefed` is not a status.** An application can have several interview rounds, and each round gets its own debrief, stored on its `Interview` row. Repeated rounds are `interview → interview`.
- `ghosted`: see open decision A.

---

## 3. Transition table

```
discovered        → matched, skipped
matched           → queued_for_review, skipped
queued_for_review → submitted, skipped
submitted         → acknowledged, assessment, interview, offer, rejected, withdrawn
acknowledged      → assessment, interview, offer, rejected, withdrawn
assessment        → assessment, interview, offer, rejected, withdrawn
interview         → interview, assessment, offer, rejected, withdrawn
offer             → accepted, declined, rejected        (rejected = offer rescinded)
rejected, withdrawn, accepted, declined, skipped → (terminal: nothing)
```

- `interview → assessment` is allowed: a take-home after a first call is common.
- `assessment → assessment` and `interview → interview` = another test or round.
  Each one is still logged as a transition, with its email as the reason.
- **Moving out of a terminal status** happens only through a **manual override** from the
  review queue, and it's logged with my reason. Code never does it on its own.

**Creating an application (bootstrapping).** Applications made outside this agent
(Sorce, manual) have no record yet. The first email seen for one creates it directly at:
`acknowledged`, `assessment`, `interview`, `offer` or `rejected`.
Creation is logged too, as a status change from `null` to that status.

---

## 4. Mapping: email category → effect

Process emails **oldest first** during a sync.

| Email | Known, active application | Unknown application | Terminal application |
|---|---|---|---|
| `confirmation` | `submitted → acknowledged`; if already further along: **link only** | create at `acknowledged` | link only |
| `rejection` | → `rejected` | create at `rejected` | link only |
| `assessment` | → `assessment` | create at `assessment` + review | **conflict** |
| `interview_invite` | → `interview` | create at `interview` + review | **conflict** |
| `offer` | → `offer` + review | create at `offer` + review | **conflict** |
| `recruiter_message` | no status change; link | store as a lead (no application) | link only |
| `other` | no effect; stored | stored | stored |

**Notify me on:** `assessment`, `interview_invite`, `offer`, `recruiter_message`.

---

## 5. Conflicts and edge cases: strict core, forgiving edges

- **`transition()` is strict.** An invalid move raises `InvalidTransition`, always.
- **The inbox agent is forgiving.** It catches the error, saves the email, sets
  `needs_review` with a readable reason (e.g. *"interview_invite for rejected application #12"*),
  and **leaves the status unchanged**. The sync never crashes because of one email.
- **Never move backwards.** An event that would move an application to an earlier
  status (e.g. a late confirmation after an interview) is *linked only*, not treated as an error.
- **Idempotency.** The same email can never cause two transitions: the email id is unique
  per transition reason.
- **Unclear email** (low confidence, failed validation, unknown company): no transition, `needs_review`.
- **Never silently drop an email.** Every fetched email ends up classified, linked, or in review.

---

## 6. Data for the feedback loop

Recorded from day one. Many fields will be **null** for Sorce applications, and that's expected.

**Per application:** company, role, seniority, location, work mode (remote/hybrid/onsite),
sponsorship needed/offered, salary band, **source** (`sorce` / `own_agent` / `manual` / `referral`),
ATS platform, CV version, job description or URL, applied_at.

**Per outcome:** final stage reached, days to outcome, rejection type
(`automated` / `template_after_review` / `personal`), `stated_reason` + `evidence_quote`
(null unless stated explicitly in the email), feedback text.

**Per interview:** stage, datetime, interviewers, questions asked, weak spots (Phase 6).

---

## 7. The feedback loop (vision)

This agent is personal to me. Its job is to get me employed in a proper job, as soon as possible,
helping at every step. Rejections are not the end of the story. They're data. The agent
looks for **why** applications fail (role fit, missing CV keywords, application mistakes,
location, visa type, salary) and turns that into suggestions. I approve them and the agent
makes the changes, so rejections go down and interviews go up over time.

**Honesty rules for rejection analysis:**
1. **Facts from email.** A reason is recorded only if the email states it, with an `evidence_quote`
   that code checks really is in the email body. Otherwise `unknown`.
2. **Facts from code, no LLM involved:** time to rejection, stage reached, ATS, template vs personal.
3. **Patterns, always with counts.** Never "you were rejected because of X". Instead:
   *"roles requiring sponsorship: 14 of 15 rejected (93%) vs 60% overall, n=15, see #3, #8, …"*.
   Below a minimum sample size: "not enough data yet".
4. **I approve every change**, and the agent measures before vs after (e.g. interview rate on CV v1 vs v2).

---

## Open decisions (mine to make)

- **A. `ghosted`: status or computed flag?**
  As a status, a scheduled job has to *move* applications into it after N days without contact,
  and every later email has to handle `ghosted → …`.
  As a flag, it's derived when needed (e.g. "acknowledged, no email for 30+ days"), with no stored state.
  *Proposed: computed flag.* It's simpler, and it can never get out of sync with the data.
- **B. Interview/assessment/offer for a terminal application: review or auto-reopen?**
  *Proposed: review + manual override.* It costs one tap, and it keeps code from silently
  reversing a final status (for example when an email is linked to the wrong application).
  The notification still goes out straight away, so I don't miss the invite.
