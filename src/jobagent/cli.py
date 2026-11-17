"""Command-line entry point: `uv run jobagent ...`."""

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress
from rich.table import Table
from rich.text import Text

from jobagent.core.categories import Category
from jobagent.core.config import Settings
from jobagent.core.states import Status
from jobagent.core.store import (
    connect,
    count_raw_emails,
    get_raw_email,
    init_db,
    list_applications,
)
from jobagent.core.text import clean_for_reading
from jobagent.eval.labels import (
    LABELS_PATH,
    append_label,
    build_queue,
    load_labels,
    split_for,
)

app = typer.Typer(help="Personal job-application agent.", no_args_is_help=True)
inbox_app = typer.Typer(help="Read and sort application emails.", no_args_is_help=True)
apps_app = typer.Typer(help="Inspect tracked applications.", no_args_is_help=True)
app.add_typer(inbox_app, name="inbox")
eval_app = typer.Typer(
    help="Measure classifiers against your labels.", no_args_is_help=True
)
app.add_typer(apps_app, name="apps")
app.add_typer(eval_app, name="eval")

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
    try:
        creds = get_credentials(
            settings.gmail_credentials_path, settings.gmail_token_path
        )
    except FileNotFoundError as e:
        # An expected setup problem, not a bug: a clear message instead of a traceback.
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=1) from None
    service = build_service(creds)

    since_utc = since.replace(tzinfo=UTC)
    console.print(f"Query: [cyan]{settings.gmail_query}[/] since {since:%Y-%m-%d}")
    with Progress(console=console) as progress:
        task = progress.add_task("Downloading", total=None)

        def on_progress(done: int, total: int) -> None:
            progress.update(task, completed=done, total=total)

        try:
            stats = fetch_new(
                service, conn, settings.gmail_query, since_utc, on_progress
            )
        except KeyboardInterrupt:
            console.print("[yellow]Stopped. Re-run to resume where you left off.[/]")
            raise typer.Exit(code=130) from None
    console.print(
        f"Matched {stats.listed}, already stored {stats.already_stored}, "
        f"[green]new {stats.stored}[/], rate-limit retries {stats.retries}. "
        f"Total stored: {count_raw_emails(conn)}"
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


# --- Labelling -------------------------------------------------------------

LABEL_KEYS: dict[str, Category] = {
    "o": Category.OFFER,
    "i": Category.INTERVIEW_INVITE,
    "a": Category.ASSESSMENT,
    "r": Category.REJECTION,
    "t": Category.RECRUITER_MESSAGE,
    "c": Category.CONFIRMATION,
    "x": Category.OTHER,
}
KEY_HELP = (
    "[bold]o[/] offer   [bold]i[/] interview   [bold]a[/] assessment   "
    "[bold]r[/] rejection\n"
    "[bold]t[/] recruiter (a real person writing about a job)   "
    "[bold]c[/] confirmation   [bold]x[/] other (not job-related)\n"
    "[dim]s skip · u undo last label · m show more · q quit[/]"
)
PREVIEW_CHARS = 1500
TARGET_PER_RARE_CLASS = 30


def _show_email(
    row,
    remaining: int,
    counts: Counter,
    chars: int,
    status: str,
    suggestion=None,
) -> None:
    body = clean_for_reading(row["body_text"]) or "(empty body)"
    shown = body[:chars]
    if len(body) > chars:
        shown += f"\n\n… {len(body) - chars} more characters (press m)"
    header = (
        f"{row['from_name']} <{row['from_addr']}>\n"
        f"{row['received_at'][:16].replace('T', ' ')}"
    )
    console.clear()
    console.print(
        f"[dim]{remaining} left · {sum(counts.values())} labelled · "
        f"interview {counts['interview_invite']}/{TARGET_PER_RARE_CLASS} · "
        f"assessment {counts['assessment']}/{TARGET_PER_RARE_CLASS}[/]"
    )
    # Text(...) prints email content literally: an email containing "[red]"
    # must not be interpreted as Rich markup.
    console.print(
        Panel(
            Text(shown), title=Text(row["subject"] or "(no subject)"), subtitle=header
        )
    )
    if suggestion is not None:
        line = Text.assemble(
            ("Model suggests: ", "cyan"),
            (suggestion.category.value, "bold cyan"),
            "  ",
            (f"“{suggestion.evidence_quote}”", "italic"),
            ("" if suggestion.evidence_found else "  (quote not in email)", "dim"),
            ("   Enter = accept", "bold"),
        )
        console.print(line)
    console.print(KEY_HELP)
    if status:
        console.print(Text(status, style="bold yellow"))


def _make_predictor(settings: Settings):
    """The local model's predict function. A separate function so tests can swap it."""
    import ollama

    from jobagent.inbox.classify import LLMClassifier

    client = ollama.Client(host=settings.ollama_host)
    return LLMClassifier(client, settings.inbox_model).predict


ENTER = ("\r", "\n")
# Down arrow (normal and "application" cursor mode) = show more; other arrows explained.
ARROW_KEYS = {
    "\x1b[B": "m",
    "\x1bOB": "m",
    **{
        seq: "arrow"
        for seq in ("\x1b[A", "\x1b[C", "\x1b[D", "\x1bOA", "\x1bOC", "\x1bOD")
    },
}


@app.command("label")
def label(
    labels_path: Annotated[
        Path, typer.Option("--labels", help="Where labels are saved (IDs only).")
    ] = LABELS_PATH,
    split: Annotated[
        str | None,
        typer.Option(help="Only show emails from this split: dev or test."),
    ] = None,
    assist: Annotated[
        bool,
        typer.Option(help="Show the local model's guess; Enter accepts. Dev only."),
    ] = False,
) -> None:
    """Hand-label emails for the inbox eval. Saves after every key; quit any time."""
    from jobagent.eval.assist import Suggester

    if split not in (None, "dev", "test"):
        console.print("[red]--split must be dev or test.[/]")
        raise typer.Exit(code=2)
    if assist and split != "dev":
        # Anchoring bias: seeing a suggestion pulls your label towards it. The test
        # split must stay blind so the final score is honest.
        console.print(
            "[red]--assist only works with --split dev (test stays blind).[/]"
        )
        raise typer.Exit(code=2)

    settings = Settings.from_env()
    conn = _open_db(settings)
    labels = load_labels(labels_path)
    in_scope = [r for r in labels.values() if split is None or r["split"] == split]
    counts = Counter(r["label"] for r in in_scope)
    queue = build_queue(conn, set(labels))
    if split:
        queue = [mid for mid in queue if split_for(mid) == split]
    # Undo stack of email IDs, oldest first. Seeded from the file, so undo also
    # reaches labels made in earlier sessions (only within this split).
    undo_stack = [
        r["gmail_message_id"] for r in sorted(in_scope, key=lambda r: r["labelled_at"])
    ]
    if not queue and not undo_stack:
        console.print("Nothing to label.")
        return

    suggester = Suggester(_make_predictor(settings)) if assist else None
    accepted = suggested_total = 0

    def prefetch(position: int) -> None:
        if suggester and position < len(queue):
            r = get_raw_email(conn, queue[position])
            suggester.prefetch(
                queue[position],
                clean_for_reading(r["subject"]),
                clean_for_reading(r["body_text"]),
            )

    i = 0
    chars = PREVIEW_CHARS
    status = ""
    try:
        with console.screen():  # a separate screen, like vim/less: no scrollback
            while i < len(queue):
                mid = queue[i]
                suggestion = None
                if suggester:
                    prefetch(i)  # usually already done: no-op
                    prefetch(i + 1)  # the model works on the next one while you read
                    if not status:
                        status = "Model is thinking…" if i == 0 else ""
                    suggestion = suggester.get(mid)
                    if suggestion is None and suggester.last_error:
                        status = f"Model unavailable ({suggester.last_error}). Label by hand."
                _show_email(
                    get_raw_email(conn, mid),
                    len(queue) - i,
                    counts,
                    chars,
                    status,
                    suggestion,
                )
                status = ""

                try:
                    raw_key = typer.getchar()
                except (KeyboardInterrupt, EOFError):
                    raw_key = "q"
                # Arrow keys arrive as escape sequences, e.g. Down = "\x1b[B".
                # Check before lower(), which would mangle them.
                key = ARROW_KEYS.get(raw_key, raw_key.lower())
                if key == "arrow":
                    status = "Arrow keys: ↓ shows more. Use the letter keys to label."
                    continue

                if key in ENTER and suggestion is not None:
                    key = next(
                        k for k, c in LABEL_KEYS.items() if c == suggestion.category
                    )
                    accepted += 1

                if key in LABEL_KEYS:
                    category = LABEL_KEYS[key]
                    append_label(
                        labels_path,
                        mid,
                        category,
                        suggested=suggestion.category if suggestion else None,
                    )
                    if suggestion:
                        suggested_total += 1
                    counts[category] += 1
                    undo_stack.append(mid)
                    status = f"✓ Saved previous email as {category.value}"
                    i, chars = i + 1, PREVIEW_CHARS
                elif key == "s":
                    status = "Skipped previous email (it will come back next session)"
                    i, chars = i + 1, PREVIEW_CHARS
                elif key == "m":
                    chars *= 4
                elif key == "u":
                    if not undo_stack:
                        status = "Nothing to undo"
                        continue
                    undone = undo_stack.pop()
                    previous = load_labels(labels_path)[undone]["label"]
                    append_label(labels_path, undone, None)
                    counts[previous] -= 1
                    queue.insert(i, undone)  # show it again, right now
                    status = f"↶ Removed label '{previous}'. Choose a new one."
                    chars = PREVIEW_CHARS
                elif key in ("q", ""):  # "" = end of input (e.g. piped/tests)
                    break
                elif key in ENTER:
                    status = "No suggestion to accept. Use the keys listed above."
                else:
                    status = f"Unknown key {key!r}. Use the keys listed above."
    finally:
        if suggester:
            suggester.close()

    console.print(
        f"Saved to {labels_path}. Labelled {sum(counts.values())} in total: "
        + ", ".join(f"{c} {n}" for c, n in counts.most_common() if n)
    )
    if suggested_total:
        console.print(
            f"Suggestions accepted with Enter: {accepted} of {suggested_total} "
            f"({accepted / suggested_total:.0%})"
        )


# --- Evaluation ------------------------------------------------------------

RUNS_DIR = Path("evals/runs")  # one JSON per eval run; git-ignored (personal counts)
RARE_TARGETS = (Category.INTERVIEW_INVITE.value, Category.ASSESSMENT.value)
RECALL_TARGET = 0.95


@eval_app.command("inbox")
def eval_inbox(
    classifier: Annotated[
        str, typer.Option(help="One of: always-other, keyword, llm.")
    ] = "keyword",
    split: Annotated[
        str, typer.Option(help="dev (default) or test. Use test once, at the end.")
    ] = "dev",
    labels_path: Annotated[Path, typer.Option("--labels")] = LABELS_PATH,
    show_errors: Annotated[
        bool,
        typer.Option(
            "--show-errors",
            help="After scoring, step through the emails it got wrong (dev only).",
        ),
    ] = False,
) -> None:
    """Score a classifier on your labels. Prints numbers only, never email text."""
    from jobagent.eval.inbox import CLASSIFIERS, disagreements, run_eval
    from jobagent.inbox.classify import PROMPT_VERSION

    if show_errors and split != "dev":
        # Studying test mistakes and then fixing them = tuning on test.
        console.print("[red]--show-errors only works on dev (test stays unseen).[/]")
        raise typer.Exit(code=2)
    from jobagent.eval.metrics import confusion_matrix, macro_f1, per_class

    choices = [*CLASSIFIERS, "llm"]
    if classifier not in choices:
        console.print(
            f"[red]Unknown classifier {classifier!r}.[/] Try: {', '.join(choices)}"
        )
        raise typer.Exit(code=2)
    if split not in ("dev", "test"):
        console.print("[red]--split must be dev or test.[/]")
        raise typer.Exit(code=2)
    if split == "test":
        console.print("[yellow]Test split: report this number, don't tune on it.[/]")

    settings = Settings.from_env()
    llm = None
    if classifier == "llm":
        import ollama

        from jobagent.inbox.classify import LLMClassifier

        llm = LLMClassifier(
            ollama.Client(host=settings.ollama_host), settings.inbox_model
        )
        classify = llm
        console.print(
            f"Model [cyan]{settings.inbox_model}[/] · prompt {PROMPT_VERSION} · "
            "running locally"
        )
    else:
        classify = CLASSIFIERS[classifier]

    conn = _open_db(settings)
    with Progress(console=console, transient=True) as progress:
        task = progress.add_task("Classifying", total=None)
        try:
            run = run_eval(
                conn,
                load_labels(labels_path),
                classify,
                split,
                on_progress=lambda d, t: progress.update(task, completed=d, total=t),
            )
        except ConnectionError:
            console.print(
                f"[red]Can't reach Ollama at {settings.ollama_host}.[/] "
                "Start it with `ollama serve`."
            )
            raise typer.Exit(code=1) from None
    if not run.y_true:
        console.print(
            f"No labelled emails in the {split} split yet. Run `jobagent label`."
        )
        raise typer.Exit(code=1)

    labels = [c.value for c in Category]
    report = per_class(run.y_true, run.y_pred, labels)

    table = Table(
        "class",
        "precision",
        "recall",
        "f1",
        "support",
        title=f"{classifier} on {split} ({len(run.y_true)} emails)",
    )
    for c in labels:
        r = report[c]
        table.add_row(
            c,
            f"{r['precision']:.2f}",
            f"{r['recall']:.2f}",
            f"{r['f1']:.2f}",
            str(r["support"]),
        )
    console.print(table)

    # The number that matters most: of the real interviews/assessments, how many caught?
    caught = sum(
        t == p for t, p in zip(run.y_true, run.y_pred, strict=True) if t in RARE_TARGETS
    )
    total = sum(t in RARE_TARGETS for t in run.y_true)
    rare_recall = caught / total if total else 0.0
    colour = "green" if rare_recall >= RECALL_TARGET else "red"
    console.print(
        f"macro F1 {macro_f1(report):.2f} · interview+assessment recall "
        f"[{colour}]{rare_recall:.2f}[/] ({caught}/{total}, target {RECALL_TARGET})"
    )

    matrix = confusion_matrix(run.y_true, run.y_pred, labels)
    short = {
        "offer": "off",
        "interview_invite": "int",
        "assessment": "ass",
        "rejection": "rej",
        "recruiter_message": "rec",
        "confirmation": "conf",
        "other": "oth",
    }
    cm = Table(
        "",
        *(short[c] for c in labels),
        title="Confusion matrix (rows = your label, columns = predicted)",
    )
    for t in labels:
        cm.add_row(short[t], *(str(matrix[t][p] or ".") for p in labels))
    console.print(cm)

    avg_ms = 1000 * sum(run.latencies_s) / len(run.latencies_s)
    note = f" · {run.missing} labelled emails missing from DB" if run.missing else ""
    console.print(f"[dim]avg latency {avg_ms:.2f} ms/email{note}[/]")
    if llm is not None:
        console.print(
            f"Invalid model output (counted as 'other'): {llm.errors} · "
            f"evidence quote not found in email: {llm.unsupported}"
        )

    from jobagent.eval.inbox import save_run

    saved = save_run(
        RUNS_DIR,
        {
            "classifier": classifier,
            "model": settings.inbox_model if llm else None,
            "prompt_version": PROMPT_VERSION if llm else None,
            "split": split,
            "n": len(run.y_true),
            "macro_f1": macro_f1(report),
            "rare_recall": {"caught": caught, "total": total, "value": rare_recall},
            "per_class": report,
            "confusion_matrix": matrix,
            "avg_latency_ms": avg_ms,
            "invalid_outputs": llm.errors if llm else None,
            "evidence_not_found": llm.unsupported if llm else None,
            "missing_from_db": run.missing,
        },
    )
    console.print(f"[dim]Run saved to {saved}[/]")

    if show_errors:
        _review_errors(conn, disagreements(run), labels_path, classifier)


def _review_errors(conn, errors, labels_path: Path, classifier: str) -> None:
    """Step through disagreements locally. Your label can be corrected here too:
    sometimes the model is right and the label was the mistake."""
    if not errors:
        console.print("No disagreements to review.")
        return
    console.print(f"[bold]{len(errors)} disagreements.[/] Press any key to start.")
    typer.getchar()

    help_line = (
        "[dim]Enter/n next · p previous · m more · "
        "a label key (o i a r t c x) corrects YOUR label · q quit[/]"
    )
    corrected = 0
    i, chars, status = 0, PREVIEW_CHARS, ""
    with console.screen():
        while 0 <= i < len(errors):
            mid, yours, predicted = errors[i]
            row = get_raw_email(conn, mid)
            body = clean_for_reading(row["body_text"]) or "(empty body)"
            shown = body[:chars] + (
                f"\n\n… {len(body) - chars} more (m)" if len(body) > chars else ""
            )
            console.clear()
            console.print(
                f"[dim]Disagreement {i + 1}/{len(errors)}[/]   "
                f"You: [bold green]{yours}[/]   {classifier}: [bold red]{predicted}[/]"
            )
            console.print(
                Panel(Text(shown), title=Text(row["subject"] or "(no subject)"))
            )
            console.print(help_line)
            if status:
                console.print(Text(status, style="bold yellow"))
            status = ""

            try:
                raw = typer.getchar()
            except (KeyboardInterrupt, EOFError):
                raw = "q"
            key = ARROW_KEYS.get(raw, raw.lower())
            if key in ("q", ""):
                break
            if key in (*ENTER, "n"):
                i, chars = i + 1, PREVIEW_CHARS
            elif key == "p":
                i, chars = max(i - 1, 0), PREVIEW_CHARS
            elif key == "m":
                chars *= 4
            elif key in LABEL_KEYS:
                new = LABEL_KEYS[key]
                append_label(labels_path, mid, new)
                errors[i] = (mid, new.value, predicted)
                corrected += 1
                status = f"Your label changed to {new.value}"
            else:
                status = f"Unknown key {key!r}"
    console.print(
        f"Reviewed {len(errors)} disagreements; corrected {corrected} of your labels."
    )


# --- Jobs (Phase 4) ---------------------------------------------------------

jobs_app = typer.Typer(help="Find and review jobs.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")


def _make_reed(settings: Settings):
    """Separate function so tests can swap in a fake client."""
    from jobagent.sources.reed import ReedClient

    if not settings.reed_api_key:
        console.print(
            "[red]No Reed API key.[/] Get a free one at reed.co.uk/developers and add "
            "REED_API_KEY=... to .env"
        )
        raise typer.Exit(2)
    return ReedClient(settings.reed_api_key)


def _make_boards():
    """The company-boards client. A separate function so tests can swap it."""
    from jobagent.sources.boards import BoardsClient

    return BoardsClient()


@jobs_app.command("fetch")
def jobs_fetch(
    days: Annotated[
        int, typer.Option(help="Reed: ignore jobs posted longer ago.")
    ] = 14,
    max_per_search: Annotated[
        int, typer.Option(help="Reed: results to read per search term.")
    ] = 200,
    source: Annotated[
        str, typer.Option(help="all, reed, or boards (company job boards).")
    ] = "all",
) -> None:
    """Search the job sources, filter, and store new matches."""
    import httpx

    from jobagent.sources.boards import load_companies
    from jobagent.sources.feed import fetch_boards, fetch_jobs

    if source not in {"all", "reed", "boards"}:
        console.print("--source must be all, reed or boards.")
        raise typer.Exit(2)
    settings = Settings.from_env()
    conn = _open_db(settings)
    today = datetime.now(UTC).date()
    new = 0

    if source in {"all", "reed"}:
        reed = _make_reed(settings)
        try:
            stats = fetch_jobs(conn, reed.search, today, max_per_search, days)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            hint = " (check REED_API_KEY in .env)" if code in (401, 403) else ""
            console.print(f"[red]Reed returned HTTP {code}{hint}.[/]")
            raise typer.Exit(1) from e
        except httpx.TransportError as e:
            console.print(f"[red]Couldn't reach Reed:[/] {e}")
            raise typer.Exit(1) from e
        new += stats.new
        console.print(
            f"Reed: {stats.seen} results, {stats.unique} distinct jobs, "
            f"[green]{stats.kept} match[/] ({stats.new} new)."
        )
        for reason, n in stats.dropped.most_common():
            console.print(f"  dropped {n}: {reason}")

    if source in {"all", "boards"}:
        companies = load_companies()
        with console.status(f"Reading {len(companies)} company job boards..."):
            boards = fetch_boards(conn, _make_boards(), companies, today)
        new += boards.new
        console.print(
            f"Company boards: {boards.companies} companies, {boards.unique} open jobs, "
            f"[green]{boards.kept} match[/] ({boards.new} new)."
        )
        for reason, n in boards.dropped.most_common():
            console.print(f"  dropped {n}: {reason}")
        if boards.failed:
            console.print(f"[yellow]  Couldn't read {len(boards.failed)} board(s):[/]")
            for line in boards.failed:
                console.print(f"    {line}", markup=False, style="yellow")

    console.print(
        f"[dim]{new} new job(s). Next: uv run jobagent jobs score · jobs list[/]"
    )


@jobs_app.command("list")
def jobs_list(
    limit: Annotated[int, typer.Option(help="How many to show.")] = 30,
    accepted: Annotated[
        bool, typer.Option("--accepted", help="Only jobs you accepted in review.")
    ] = False,
    rejected: Annotated[
        bool, typer.Option("--rejected", help="Include jobs you rejected.")
    ] = False,
) -> None:
    """Stored jobs, best fit first. Rejected jobs are hidden; ✓ marks accepted ones."""
    from jobagent.core.store import list_jobs
    from jobagent.match.filters import Region

    conn = _open_db(Settings.from_env())
    rows = list_jobs(
        conn, limit=10_000 if accepted else limit, include_rejected=rejected
    )
    if accepted:
        rows = [r for r in rows if r["decision"] == "accepted"][:limit]
    if not rows:
        console.print("No jobs yet. Run: uv run jobagent jobs fetch")
        return
    table = Table("ID", "Fit", "Title", "Company", "Where", "Posted", "Salary")
    for r in rows:
        posted = r["posted_at"]
        mark = {"accepted": " ✓", "rejected": " ✗"}.get(r["decision"], "")
        table.add_row(
            str(r["id"]) + mark,
            _fit_cell(r["score"]),
            Text(r["title"]),  # Text(): job ads are untrusted, never parsed as markup
            Text(r["company"]),
            Text(f"{r['location']} ({Region(r['region']).name.title()})"),
            datetime.fromisoformat(posted).strftime("%d %b") if posted else "",
            _short_salary(r["salary_min"], r["salary_max"]),
        )
    console.print(table)
    console.print("[dim]Tip: widen the terminal to see full titles.[/]")


@jobs_app.command("clear")
def jobs_clear(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation.")] = False,
) -> None:
    """Delete all stored jobs, e.g. after the filters change. Re-fetch afterwards."""
    from jobagent.core.store import clear_jobs

    if not yes and not typer.confirm("Delete all stored jobs?"):
        raise typer.Exit(1)
    conn = _open_db(Settings.from_env())
    console.print(f"Deleted {clear_jobs(conn)} jobs.")


def _fit_cell(score: int | None) -> Text:
    if score is None:
        return Text("-", style="dim")
    colour = "green" if score >= 70 else "yellow" if score >= 50 else "red"
    return Text(str(score), style=colour)


def _make_scorer(settings: Settings):
    """The local model's fit scorer. A separate function so tests can swap it."""
    import ollama

    from jobagent.match.score import MODEL_TIMEOUT, FitScorer

    client = ollama.Client(host=settings.ollama_host, timeout=MODEL_TIMEOUT)
    return FitScorer(client, settings.inbox_model)


@jobs_app.command("score")
def jobs_score(
    limit: Annotated[
        int | None, typer.Option(help="Score at most this many (try 3 first).")
    ] = None,
    rescore: Annotated[
        bool, typer.Option(help="Score again jobs that already have a score.")
    ] = False,
) -> None:
    """Compare each job with your CV using the local model, and store a fit score."""
    import json
    from dataclasses import asdict

    import httpx

    from jobagent.core.store import jobs_to_score, save_full_description, save_score
    from jobagent.match.cv import cv_for, load_cv
    from jobagent.match.score import PROMPT_VERSION

    settings = Settings.from_env()
    conn = _open_db(settings)
    rows = jobs_to_score(conn, rescore=rescore, limit=limit)
    if not rows:
        console.print("Nothing to score. Fetch jobs first, or use --rescore.")
        return
    reed = _make_reed(settings)
    scorer = _make_scorer(settings)
    console.print(
        f"Model {settings.inbox_model} · prompt {PROMPT_VERSION} · running locally"
    )

    verdicts: Counter = Counter()
    failures: list[str] = []  # "#ID title: reason", listed at the end
    snippet_only = unverified_job = unverified_cv = not_proven = second_look = 0
    verifier_failed = 0
    with Progress(console=console) as progress:
        task = progress.add_task("Scoring", total=len(rows))
        for row in rows:
            description = row["full_description"]
            if description is None and row["source"] != "reed":
                description = row["description"]  # boards give the whole ad up front
            if description is None:
                try:
                    description = reed.full_description(row["external_id"])
                    save_full_description(conn, row["id"], description)
                except httpx.HTTPError:
                    description = row["description"]  # the snippet: better than nothing
                    snippet_only += 1
            cv_key = cv_for(row["title"])
            try:
                fit = scorer.score(
                    row["title"], row["company"], description, load_cv(cv_key)
                )
            except ConnectionError:
                console.print(
                    f"[red]Can't reach Ollama at {settings.ollama_host}.[/] "
                    "Start it with `ollama serve`."
                )
                raise typer.Exit(1) from None
            if fit is None:  # named and reported, never a made-up score
                reason = getattr(scorer, "last_failure", "") or "no valid answer"
                failures.append(f"#{row['id']} {row['title']}: {reason}")
            else:
                save_score(
                    conn,
                    row["id"],
                    score=fit.score,
                    verdict=fit.verdict,
                    cv=cv_key,
                    assessment_json=json.dumps(asdict(fit)),
                    unverified=fit.unverified_job_quotes + fit.unverified_cv_quotes,
                    model=settings.inbox_model,
                    prompt_version=PROMPT_VERSION,
                )
                verdicts[fit.verdict] += 1
                unverified_job += fit.unverified_job_quotes
                unverified_cv += fit.unverified_cv_quotes
                not_proven += fit.not_proven
                second_look += sum(r.second_look for r in fit.requirements)
                verifier_failed += fit.verifier_failed
            progress.advance(task)

    scored = sum(verdicts.values())
    console.print(f"Scored {scored} of {len(rows)} jobs.")
    for verdict in ("strong", "worth applying", "stretch", "poor fit"):
        if verdicts[verdict]:
            console.print(f"  {verdict}: {verdicts[verdict]}")
    if failures:
        console.print(
            f"[yellow]  Not scored this run ({len(failures)}); any old score is kept:[/]"
        )
        for line in failures:  # markup off: job titles can contain [brackets]
            console.print(f"    {line}", markup=False, style="yellow")
    if snippet_only:
        console.print(
            f"[yellow]  Scored on the snippet only (full ad unavailable): {snippet_only}[/]"
        )
    console.print(
        f"[dim]  Requirement quotes not found in the ad: {unverified_job} · "
        f"'met' claims rejected (CV quote not in your CV): {unverified_cv} · "
        f"(CV line doesn't prove it): {not_proven} · "
        f"proof found on a second look: {second_look}[/]"
    )
    if verifier_failed:
        console.print(
            f"[yellow]  Second check gave no valid answer for {verifier_failed} job(s); "
            "their 'met' claims weren't double-checked.[/]"
        )
    console.print(
        "[dim]Best first: uv run jobagent jobs list · details: jobs show ID[/]"
    )


@jobs_app.command("recount")
def jobs_recount() -> None:
    """Re-apply the current scoring rules to saved judgements: seconds, no model calls."""
    import json
    from dataclasses import asdict

    from jobagent.core.store import save_score, scored_jobs
    from jobagent.match.score import PROMPT_VERSION, recount

    conn = _open_db(Settings.from_env())
    changed = skipped = 0
    for row in scored_jobs(conn):
        if row["prompt_version"] != PROMPT_VERSION:
            skipped += (
                1  # older judgements were made differently: rescore those instead
            )
            continue
        description = row["full_description"] or row["description"]
        fit = recount(json.loads(row["assessment_json"]), description, row["title"])
        if fit.score != row["score"]:
            console.print(
                f"  {row['id']:>3}  {row['score']:>3} → {fit.score:<3} {row['title']}"
            )
            changed += 1
        save_score(
            conn,
            row["id"],
            score=fit.score,
            verdict=fit.verdict,
            cv=row["cv"],
            assessment_json=json.dumps(asdict(fit)),
            unverified=row["unverified"],
            model=row["model"],
            prompt_version=PROMPT_VERSION,
        )
    console.print(f"Recounted with current rules: {changed} score(s) changed.")
    if skipped:
        console.print(
            f"[yellow]  {skipped} scored with an older prompt: "
            "run `jobs score --rescore` for those.[/]"
        )


@jobs_app.command("review")
def jobs_review(
    min_score: Annotated[
        int, typer.Option(help="Only jobs scoring at least this.")
    ] = 0,
    limit: Annotated[int | None, typer.Option(help="Stop after this many.")] = None,
) -> None:
    """Go through scored jobs, best first: accept, reject or skip each one.

    Every decision is saved. Accepted jobs are your shortlist to apply to; all decisions
    become labels for checking the scorer (do high scores match what you accept?).
    """
    from jobagent.core.store import jobs_to_review, save_decision

    conn = _open_db(Settings.from_env())
    rows = jobs_to_review(conn, min_score=min_score, limit=limit)
    if not rows:
        console.print("Nothing to review. Score jobs first: uv run jobagent jobs score")
        return
    console.print(
        f"{len(rows)} job(s) to review, best first.\n"
        "[bold]a[/] accept · [bold]r[/] reject · [bold]s[/] skip (decide later) · "
        "[bold]o[/] open the ad in your browser · [bold]q[/] quit (progress is saved)"
    )
    done: Counter = Counter()
    for i, r in enumerate(rows, start=1):
        console.rule(f"{i} of {len(rows)}")
        _print_job(r)
        while True:
            choice = typer.prompt("Decision [a/r/s/o/q]", default="s").strip().lower()
            if choice == "o":
                typer.launch(r["url"])  # then ask again
            elif choice in {"a", "r", "s", "q"}:
                break
            else:
                console.print("Type a, r, s, o or q.")
        if choice == "q":
            break
        if choice == "s":
            done["skipped"] += 1
            continue
        decision = "accepted" if choice == "a" else "rejected"
        reason = typer.prompt(
            "Why? (optional, Enter to skip)", default="", show_default=False
        )
        save_decision(
            conn, r, decision, reason
        )  # saved at once: quitting loses nothing
        done[decision] += 1

    console.print(
        f"Accepted {done['accepted']} · rejected {done['rejected']} · "
        f"skipped {done['skipped']}."
    )
    if done["accepted"]:
        console.print("[dim]Your shortlist: uv run jobagent jobs list --accepted[/]")


@jobs_app.command("show")
def jobs_show(
    job_id: Annotated[int, typer.Argument(help="Job ID from jobs list.")],
) -> None:
    """Everything about one job: the fit assessment and a link to apply."""

    from jobagent.core.store import get_job

    conn = _open_db(Settings.from_env())
    try:
        r = get_job(conn, job_id)
    except KeyError:
        console.print(f"No job with ID {job_id}.")
        raise typer.Exit(1) from None
    _print_job(r)


def _print_job(r) -> None:
    """The full fit assessment for one job row (from get_job or jobs_to_review)."""
    import json

    console.print(
        Text(f"{r['title']} · {r['company']} · {r['location']}", style="bold")
    )
    console.print(Text(r["url"], style="cyan"))
    if r["score"] is None:
        console.print("Not scored yet. Run: uv run jobagent jobs score")
        return
    a = json.loads(r["assessment_json"])
    if "requirements" not in a:  # scored before prompt v3 changed the format
        console.print(
            f"Scored with an older prompt ({r['prompt_version']}). "
            "Run: uv run jobagent jobs score --rescore"
        )
        return
    console.print(
        Text.assemble(
            "Fit ", _fit_cell(r["score"]), f" · {r['verdict']} · CV: {r['cv']}"
        )
    )
    console.print(Text(a["summary"]))
    if a["dealbreakers"]:
        console.print("[red bold]Dealbreakers[/]")
        for d in a["dealbreakers"]:
            console.print(Text(f"  ✗ {d}"))
    if a["concerns"]:
        console.print("[yellow bold]Check before applying[/]")
        for c in a["concerns"]:
            console.print(Text(f"  ! {c}"))

    reqs = a["requirements"]
    met = [q for q in reqs if q["met"]]
    missing = [q for q in reqs if not q["met"]]
    console.print(f"[green bold]You meet ({len(met)})[/]")
    for q in met:
        found = "  (found on a second look)" if q.get("second_look") else ""
        console.print(Text(f"  ✓ {q['requirement']}{found}"))
        console.print(Text(f'      your CV: "{q["cv_quote"]}"', style="dim"))
    console.print(f"[yellow bold]Missing ({len(missing)})[/]")
    for q in missing:
        tag = " (essential)" if q["essential"] else " (nice to have)"
        note = ""
        if q["claimed_but_unverified"]:
            note = "  [claimed, but no matching line in your CV]"
        elif q.get("not_proven"):  # .get: scores saved before v4 lack the field
            note = "  [checker: the CV line it cited doesn't show this]"
        console.print(Text(f"  · {q['requirement']}{tag}{note}"))
        if q.get("not_proven"):
            console.print(Text(f'      your CV: "{q["cv_quote"]}"', style="dim"))
    if not reqs:
        console.print("[dim]  The model listed no requirements.[/]")


def _short_salary(low: float | None, high: float | None) -> str:
    """£60k–80k for yearly pay; £500–600/day for contract day rates."""
    if not low:
        return ""
    high = high or low
    if high < 2000:  # nobody's yearly salary is under £2,000: it's a day rate
        return f"£{low:,.0f}–{high:,.0f}/day"
    return f"£{low / 1000:.0f}k–{high / 1000:.0f}k"


if __name__ == "__main__":
    app()


# ---- Phase 5: apply ---------------------------------------------------------------------

apply_app = typer.Typer(
    help="Prepare applications, approve them, send them.", no_args_is_help=True
)
app.add_typer(apply_app, name="apply")


def _make_letter_writer(settings: Settings):
    """The local model's cover-letter writer. A separate function so tests can swap it."""
    import ollama

    from jobagent.apply.letter import LetterWriter
    from jobagent.match.score import MODEL_TIMEOUT

    client = ollama.Client(host=settings.ollama_host, timeout=MODEL_TIMEOUT)
    return LetterWriter(client, settings.inbox_model)


def _load_answers_or_exit():
    from jobagent.apply.answers import AnswersError, load_answers

    try:
        return load_answers()
    except AnswersError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None


@apply_app.command("answers")
def apply_answers() -> None:
    """Show your approved screening answers (the only answers the applier will use)."""
    from jobagent.apply.answers import QUESTIONS

    answers = _load_answers_or_exit()
    table = Table(show_lines=True)
    table.add_column("Question")
    table.add_column("Your answer")
    for key, question in QUESTIONS.items():
        value = answers.get(key)
        table.add_row(question, value or Text("comes to you each time", style="yellow"))
    console.print(table)


@apply_app.command("prepare")
def apply_prepare(
    limit: Annotated[
        int | None, typer.Option(help="Prepare at most this many (try 3 first).")
    ] = None,
    redo: Annotated[
        bool, typer.Option(help="Redraft letters not yet sent (sent ones are kept).")
    ] = False,
    redo_flagged: Annotated[
        bool,
        typer.Option(help="Redraft only unsent letters the checks flagged."),
    ] = False,
) -> None:
    """Draft a cover letter for every tier A (70+) and B (50-69) job. Nothing is sent."""
    import json

    from jobagent.apply.letter import WORD_LIMITS, tier_for
    from jobagent.core.store import delete_prepared_packs, jobs_to_prepare, save_pack
    from jobagent.match.cv import load_cv

    settings = Settings.from_env()
    conn = _open_db(settings)
    if redo or redo_flagged:
        n = delete_prepared_packs(conn, flagged_only=redo_flagged and not redo)
        console.print(f"Redrafting {n} unsent letter(s).")
    skipped: Counter = Counter()
    todo = []
    for row in jobs_to_prepare(conn):
        dealbreakers = json.loads(row["assessment_json"])["dealbreakers"]
        tier = tier_for(row["score"], dealbreakers, row["decision"])
        if tier:
            todo.append((row, tier))
        elif row["decision"] == "rejected":
            skipped["you rejected it"] += 1
        elif dealbreakers:
            skipped["a dealbreaker (e.g. US-based, clearance)"] += 1
    todo = todo[:limit] if limit else todo
    if not todo:
        console.print("Nothing to prepare: every eligible job already has a letter.")
        return

    writer = _make_letter_writer(settings)
    console.print(
        f"Writing {len(todo)} cover letter(s) with {settings.inbox_model}, locally."
    )
    made: Counter = Counter()
    flagged: list[str] = []
    with Progress(console=console) as progress:
        task = progress.add_task("Letters", total=len(todo))
        for row, tier in todo:
            ad = row["full_description"] or row["description"]
            letter = writer.write(
                row["title"], row["company"], ad, load_cv(row["cv"]), tier
            )
            save_pack(
                conn,
                row,
                tier=tier,
                cv=row["cv"],
                letter=letter.text,
                problems=letter.problems,
                warnings=letter.warnings,
                model=settings.inbox_model,
            )
            made[tier] += 1
            if letter.problems:
                flagged.append(f"#{row['id']} {row['title']}: {letter.problems[0]}")
            progress.advance(task)

    console.print(
        f"Prepared {sum(made.values())}: tier A {made['A']} "
        f"({WORD_LIMITS['A']} words max), tier B {made['B']} ({WORD_LIMITS['B']})."
    )
    for reason, n in skipped.items():
        console.print(f"[dim]  Not prepared, {reason}: {n}[/]")
    if flagged:
        console.print(f"[yellow]  Letters that need your edit ({len(flagged)}):[/]")
        for line in flagged:
            console.print(f"    {line}", markup=False, style="yellow")
    console.print("[dim]Next: uv run jobagent apply review[/]")


def _copy_to_clipboard(text: str) -> bool:
    """macOS clipboard. False if unavailable (the letter is on screen anyway)."""
    import subprocess

    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def _edit_text(text: str) -> str | None:
    """Open text in your editor ($EDITOR, else nano); return the saved text, or None."""
    import os
    import shlex
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(text)
        path = Path(f.name)
    try:
        editor = shlex.split(os.environ.get("EDITOR") or "nano")
        subprocess.run([*editor, str(path)], check=True)
        return path.read_text()
    except (OSError, subprocess.CalledProcessError) as e:
        console.print(f"[red]Couldn't open an editor: {e}[/]")
        return None
    finally:
        path.unlink(missing_ok=True)  # the letter is saved in the database, not here


def _print_pack(p, problems: list[str], warnings: list[str]) -> None:
    from jobagent.match.cv import CV_FILES

    console.print(
        Text(f"{p['title']} · {p['company']} · {p['location']}", style="bold")
    )
    console.print(f"Tier {p['tier']} · fit {p['score']} · {p['url']}")
    console.print(f"Attach CV: [cyan]{CV_FILES[p['cv']]}[/]")
    console.print(Panel(Text(p["letter"] or "(no letter)"), title="Cover letter"))
    for problem in problems:
        console.print(Text(f"  ✗ {problem}", style="red"))
    for warning in warnings:
        console.print(Text(f"  ! {warning}", style="yellow"))


def _fetch_form_html(url: str) -> str:
    """The job's public application page. A separate function so tests can swap it."""
    import httpx

    from jobagent.sources.base import USER_AGENT

    response = httpx.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
    )
    response.raise_for_status()
    return response.text


