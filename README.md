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

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Status

Phase 1 (inbox agent + eval): in progress.
