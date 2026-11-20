# jobagent

A personal agent for the job-application flow: it triages application emails,
tracks every application through a state machine, and is building towards
(nearly) fully automated applying with a feedback loop that learns from outcomes.

See [BRIEF.md](BRIEF.md) for the full spec and phases, and
[docs/state-machine.md](docs/state-machine.md) for the core design.

## Setup

```sh
brew install uv          # if needed
uv sync                  # creates .venv and installs dependencies
cp .env.example .env     # then fill in values
```

## Gmail access (one-time)

1. [Google Cloud console](https://console.cloud.google.com/) → create a project (e.g. `jobagent`).
2. **APIs & Services → Library** → search "Gmail API" → **Enable**.
3. **Google Auth Platform** (OAuth consent screen) → Get started:
   app name `jobagent`, your email as support/contact, Audience **External**.
4. **Audience → Test users** → add your own Gmail address.
5. **Clients → Create client** → type **Desktop app** → download the JSON.
6. Save it as `~/.config/jobagent/credentials.json` (outside the repo and iCloud-synced folders).
7. `uv run jobagent inbox fetch --since 2026-10-01`: a browser opens once to approve
   **read-only** access; the token is saved to `~/.config/jobagent/token.json`. Emails are stored in `~/.local/share/jobagent/jobagent.db`.

In "Testing" mode Google expires the login roughly every 7 days; the next run just
opens the browser again.

## Your files (all outside the repo, never committed)

| File | What it holds |
|---|---|
| `~/.local/share/jobagent/cv/cv_ai_engineer.pdf`, `cv_forward_deployed_engineer.pdf` | Your CVs |
| `~/.local/share/jobagent/answers.toml` | Approved screening answers (right to work, notice, salary...) |
| `~/.local/share/jobagent/profile.toml` | Contact details for forms (name, email, phone, links) |
| `~/.local/share/jobagent/jobagent.db` | Jobs, scores, letters, saved form answers, applications |
| `~/.local/share/jobagent/screenshots/` | Each submitted form, as filled |
| `.env` | `REED_API_KEY` |

Each file explains itself if missing: the command that needs it says what to create.

## Usage

```sh
uv run jobagent daily                    # fetch new jobs, score them, write letters
uv run jobagent apply review --submit    # approve each; the agent submits the form
```

In review, **a** approves. For Greenhouse, Lever and Ashby jobs the agent reads the
form, asks you any question it has no approved answer for (once: answers are saved for
the next form), fills it in Chrome, and with `--submit` clicks Submit and waits for the
site's confirmation. A CAPTCHA, an unknown required field or no confirmation hands the
page to you. Reed jobs need a login, so they open for you to apply by hand.

Other commands:

```sh
uv run jobagent jobs list                # scored jobs, best first (jobs show ID for one)
uv run jobagent apply prepare --redo-flagged   # redraft letters the checks flagged
uv run jobagent apply recheck            # re-run the letter checks after adding one
uv run jobagent inbox fetch --since 2026-10-01  # download new emails (idempotent)
uv run jobagent apps list [--status interview]  # tracked applications
```

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Status

**v1, complete.** Job sources (Reed plus 53 company boards on Greenhouse, Lever and
Ashby), fit scoring with verified requirements, cover letters checked against the CV,
and form filling with optional submission for all three boards. The inbox classifier
(Phase 1) is parked mid-evaluation.

v1 is a single-user, local tool and stays in use while v2 is built in a new repo: an
agent (framework, tools, MCP servers) with a shared job index, built test-first.
What v1 taught is in [docs/journal.md](docs/journal.md), ending with "v1 lessons".