_SEARCH_OVER = (
    15  # more options than this (e.g. 3,000 universities): search, don't list
)


def _ask_question(q) -> str | None:
    """Ask one form question in the terminal. Empty input skips it (you'll fill it on
    the page). Choices are picked by number, so the answer is always a real option."""
    from jobagent.apply.fill import MULTI_SEPARATOR

    console.print(Text(f"\n{q.label}", style="bold"))
    if not q.options:
        if q.kind == "textarea":
            console.print(
                "[dim]Your editor opens for a longer answer. Save and close.[/]"
            )
            text = _edit_text("")
            return text.strip() if text and text.strip() else None
        text = typer.prompt("Answer (Enter to skip)", default="", show_default=False)
        return text.strip() or None

    options = list(q.options)
    while True:
        shown = options
        if len(options) > _SEARCH_OVER:
            term = typer.prompt(
                f"{len(options)} options: type part of yours (Enter to skip)",
                default="",
                show_default=False,
            ).strip()
            if not term:
                return None
            shown = [o for o in options if term.lower() in o.lower()][:_SEARCH_OVER]
            if not shown:
                console.print("No match. Try fewer letters.")
                continue
        for n, option in enumerate(shown, start=1):
            console.print(f"  {n}. {option}", markup=False)
        many = q.kind == "multiple-select"
        hint = "numbers, comma-separated" if many else "number"
        raw = typer.prompt(
            f"Pick ({hint}; Enter to skip)", default="", show_default=False
        )
        if not raw.strip():
            return None
        try:
            picks = [shown[int(x) - 1] for x in raw.split(",") if x.strip()]
        except (ValueError, IndexError):
            console.print(f"Type {hint} from the list.")
            continue
        if picks and (many or len(picks) == 1):
            return MULTI_SEPARATOR.join(picks)
        console.print("Pick one number.")


