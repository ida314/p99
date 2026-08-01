"""Command line entry point.

Bare `<command>` launches the TUI. The subcommands are the things you want without
starting a run: your numbers, your history, and the maintenance operations the
event log makes possible.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from . import (
    branding,
    catalog,
    config as config_module,
    db,
    events,
    paths,
    queues,
    render,
    scoring,
    srs,
    stats,
)

console = Console()


def _open() -> sqlite3.Connection:
    conn = db.open_db()
    if catalog.count(conn) == 0:
        catalog.seed(conn)
    return conn


# --- commands --------------------------------------------------------------


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui.app import run

    run()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = _open()
    cfg = config_module.load(conn)
    weights = scoring.load_weights(cfg.scoring.weights)
    days = None if args.days == 0 else (args.days or cfg.stats.window_days)
    min_samples = args.min_samples or cfg.stats.min_samples

    label = args.pattern or args.tag or args.difficulty or "all problems"
    overall = stats.distribution(
        conn,
        label=label.replace("-", " ").upper(),
        tag=args.tag,
        pattern=args.pattern,
        difficulty=args.difficulty,
        days=days,
        min_samples=min_samples,
        weights=weights,
    )
    if overall.n == 0 and not args.by:
        console.print(
            render.empty_state(
                "No finished attempts in this window.",
                f"Percentiles need attempts. Run `{branding.COMMAND}` and log some,"
                " or widen --days.",
            )
        )
        return 0

    console.print()
    console.print(render.distribution_panel(overall))

    if args.by:
        for dist in stats.distributions_by(
            conn, args.by, days=days, min_samples=min_samples, weights=weights, limit=args.limit
        ):
            if dist.n:
                console.print()
                console.print(render.distribution_panel(dist))
    console.print()
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    conn = _open()
    cfg = config_module.load(conn)
    weights = scoring.load_weights(cfg.scoring.weights)
    runs = stats.load_runs(conn, weights=weights)
    console.print()
    console.print(render.history_table(runs, limit=args.limit))

    if args.run is not None and runs:
        index = args.run - 1
        if 0 <= index < len(runs):
            run = runs[index]
            console.print()
            console.print(render.death_screen(run, stats.standing(runs, run.session_id), runs))
            console.print()
            for attempt in run.attempts:
                if not attempt.get("ended_at"):
                    continue
                score = scoring.score_attempt(attempt, attempt["difficulty"], weights)
                console.print(
                    render.stat_line(
                        attempt["title"],
                        attempt["difficulty"],
                        score,
                        attempt.get("self_confidence"),
                    )
                )
                console.print()
        else:
            console.print(f"[red]no run #{args.run}[/red] — there are {len(runs)}")
    console.print()
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Rebuild every projection from the log.

    This is the payoff of the append-only design: fix a bug in the projection
    logic, replay, and all history is corrected retroactively.
    """
    conn = db.open_db()
    n = events.replay(conn)
    sessions = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    attempts = conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"]
    cards = conn.execute("SELECT COUNT(*) AS n FROM fsrs_cards").fetchone()["n"]
    console.print(
        f"replayed [bold]{n}[/bold] events → {sessions} sessions, "
        f"{attempts} attempts, {cards} cards"
    )
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    """Today's queue (spec §10). Generated on demand until Phase 3's cron."""
    conn = db.open_db()
    cfg = config_module.load(conn)
    weights = scoring.load_weights(cfg.scoring.weights)
    queue = queues.ensure(
        conn,
        n=args.n or cfg.session.planned_n,
        active_list=cfg.session.active_list,
        weights=weights,
        regenerate=args.regenerate,
    )
    console.print()
    console.print(f"  [bold]today's queue[/bold]  ·  {queue.date}")
    console.print(render.queue_panel(queue))
    console.print()
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    conn = db.open_db()
    source = Path(args.file).expanduser() if args.file else None
    n = catalog.seed(conn, source=source, name=args.list)
    total = catalog.count(conn)
    console.print(f"seeded [bold]{n}[/bold] problems from {args.file or args.list} → {total} in catalog")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import capture

    conn = db.open_db()
    cfg = config_module.load(conn)
    ov = stats.overview(conn)
    editor = " ".join(config_module.editor())

    now = datetime.now(timezone.utc)
    card_count, due_now = srs.counts(conn, now)
    if card_count:
        nxt = srs.next_due(conn)
        cards_state = f"{card_count} scheduled, {due_now} due now"
        if nxt:
            cards_state += f", next {nxt[:10]}"
    else:
        cards_state = "none yet — finish a problem and one appears"

    changed = config_module.overrides(conn)
    capture_state = "on" if cfg.capture.enabled else "off"
    if cfg.capture.enabled:
        capture_state += f", wrong answers {'on' if cfg.capture.on_failed_submit else 'off'}"

    rows = [
        ("config", str(paths.config_file()), paths.config_file().exists()),
        ("database", str(paths.db_file()), paths.db_file().exists()),
        ("code archive", str(paths.code_dir()), paths.code_dir().exists()),
        ("notes", str(paths.notes_dir()), paths.notes_dir().exists()),
        ("editor", editor, capture.editor_available()),
        ("capture", capture_state, cfg.capture.enabled),
        (
            "settings",
            f"{len(changed)} changed in-app" + (f": {', '.join(sorted(changed))}" if changed else ""),
            True,
        ),
        ("catalog", f"{ov.catalog_size} problems ({cfg.session.active_list})", ov.catalog_size > 0),
        ("weights", f"{cfg.scoring.weights} ({', '.join(scoring.available_weights())})", True),
        ("schedule", f"{cfg.srs.params} ({', '.join(srs.available_params())})", True),
        ("cards", cards_state, card_count > 0),
        ("events", str(conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]), True),
        ("runs", f"{ov.total_runs} — {stats.gate_note(ov.total_runs)}", True),
    ]
    console.print()
    for label, value, ok in rows:
        mark = "[green]ok[/green] " if ok else "[yellow]-- [/yellow]"
        console.print(f"  {mark} [bright_black]{label:<14}[/bright_black] {value}", soft_wrap=True)
    console.print()
    if not capture.editor_available():
        console.print(
            "  [yellow]no $EDITOR found[/yellow] — solution archiving and reflection "
            "notes will be skipped. Set $EDITOR.\n"
        )
    return 0


