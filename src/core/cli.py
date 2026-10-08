"""Command line entry point.

Bare `<command>` launches the TUI. The subcommands are the things you want without
starting a run: your numbers, your history, and the maintenance operations the
event log makes possible.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from . import (
    branding,
    cache,
    catalog,
    config as config_module,
    db,
    events,
    methods,
    paths,
    problemtypes,
    queues,
    render,
    scoring,
    srs,
    stats,
)

console = Console()


def _open() -> sqlite3.Connection:
    """The database, with the catalog brought in line with `config.toml`.

    Every command that reads problems goes through here, so a set named in the
    config is there whichever way you came in -- the same sync the app runs on
    launch, and as cheap.
    """
    conn = db.open_db()
    catalog.sync(conn, config_module.load(conn).sets)
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
    # One type at a time, the active list's unless told otherwise: a percentile
    # over a design and a LeetCode problem is a number about neither.
    type_name = problemtypes.normalise(args.type) or catalog.type_of(
        conn, cfg.session.active_list
    )

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
        type=type_name,
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
            conn,
            args.by,
            days=days,
            min_samples=min_samples,
            weights=weights,
            limit=args.limit,
            type=type_name,
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
                        render.attempt_rows(attempt),
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
    ways = conn.execute("SELECT COUNT(*) AS n FROM problem_methods").fetchone()["n"]
    console.print(
        f"replayed [bold]{n}[/bold] events → {sessions} sessions, "
        f"{attempts} attempts, {cards} cards, {ways} recorded methods"
    )
    return 0


def cmd_mastered(args: argparse.Namespace) -> int:
    """Problems that have left the rotation.

    The same list the TUI's `m` shows, through the same renderer: mastery is the
    one scheduling decision with no other trace, so both surfaces have to be
    able to answer for it.
    """
    conn = db.open_db()
    cfg = config_module.load(conn)
    rows = srs.mastered_cards(conn)
    size = len(catalog.all_problems(conn, cfg.session.active_list))
    console.print()
    console.print("  [bold]mastered problems[/bold]")
    console.print(render.mastered_table(rows, size, datetime.now(timezone.utc)))
    console.print()
    return 0


def cmd_methods(args: argparse.Namespace) -> int:
    """The ways you know to solve a problem, through the same renderer as `a`.

    One problem's whole list, or every problem's when you name none. Read-only:
    `o` and `e` both write, and both need a screen -- this is the half you can
    pipe into something.
    """
    conn = db.open_db()
    slugs = (
        [args.slug]
        if args.slug
        else [r["slug"] for r in methods.problems_with_methods(conn)]
    )
    if not slugs:
        console.print()
        console.print("  nothing recorded yet")
        console.print()
        return 0
    console.print()
    for slug in slugs:
        console.print(f"  [bold]{slug}[/bold]")
        for line in render.method_list(
            methods.for_problem(conn, slug), methods.variants_for(conn, slug)
        ):
            console.print(line)
        console.print()
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    """Today's queue (spec §10). Generated on demand until Phase 3's cron."""
    conn = _open()
    cfg = config_module.load(conn)
    weights = scoring.load_weights(cfg.scoring.weights)
    active = args.list or cfg.session.active_list
    queue = queues.ensure(
        conn,
        n=args.n or cfg.queue_n_for(active),
        active_list=active,
        weights=weights,
        regenerate=args.regenerate,
        reviews_per_day=cfg.reviews_per_day_for(active),
    )
    console.print()
    console.print(f"  [bold]today's queue[/bold]  ·  {active}  ·  {queue.date}")
    console.print(render.queue_panel(queue))
    console.print()
    return 0


def _print_reports(reports: list[catalog.SetReport]) -> bool:
    """One line per set, and every entry that was left out. True if all was well."""
    fine = True
    for report in reports:
        if report.error:
            fine = False
            console.print(
                f"  [yellow]--[/yellow]  {report.name:<18} [yellow]{report.error}[/yellow]",
                highlight=False,
                soft_wrap=True,
            )
            continue
        ptype = problemtypes.load(report.type)
        kind = report.type + (" [yellow](no such type)[/yellow]" if ptype.missing else "")
        console.print(
            f"  [green]ok[/green]  {report.name:<18} [bold]{report.problems:>4}[/bold] problems"
            f"  [bright_black]{kind}  ·  {report.source}[/bright_black]",
            highlight=False,
            soft_wrap=True,
        )
        for line in report.skipped:
            fine = False
            console.print(f"        [yellow]left out[/yellow]  {line}", highlight=False)
        fine = fine and not ptype.missing
    return fine


def cmd_seed(args: argparse.Namespace) -> int:
    """Bring the catalog in line with `config.toml`, and say what that did.

    With no arguments this is the sync the app runs on every launch, out loud:
    every set, how many problems it held, and any entry that was left out and
    why. `--file` is the by-hand way in, for a list you have not named in the
    config.
    """
    conn = db.open_db()
    cfg = config_module.load(conn)
    if args.file:
        name = args.list or catalog.DEFAULT_LIST
        sets = [
            catalog.ProblemSet(
                name,
                problemtypes.normalise(args.type) or cfg.type_of(name),
                str(Path(args.file).expanduser().resolve()),
            )
        ]
    elif args.list:
        named = cfg.set_named(args.list)
        sets = [named or catalog.ProblemSet(args.list, problemtypes.normalise(args.type) or cfg.type_of(args.list))]
    else:
        sets = list(cfg.sets)

    console.print()
    fine = _print_reports(catalog.sync(conn, sets))
    console.print(f"\n  {catalog.count(conn)} problems in the catalog\n")
    return 0 if fine else 1


def _rule_words(entry: problemtypes.Field) -> str:
    """What one field does to a review, in a line. "" for a field that does nothing."""
    rule = entry.review
    bits: list[str] = []
    if rule.again_on:
        bits.append(f"{'/'.join(rule.again_on)} fails it")
    if rule.again_below is not None:
        bits.append(f"under {rule.again_below:g} fails it")
    if rule.hard_on:
        bits.append(f"{'/'.join(rule.hard_on)} is Hard")
    if rule.hard_below is not None:
        bits.append(f"under {rule.hard_below:g} is Hard")
    if rule.unless_better_known and (rule.hard_on or rule.hard_below is not None):
        bits[-1] += " unless a better method is recorded"
    if rule.easy_on:
        bits.append(f"Easy needs {'/'.join(rule.easy_on)}")
    if rule.easy_from is not None:
        bits.append(f"Easy needs {rule.easy_from:g} or more")
    if rule.easy_needs_answer:
        bits.append("Easy needs an answer")
    return ", ".join(bits)


def _print_type(ptype: problemtypes.ProblemType, cfg: config_module.Config) -> None:
    sets = [s.name for s in cfg.sets if s.type == ptype.name]
    console.print(
        f"  [bold]{ptype.name}[/bold]  [bright_black]{ptype.title}  ·  {ptype.source}[/bright_black]",
        highlight=False,
        soft_wrap=True,
    )
    console.print(f"    [bright_black]sets     [/bright_black] {', '.join(sets) or '—'}", highlight=False)

    screens = [
        name
        for name in ("patterns", "methods", "solution", "note", "again")
        if getattr(ptype.screens, name)
    ]
    console.print(f"    [bright_black]screens  [/bright_black] {', '.join(screens) or 'none'}", highlight=False)
    asks = ["judge" if ptype.judge else "no judge"]
    if ptype.fetch:
        asks.append(f"offline cache from {ptype.fetch}")
    if ptype.language:
        asks.append(f"archived as {ptype.language}")
    if ptype.par_seconds:
        asks.append(
            "par " + "/".join(scoring.fmt_duration(seconds) for _, seconds in ptype.par_seconds)
        )
    console.print(f"    [bright_black]solve    [/bright_black] {', '.join(asks)}", highlight=False)

    for group in ptype.groups():
        if group.labelled:
            console.print(f"    [bright_black]{group.label}[/bright_black]", highlight=False)
        for entry in group.fields:
            where = "column" if entry.key in problemtypes.COLUMN_KEYS else "answers"
            line = f"      {entry.key:<26} [bright_black]{entry.kind:<9}{where:<9}[/bright_black]"
            words = _rule_words(entry)
            if words:
                line += f" {words}"
            console.print(line, highlight=False, soft_wrap=True)
    if not ptype.fields:
        console.print("    [bright_black]asks nothing beyond the verdict[/bright_black]")
    for problem in ptype.problems:
        console.print(f"    [yellow]--[/yellow] {problem}", highlight=False, soft_wrap=True)
    console.print()


_LABEL_LINE = re.compile(r'^label[ \t]*=.*$', re.MULTILINE)


def _relabel(text: str, name: str) -> str:
    """A copied type's file, named for what it now is.

    Only the `label` line, and only the first one. A type copied out under a new
    name is a new type, and left alone it would go on calling itself by the old
    one -- on the stats screen's title, in this command's own listing -- which
    is two types you cannot tell apart on sight. Everything else in the file is
    left exactly as written, comments included: those are the part worth
    copying.
    """
    label = f'label = "{name.replace("-", " ")}"'
    if _LABEL_LINE.search(text):
        return _LABEL_LINE.sub(label, text, count=1)
    return f"{label}\n\n{text}"


def cmd_types(args: argparse.Namespace) -> int:
    """The problem types there are, what each one asks, and where it came from.

    `--new` writes one into your config directory to edit. It starts as a copy
    of an existing type, comments and all, because a page of worked fields is a
    better place to start than a blank file and a format to look up.
    """
    cfg = config_module.load()
    if args.new:
        name = problemtypes.normalise(args.new)
        if not name:
            console.print(f"[red]{args.new!r} is not a name[/red] — lowercase letters, digits and hyphens")
            return 1
        target = problemtypes.user_path(name)
        if target.exists():
            console.print(f"[yellow]{target} already exists[/yellow] — edit it, or delete it to start over")
            return 1
        base = problemtypes.normalise(args.base) or (
            name if problemtypes.bundled_text(name) is not None else problemtypes.DEFAULT_TYPE
        )
        text = None
        mine = problemtypes.user_path(base)
        if mine.exists():
            text = mine.read_text(encoding="utf-8")
        if text is None:
            text = problemtypes.bundled_text(base)
        if text is None:
            console.print(
                f"[red]no type called {base!r} to start from[/red] — "
                f"there is {', '.join(problemtypes.available())}"
            )
            return 1
        if name != base:
            text = _relabel(text, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        # Never wrapped: a path broken across two lines is a path you cannot
        # paste.
        console.print(f"\n  wrote {target}", highlight=False, soft_wrap=True)
        console.print(
            f"  [bright_black]a copy of `{base}`. Edit it, then name it under \\[sets] in[/bright_black]"
        )
        console.print(
            f"  [bright_black]{paths.config_file()}:[/bright_black]\n",
            highlight=False,
            soft_wrap=True,
        )
        console.print(f"    \\[sets.my-{name}-problems]", highlight=False)
        console.print(f'    type = "{name}"', highlight=False)
        console.print('    path = "sets/my-problems.json"\n', highlight=False)
        return 0

    # Every type there is a file for, and every type a set names whether or not
    # there is one -- a set pointing at a type that does not exist is exactly
    # the thing this listing has to be able to show.
    names = [problemtypes.normalise(args.name)] if args.name else list(
        dict.fromkeys((*problemtypes.available(), *(s.type for s in cfg.sets)))
    )
    console.print()
    fine = True
    for name in names:
        ptype = problemtypes.load(name)
        fine = fine and not ptype.problems
        _print_type(ptype, cfg)
    return 0 if fine else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    """Warm the offline cache, so a flight is a normal run (see `core.cache`).

    The whole active list, in priority order, bounded by `[cache] max_mb`. There
    is no per-problem selection on purpose: the queue cannot know what you will
    need on day two of a trip, and a cache that is missing the problem you were
    handed fails at the one moment nothing can be done about it.
    """
    conn = _open()
    cfg = config_module.load(conn)
    lists = (args.list,) if args.list else cfg.active_lists
    budget = args.max_mb * 1024 * 1024 if args.max_mb else cfg.cache.budget_bytes

    if args.session:
        # Prompted, never taken from argv: an argument would be visible in `ps`
        # and would land in shell history.
        import getpass

        value = getpass.getpass("LEETCODE_SESSION cookie (input hidden): ").strip()
        if not value:
            console.print("[yellow]nothing entered[/yellow] — session unchanged")
            return 1
        path = cache.write_session(value)
        console.print(f"stored in {path} (0600)")

    if args.dry_run:
        order = cache.priority(conn, lists)
        have = sum(1 for p in order if cache.local_path(p.slug) is not None)
        console.print()
        console.print(
            f"  [bold]{len(order)}[/bold] problems in {', '.join(lists)} — "
            f"{have} already cached, {len(order) - have} to fetch"
        )
        console.print(f"  budget [bold]{cache.fmt_bytes(budget)}[/bold]\n")
        for index, problem in enumerate(order, start=1):
            mark = "[green]·[/green]" if cache.local_path(problem.slug) else " "
            console.print(f"  {mark} {index:>3}. {problem.slug}", highlight=False)
        console.print()
        return 0

    console.print()
    with console.status("fetching…") as spinner:

        def progress(slug: str, state: str) -> None:
            spinner.update(f"{slug} — {state}")
            if state.startswith("failed"):
                console.print(f"  [yellow]--[/yellow] {slug}: {state}", highlight=False)

        report = cache.sync(
            conn,
            lists=lists,
            budget_bytes=budget,
            language=cfg.capture.language,
            refresh=args.refresh,
            on_progress=progress,
        )

    bits = [f"[bold]{len(report.fetched)}[/bold] fetched"]
    if report.kept:
        bits.append(f"{len(report.kept)} already cached")
    if report.upgraded:
        bits.append(f"{len(report.upgraded)} re-rendered")
    if report.pruned:
        bits.append(f"{len(report.pruned)} pruned")
    if report.skipped:
        bits.append(f"[yellow]{len(report.skipped)} over budget[/yellow]")
    if report.failures:
        bits.append(f"[yellow]{len(report.failures)} failed[/yellow]")
    console.print(
        f"  {', '.join(bits)} — {cache.fmt_bytes(report.total_bytes)}"
        f" / {cache.fmt_bytes(budget)}"
    )
    if report.skipped:
        console.print(
            r"  [yellow]budget spent[/yellow] — raise \[cache] max_mb to cache the"
            f" remaining {len(report.skipped)}"
        )
    console.print()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import audio, capture

    conn = db.open_db()
    cfg = config_module.load(conn)
    reports = catalog.sync(conn, cfg.sets)
    complaints = catalog.complaints(reports)
    ov = stats.overview(conn)
    editor = " ".join(config_module.editor())

    sets_state = ", ".join(
        f"{r.name} ({r.problems})" for r in reports if not r.error
    ) or "none"
    types_in_use = list(dict.fromkeys(s.type for s in cfg.sets))
    # Where each one came from, when it was not the package: a type you have
    # written over a bundled one is the first thing to suspect when a prompt is
    # asking something you do not remember it asking.
    origin = {"bundled": "", "missing": " (no such type)"}
    types_state = ", ".join(
        f"{name}{origin.get(problemtypes.load(name).source, ' (yours)')}"
        for name in types_in_use
    )

    now = datetime.now(timezone.utc)
    card_count, due_now, mastered = srs.counts(conn, now)
    if card_count:
        nxt = srs.next_due(conn)
        cards_state = f"{card_count} scheduled, {due_now} due now"
        if mastered:
            cards_state += f", {mastered} mastered"
        if nxt:
            cards_state += f", next {nxt[:10]}"
    else:
        cards_state = "none yet — finish a problem and one appears"

    cache_status = cache.status(
        conn, lists=cfg.active_lists, budget_bytes=cfg.cache.budget_bytes
    )
    cache_state = (
        f"{cache_status.cached}/{cache_status.total} problems, "
        f"{cache.fmt_bytes(cache_status.bytes)} / {cache.fmt_bytes(cache_status.budget_bytes)}"
    )
    if cache_status.fetched_at:
        cache_state += f", fetched {cache_status.fetched_at[:10]}"
    if cfg.cache.offline:
        cache_state += "  ·  OFFLINE MODE ON"
    if not cache_status.cached:
        cache_state += f" — run `{branding.COMMAND} fetch`"

    # Never the value, only whether there is one.
    has_session = cache.session_cookie() is not None
    if not has_session:
        session_state = f"none — premium problems will be skipped ({cache.ENV_SESSION})"
    elif os.environ.get(cache.ENV_SESSION):
        session_state = f"set via {cache.ENV_SESSION}"
    else:
        session_state = str(paths.session_file())
        if cache.session_is_exposed():
            session_state += "  [yellow](readable by others — chmod 600)[/yellow]"

    changed = config_module.overrides(conn)
    capture_state = "on" if cfg.capture.enabled else "off"
    if cfg.capture.enabled:
        capture_state += f", wrong answers {'on' if cfg.capture.on_failed_submit else 'off'}"

    # The recorder is only a problem when it is meant to be running, so this is
    # green whenever speech mode is off and only complains when it is on.
    recorder_ok = audio.available()
    if cfg.audio.speech_mode:
        speech_state = (
            f"on, {cfg.audio.bitrate_kbps} kbps from {cfg.audio.input_format}:{cfg.audio.device}"
        )
        if not recorder_ok:
            speech_state += f" — no {audio.ffmpeg()} on PATH, runs won't record"
    else:
        speech_state = "off" + ("" if recorder_ok else f" (no {audio.ffmpeg()} on PATH)")

    rows = [
        ("config", str(paths.config_file()), paths.config_file().exists()),
        ("database", str(paths.db_file()), paths.db_file().exists()),
        ("code archive", str(paths.code_dir()), paths.code_dir().exists()),
        ("notes", str(paths.notes_dir()), paths.notes_dir().exists()),
        ("editor", editor, capture.editor_available()),
        ("capture", capture_state, cfg.capture.enabled),
        ("speech mode", speech_state, recorder_ok or not cfg.audio.speech_mode),
        (
            "settings",
            f"{len(changed)} changed in-app" + (f": {', '.join(sorted(changed))}" if changed else ""),
            True,
        ),
        ("catalog", f"{ov.catalog_size} problems ({cfg.session.active_list})", ov.catalog_size > 0),
        ("sets", sets_state, not any(r.error or r.skipped for r in reports)),
        (
            "types",
            types_state,
            not any(problemtypes.load(name).problems for name in types_in_use),
        ),
        ("cache", cache_state, cache_status.complete),
        ("leetcode session", session_state, has_session),
        ("weights", f"{cfg.scoring.weights} ({', '.join(scoring.available_weights())})", True),
        ("schedule", f"{cfg.srs.params} ({', '.join(srs.available_params())})", True),
        ("cards", cards_state, card_count > 0),
        ("events", str(conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]), True),
        ("runs", str(ov.total_runs), True),
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
    # Named one by one, because every one of these is otherwise silent: a set
    # that did not load is a list that is simply not there, and a type that
    # does not exist is a problem that runs and asks nothing.
    if complaints:
        for line in complaints:
            console.print(f"  [yellow]{line}[/yellow]", highlight=False, soft_wrap=True)
        console.print(
            f"  [bright_black]`{branding.COMMAND} seed` and `{branding.COMMAND} types`"
            " say more[/bright_black]\n"
        )
    # Only failures are worth naming. A healthy cache is the status line above;
    # listing 150 problems that worked would be a screen nobody reads.
    if cache_status.failures:
        console.print(f"  [yellow]{len(cache_status.failures)} problems failed to cache[/yellow]")
        for slug, reason in sorted(cache_status.failures.items()):
            console.print(f"    {slug}: {reason}", highlight=False)
        console.print()
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
    p_stats.add_argument(
        "--by", choices=["pattern", "difficulty", "tag", "strategy"], help="break down by"
    )
    p_stats.add_argument("--type", help="which problem type (default: the active list's)")
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
    p_queue.add_argument("--list", help="which list's queue (default: the active one)")
    p_queue.set_defaults(func=cmd_queue)

    p_types = sub.add_parser("types", help="the problem types, and what each one asks")
    p_types.add_argument("name", nargs="?", help="one type; omit for all of them")
    p_types.add_argument(
        "--new", metavar="NAME", help="write a type into your config directory to edit"
    )
    p_types.add_argument(
        "--from",
        dest="base",
        metavar="TYPE",
        help="the type --new starts as a copy of (default: the bundled one of that name)",
    )
    p_types.set_defaults(func=cmd_types)

    p_mastered = sub.add_parser("mastered", help="problems that have left the rotation")
    p_mastered.set_defaults(func=cmd_mastered)

    p_methods = sub.add_parser("methods", help="every way you know to solve a problem")
    p_methods.add_argument("slug", nargs="?", help="one problem; omit for all of them")
    p_methods.set_defaults(func=cmd_methods)

    p_replay = sub.add_parser("replay", help="rebuild projections from the event log")
    p_replay.set_defaults(func=cmd_replay)

    p_seed = sub.add_parser("seed", help="seed every problem set in the config")
    p_seed.add_argument("--file", help="a JSON list to seed by hand, outside the config")
    p_seed.add_argument("--list", help="one set; with --file, the name to seed it under")
    p_seed.add_argument("--type", help="with --file, the problem type its entries are")
    p_seed.set_defaults(func=cmd_seed)

    p_fetch = sub.add_parser("fetch", help="cache the active list for offline use")
    p_fetch.add_argument(
        "--refresh", action="store_true", help="re-download problems already cached"
    )
    p_fetch.add_argument("--list", help="cache this list instead of the active one")
    p_fetch.add_argument(
        "--max-mb", type=int, dest="max_mb", help="cache size ceiling (default: config)"
    )
    p_fetch.add_argument(
        "--dry-run", action="store_true", help="show the priority order, fetch nothing"
    )
    p_fetch.add_argument(
        "--session",
        action="store_true",
        help="prompt for your LEETCODE_SESSION cookie (unlocks premium problems)",
    )
    p_fetch.set_defaults(func=cmd_fetch)

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