def _fetch_form_json(url: str) -> dict:
    """A job's questions from a public ATS API. A separate function so tests can swap it."""
    import httpx

    from jobagent.sources.base import USER_AGENT

    response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return response.json()


def _read_form(p):
    """(questions, the form's page) for a job whose form can be read, else None."""
    from jobagent.apply.forms import (
        greenhouse_urls,
        lever_apply_url,
        parse_greenhouse_questions,
        parse_lever_form,
    )

    if p["source"] == "lever":
        url = lever_apply_url(p["url"])
        return parse_lever_form(_fetch_form_html(url)), url
    if p["source"] == "greenhouse":
        api, url = greenhouse_urls(p["external_id"])
        return parse_greenhouse_questions(_fetch_form_json(api)), url
    return None


def _complete_form(conn, p, letter: str, answers):
    """For a job with a readable form (Lever, Greenhouse): fill what's known, ask you the
    rest, save your answers for next time. Returns (questions, {field: answer}, form
    page), or None if the form can't be read (then you fill it on the page, as before)."""
    from jobagent.apply.fill import LETTER_FIELDS, plan_form, question_key
    from jobagent.apply.profile import ProfileError, load_profile
    from jobagent.core.store import save_answer, saved_answers

    if p["source"] not in ("lever", "greenhouse"):
        return None
    try:
        profile = load_profile()
    except ProfileError as e:
        console.print(Text(str(e), style="yellow"))
        return None
    try:
        questions, form_url = _read_form(p)
    except Exception as e:  # noqa: BLE001 (any failure: fall back to the page)
        console.print(f"[yellow]Couldn't read the form ({e}); fill it on the page.[/]")
        return None

    plan = plan_form(questions, profile, answers, saved_answers(conn), letter)
    console.print(
        f"[green]Form: {sum(f not in LETTER_FIELDS for f in plan.filled)} answered from "
        "your profile, answers and earlier forms[/] (plus the letter) · "
        f"[yellow]{len(plan.needs_you)} need you[/]"
    )
    for q in plan.needs_you:
        answer = _ask_question(q)
        if answer is None:
            console.print("[dim]Skipped: fill it on the page.[/]")
            continue
        save_answer(conn, question_key(q.label), q.label, answer)
        plan.filled[q.field] = answer
    return questions, plan.filled, form_url


