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


if __name__ == "__main__":
    app()
