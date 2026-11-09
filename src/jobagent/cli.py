"""Command-line entry point: `uv run jobagent ...`."""

from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from jobagent.core.config import Settings
from jobagent.core.states import Status
from jobagent.core.store import connect, count_raw_emails, init_db, list_applications

app = typer.Typer(help="Personal job-application agent.", no_args_is_help=True)
inbox_app = typer.Typer(help="Read and sort application emails.", no_args_is_help=True)
apps_app = typer.Typer(help="Inspect tracked applications.", no_args_is_help=True)
app.add_typer(inbox_app, name="inbox")
app.add_typer(apps_app, name="apps")

console = Console()


def _open_db(settings: Settings):
    conn = connect(settings.db_path)
    init_db(conn)
    return conn


@inbox_app.command("fetch")
def inbox_fetch(
    since: Annotated[
        datetime,
        typer.Option(
            formats=["%Y-%m-%d"], help="Fetch emails received on/after this date."
        ),
    ],
) -> None:
    """Download new emails from Gmail into the local database (read-only access)."""
    # Imported here so other commands don't pay the Google libraries' import time.
    from jobagent.gmail.auth import build_service, get_credentials
    from jobagent.gmail.fetch import fetch_new

    settings = Settings.from_env()
    conn = _open_db(settings)
    creds = get_credentials(settings.gmail_credentials_path, settings.gmail_token_path)
    service = build_service(creds)

    since_utc = since.replace(tzinfo=UTC)
    console.print(f"Query: [cyan]{settings.gmail_query}[/] since {since:%Y-%m-%d}")
    stats = fetch_new(service, conn, settings.gmail_query, since_utc)
    console.print(
        f"Matched {stats.listed}, already stored {stats.already_stored}, "
        f"[green]new {stats.stored}[/]. Total stored: {count_raw_emails(conn)}"
    )


@apps_app.command("list")
def apps_list(
    status: Annotated[
        Status | None, typer.Option(help="Only show this status.")
    ] = None,
) -> None:
    """List tracked applications, most recently updated first."""
    conn = _open_db(Settings.from_env())
    rows = list_applications(conn, status)
    table = Table("ID", "Company", "Role", "Status", "Source", "Updated")
    for r in rows:
        table.add_row(
            str(r["id"]),
            r["company"],
            r["role"],
            r["status"],
            r["source"],
            r["updated_at"][:10],
        )
    console.print(table if rows else "No applications yet.")


if __name__ == "__main__":
    app()