def _print_form_answers(questions, by_field: dict[str, str], letter: str) -> None:
    """Everything to copy by hand, in page order (when the browser can't fill it)."""
    console.print("\n[bold]Answers for this form[/] (the letter goes in the last box):")
    for q in questions:
        answer = by_field.get(q.field)
        if answer and answer != letter:
            console.print(Text(f"  {q.label}: ", style="dim") + Text(answer))


def _fill_in_browser(
    p, questions, by_field: dict[str, str], form_url: str, letter: str
) -> bool:
    """Open the form in Chrome, fill it, and ask (while it's still open) whether you
    submitted. If the browser can't start, you get the answers to copy instead."""
    from jobagent.apply.browser import open_and_fill
    from jobagent.match.cv import CV_FILES, DEFAULT_CV_DIR

    cv_path = DEFAULT_CV_DIR / CV_FILES[p["cv"]]

    def hand_over(result) -> bool:
        console.print(f"[green]Filled {len(result.filled)} field(s) in the browser.[/]")
        for failed in result.failed:
            console.print(Text(f"  ✗ fill this yourself: {failed}", style="yellow"))
        console.print(
            "[bold]Check every field in the browser, then click Submit yourself.[/]"
        )
        return typer.confirm("Did you submit the application?", default=False)

    try:
        return open_and_fill(
            form_url, questions, by_field, cv_path, hand_over, ats=p["source"]
        )
    except Exception as e:  # noqa: BLE001 (no browser: fall back to copying by hand)
        console.print(f"[yellow]Couldn't open the browser ({e}).[/]")
        _print_form_answers(questions, by_field, letter)
        _copy_to_clipboard(letter)
        typer.launch(form_url)
        return typer.confirm("Did you submit the application?", default=False)


