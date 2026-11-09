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
6. Save it as `secrets/credentials.json` (gitignored).
7. `uv run jobagent inbox fetch --since 2026-10-01`: a browser opens once to approve
   **read-only** access; the token is saved to `secrets/token.json`.

In "Testing" mode Google expires the login roughly every 7 days; the next run just
opens the browser again.

## Usage

```sh
uv run jobagent inbox fetch --since 2026-10-01   # download new emails (idempotent)
uv run jobagent apps list [--status interview]   # tracked applications
```

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Status

Phase 1 (inbox agent + eval): in progress.
