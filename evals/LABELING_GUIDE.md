# Labelling guide: inbox categories

One label per email: what this email **is**, not what you hope it means.
Run `uv run jobagent label`. Labels are saved to `evals/inbox_labels.jsonl` (IDs only, git-ignored).

## The categories

| Key | Label | Use when… |
|---|---|---|
| `o` | `offer` | A job offer, verbal or written ("pleased to offer you…", offer letter attached). |
| `i` | `interview_invite` | They invite you to talk: ask for availability, send a booking link, propose a time, or confirm/reschedule an interview. |
| `a` | `assessment` | They send a test to complete: coding challenge, take-home, online test, video interview recorded on your own (HireVue-style), personality/aptitude test. |
| `r` | `rejection` | They're not moving forward, at any stage. |
| `t` | `recruiter_message` | A real person writes to you about a job, and it isn't one of the above: agency recruiter outreach, a hiring manager's question, a follow-up, "are you still interested?" |
| `c` | `confirmation` | An automated acknowledgement of your application ("we received your application", "thank you for applying", "thank you for your interest in the role"), including via Sorce (eprivatemail.com). |
| `x` | `other` | Everything else: job alerts, newsletters, marketing, community events, calendar invites unrelated to a job, account/security emails, personal mail. |

## When several fit, the highest one wins

`offer > interview_invite > assessment > rejection > recruiter_message > confirmation > other`

## Edge cases

| Situation | Label | Why |
|---|---|---|
| "After interviewing many candidates, unfortunately…" | `rejection` | It mentions an interview, but it's turning you down. |
| Rejection that says "we'll keep your CV on file" or "apply for other roles" | `rejection` | The outcome for *this* application is no. |
| Confirmation that also says "if shortlisted, you'll be asked to take a test" | `confirmation` | Nothing to do yet; no test has been sent. |
| An assessment that must be passed before an interview is booked | `assessment` | The action needed now is the test. |
| A calendar invite (Google Calendar or Outlook) for an interview | `interview_invite` | |
| A calendar invite for anything else (community call, webinar) | `other` | |
| Interview **reminder** or **reschedule** for an interview already booked | `interview_invite` | It's still about the interview; missing it is costly. |
| Assessment **reminder** ("your test expires in 2 days") | `assessment` | Same reason: costly to miss. |
| "Thanks for **completing** the assessment, we'll review your responses" (even with a feedback/insights link) | `confirmation` | The test is done; nothing to act on, nothing decided. |
| Someone declines a request that isn't your job application ("we can't share that information") | `other` | A rejection is only about your own application. |
| Recruiter sends a job they think suits you (you haven't applied) | `recruiter_message` | A real person, about a job. |
| A mass "jobs you might like" email, even from a recruiting agency | `other` | Automated, not about your application. |
| Offer to "book a call" from a careers coach or paid service | `other` | Marketing, not a hiring process. |
| A reply in a thread ("Re: …"), including copies of your own reply relayed back by Sorce | What the thread is about at that point (e.g. still arranging a call → `interview_invite`) | The latest message in the thread decides. |
| An email listing many jobs (Indeed/LinkedIn/Sorce alert), even if listings mention "assessment" or "interview" | `other` | Automated alert; not about one of your applications. |
| A single-job "job match" / "invite to apply" from a job board, even if "sent by the employer" | `other` | Template sent to many people. Test: could it go to 1,000 people unchanged except the name? Then `other`. |
| An empty body or an unreadable email | `s` (skip) | Don't guess. |
| Not sure after reading it all (press `m`) | `s` (skip) | A wrong label does more damage than a missing one. |

## Habits

- **Read the subject and the first paragraph, then decide.** Press `m` only when it's genuinely unclear.
- **Be consistent.** If you change your mind about a rule, write it in this file and go back (`u`) over recent emails it affects.
- **Short sessions.** Fatigue causes mistakes; 50–100 emails at a time is plenty.
- **Target:** 300–400 labelled in total, with at least **30 `interview_invite`** and at least **30 `assessment`**. The counter at the top tracks both.