@apply_app.command("recheck")
def apply_recheck() -> None:
    """Re-run the code checks on every unsent letter (seconds, no model). Use after a
    check is added; then `apply prepare --redo-flagged` redrafts what it caught."""
    import json

    from jobagent.apply.letter import WORD_LIMITS, check_letter
    from jobagent.core.store import packs_to_review, update_pack_letter
    from jobagent.match.cv import load_cv

    conn = _open_db(Settings.from_env())
    # Kept from the model's sentence check: it isn't re-run here.
    model_problem, model_warning = "not backed by your CV", ("removed,", "the sentence")
    newly = []
    for p in packs_to_review(conn):
        before = json.loads(p["problems_json"])
        problems, warnings = check_letter(
            p["letter"],
            load_cv(p["cv"]),
            p["ad"] or "",
            p["title"],
            p["company"],
            WORD_LIMITS[p["tier"]],
        )
        problems += [x for x in before if x.startswith(model_problem)]
        warnings += [
            w for w in json.loads(p["warnings_json"]) if w.startswith(model_warning)
        ]
        update_pack_letter(conn, p, p["letter"], problems, warnings)
        if problems and not before:
            newly.append(f"#{p['job_id']} {p['title']}: {problems[0]}")
    console.print(f"Rechecked. Newly flagged: {len(newly)}")
    for line in newly:
        console.print(Text(f"  {line}"))
    if newly:
        console.print("Next: uv run jobagent apply prepare --redo-flagged")