# --- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=branding.COMMAND,
        description=branding.DESCRIPTION,
    )
    sub = parser.add_subparsers(dest="command")

    p_stats = sub.add_parser("stats", help="solve time distributions")
    p_stats.add_argument("--tag")
    p_stats.add_argument("--pattern")
    p_stats.add_argument("--difficulty", choices=["easy", "medium", "hard"])
    p_stats.add_argument("--days", type=int, help="lookback window; 0 for all time")
    p_stats.add_argument("--by", choices=["pattern", "difficulty", "tag"], help="break down by")
    p_stats.add_argument("--limit", type=int, default=12)
    p_stats.add_argument("--min-samples", type=int, dest="min_samples")
    p_stats.set_defaults(func=cmd_stats)

    p_history = sub.add_parser("history", help="run rankings")
    p_history.add_argument("--limit", type=int)
    p_history.add_argument("--run", type=int, help="show one run's stat lines, by run number")
    p_history.set_defaults(func=cmd_history)

    p_queue = sub.add_parser("queue", help="today's queue and why it looks like that")
    p_queue.add_argument("-n", type=int, help="problems in the queue (default: config)")
    p_queue.add_argument(
        "--regenerate", action="store_true", help="rebuild today's queue from current cards"
    )
    p_queue.set_defaults(func=cmd_queue)

    p_replay = sub.add_parser("replay", help="rebuild projections from the event log")
    p_replay.set_defaults(func=cmd_replay)

    p_seed = sub.add_parser("seed", help="upsert the problem catalog")
    p_seed.add_argument("--file", help="a JSON catalog to seed from")
    p_seed.add_argument("--list", default=catalog.DEFAULT_LIST)
    p_seed.set_defaults(func=cmd_seed)

    p_doctor = sub.add_parser("doctor", help="paths, catalog, editor, config")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        return cmd_tui(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
