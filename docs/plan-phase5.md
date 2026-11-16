# Plan: Phase 5 lite: apply

**Goal:** turn scored jobs into sent applications, fast, without ever sending something I
haven't approved or claiming something that isn't true. Target: 50+ applications a day once
there are enough jobs (that needs more sources; see the last step).

## Which jobs get an application

Scored jobs are split into tiers. Nothing is limited to the top few.

| Tier | Rule | What gets prepared |
|---|---|---|
| **A** | score 70+ | best CV + a tailored cover letter (the ad's main requirements, matched to real CV bullets) |
| **B** | score 50–69 | best CV + a short tailored letter |
| **Skip** | under 50, or a hard blocker | nothing, unless I override |

A **hard blocker** is a fact code can check: based outside the UK, security clearance, or a
dealbreaker from the scorer. Jobs I rejected in review are never prepared.

## Screening answers (config, git-ignored, approved by me)

A local `answers.yaml` holds my answers to the questions every form asks: right to work,
sponsorship, notice period, earliest start, salary, location. Rules:

- The applier **only copies** answers from this file. It never writes its own answer to a
  screening question.
- A question with no matching answer, or one marked `ask_me`, stops that application and
  comes to me. (Example: yes/no questions about needing sponsorship *in the future*, where the
  honest answer depends on which visa is granted.)
- Salary is "open/negotiable" where the form allows text; a form that forces a number comes
  to me.

## Cover letters (local model, grounded)

- Drafted by the local model from the ad and my CV only.
- **Code checks the facts:** every number, employer, project and tool named in the letter must
  appear in my CV. A letter that fails the check is regenerated once, then comes to me
  flagged. No invented experience.
- Short: three paragraphs, under 250 words. Tier B shorter.
- **No em dashes** (—) or spaced en dashes ( – ) between clauses: they read as machine-written.
  Enforced in code, not just asked for in the prompt: a letter containing one is rewritten.
- Saved with the application, so I always know exactly what was sent.

## Steps

1. **Answers file + application packs.** `answers.yaml`; `apply prepare` builds a pack for each
   tier A/B job (CV choice, letter, answers) and stores it. Nothing is sent.
2. **Approval queue.** `apply review`: one pack at a time: approve, edit the letter, skip or
   reject. On approve: open the application page, copy the letter to the clipboard, show the
   CV file; **I press submit**. Then it asks "Sent?" and records the application (source
   `own_agent`, CV version, letter, answers, time). Reed applications are always sent this
   way: Reed's apply flow is behind a login, which the brief rules out for automation.
3. **Greenhouse source + form filling.** Add Greenhouse company boards as a job source, then
   Playwright fills their public application forms from the pack and **stops before submit**
   (manual mode). Anything it can't fill (unknown question, CAPTCHA, upload error) stops and
   comes to me. Every attempt is logged.
4. **Auto mode (later).** Only after N approvals in a row with no edits; a daily cap; hard
   rules still enforced. Off by default.
5. **More sources for volume.** Lever, Ashby, Adzuna. 47 jobs from one source isn't enough for
   50 a day.

## What counts as done (for step 1–2)

- Every tier A/B job has a pack; tier "skip" and rejected jobs don't.
- A letter naming anything not in my CV is caught by code (tested).
- Unanswerable questions come to me instead of being guessed (tested).
- Every sent application is stored with exactly what was sent.