@apply_app.command("review")
def apply_review() -> None:
    """Approve each application, then send it yourself. Nothing is sent without you.

    a: approve (copies the letter, opens the application page; you submit, then confirm)
    e: edit the letter · s: skip for now · r: reject (never apply) · o: open the ad · q: quit
    """
    import json

    from jobagent.apply.answers import QUESTIONS
    from jobagent.apply.letter import WORD_LIMITS, check_letter
    from jobagent.core.store import (
        get_job,
        mark_sent,
        packs_to_review,
        reject_pack,
        save_decision,
        update_pack_letter,
    )
    from jobagent.match.cv import CV_FILES, DEFAULT_CV_DIR, load_cv

    answers = _load_answers_or_exit()
    conn = _open_db(Settings.from_env())
    packs = packs_to_review(conn)
    if not packs:
        console.print("Nothing to review. Run: uv run jobagent apply prepare")
        return
    console.print(
        f"{len(packs)} application(s) ready, tier A first.\n"
        "[bold]a[/] approve & send · [bold]e[/] edit letter · [bold]s[/] skip · "
        "[bold]r[/] reject · [bold]o[/] open ad · [bold]q[/] quit"
    )
    console.print(f"[dim]CVs are in {DEFAULT_CV_DIR}[/]")
    console.print("[dim]Screening answers (copy from here):[/]")
    for key, question in QUESTIONS.items():
        value = answers.get(key) or "[comes to you: answer it yourself]"
        console.print(f"  {question}: {value}", markup=False, style="dim")

    done: Counter = Counter()
    for i, p in enumerate(packs, start=1):
        console.rule(f"{i} of {len(packs)}")
        letter = p["letter"]
        problems = json.loads(p["problems_json"])
        warnings = json.loads(p["warnings_json"])
        _print_pack(p, problems, warnings)
        while True:
            choice = typer.prompt("Decision [a/e/s/r/o/q]", default="s").strip().lower()
            if choice == "o":
                typer.launch(p["url"])
            elif choice == "e":
                edited = _edit_text(letter)
                if edited is not None and edited.strip() != letter.strip():
                    letter = edited.strip()
                    ad = p["ad"] or ""
                    problems, warnings = check_letter(
                        letter,
                        load_cv(p["cv"]),
                        ad,
                        p["title"],
                        p["company"],
                        WORD_LIMITS[p["tier"]],
                    )
                    update_pack_letter(conn, p, letter, problems, warnings)
                    p = {**dict(p), "letter": letter}
                    _print_pack(p, problems, warnings)
            elif (
                choice == "a"
                and problems
                and not typer.confirm(
                    "The checker found problems (above). Send it anyway?", default=False
                )
            ):
                console.print("[dim]Press e to edit the letter.[/]")
            elif choice in {"a", "s", "r", "q"}:
                break
            else:
                console.print("Type a, e, s, r, o or q.")
        if choice == "q":
            break
        if choice == "s":
            done["skipped"] += 1
            continue
        if choice == "r":
            reject_pack(conn, p)
            save_decision(
                conn, get_job(conn, p["job_id"]), "rejected", "rejected at apply"
            )
            done["rejected"] += 1
            continue
        form = _complete_form(conn, p, letter, answers)
        if form is not None:
            questions, by_field, form_url = form
            submitted = _fill_in_browser(p, questions, by_field, form_url, letter)
        else:
            copied = _copy_to_clipboard(letter)
            console.print(
                "Letter copied to the clipboard. "
                if copied
                else "Copy the letter above. "
            )
            typer.launch(p["url"])
            submitted = typer.confirm("Did you submit the application?", default=False)
        if submitted:
            # The record of what was sent: the whole form if it was read, else the
            # screening answers you'd have copied from the list above.
            if form is not None:
                sent_answers = {
                    q.label: by_field[q.field] for q in questions if q.field in by_field
                }
            else:
                sent_answers = {k: answers.values[k] for k in QUESTIONS}
            mark_sent(
                conn,
                p,
                letter=letter,
                answers=sent_answers,
                cv_file=CV_FILES[p["cv"]],
            )
            done["sent"] += 1
        else:
            console.print("[dim]Kept for later.[/]")
            done["skipped"] += 1

    console.print(
        f"Sent {done['sent']} · rejected {done['rejected']} · skipped {done['skipped']}."
    )
