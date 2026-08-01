"""Rich renderables shared by the CLI and the TUI.

One implementation per screen, rendered into a terminal by the `stats` command
and into a widget by the Textual app, so the two can never drift.
"""

from __future__ import annotations

from typing import Sequence

from . import branding

from rich.console import Group, RenderableType
from rich.text import Text

from .scoring import Score, fmt_duration, ordinal
from .stats import PHASE_2_GATE, Distribution, Run, RunStanding

WIDTH = 62

DIFFICULTY_STYLE = {"easy": "green", "medium": "yellow", "hard": "red"}
# keyed by the rendered label, since that is what the stat line carries
VERDICT_LABEL_STYLE = {
    "ACCEPTED": "bold green",
    "WRONG ANSWER": "red",
    "TIME LIMIT EXCEEDED": "red",
    "USED EDITORIAL": "bright_black",
    "GAVE UP": "bright_black",
}

BANNER = branding.BANNER


def rule(char: str = "─", width: int = WIDTH, style: str = "bright_black") -> Text:
    """A divider indented to line up with the two-space content gutter."""
    return Text("  " + char * (width - 4), style=style)


def bar(ratio: float | None, width: int = 13, filled: str = "█", empty: str = "░") -> str:
    if ratio is None:
        return ""
    ratio = max(0.0, min(1.0, ratio))
    n = round(ratio * width)
    return filled * n + empty * (width - n)


def signed(n: int) -> str:
    if n == 0:
        return "0"
    return f"+{n}" if n > 0 else f"−{abs(n)}"


def delta_style(n: int) -> str:
    if n > 0:
        return "green"
    if n < 0:
        return "red"
    return "bright_black"


# --- per-problem stat line (spec §5) ---------------------------------------


def stat_line(title: str, difficulty: str, score: Score, confidence: int | None = None) -> RenderableType:
    head = Text("  ")
    head.append(title, style="bold")
    pad = max(1, WIDTH - 2 - len(title) - len(difficulty) - 2)
    head.append(" " * pad)
    head.append(f"[{difficulty.upper()}]", style=DIFFICULTY_STYLE.get(difficulty.lower(), "white"))

    rows: list[RenderableType] = [head, rule()]
    for c in score.components:
        line = Text("  ")
        line.append(f"{c.label:<9}", style="bright_black")
        line.append(f"{c.detail:<26}", style=VERDICT_LABEL_STYLE.get(c.detail, ""))
        line.append(f"{bar(c.ratio):<14}", style="cyan")
        line.append(f"{signed(c.delta):>6}", style=delta_style(c.delta))
        rows.append(line)

    if confidence:
        line = Text("  ")
        line.append(f"{'recall':<9}", style="bright_black")
        line.append(f"{CONFIDENCE_LABELS.get(confidence, str(confidence)):<26}")
        line.append(f"{'':<14}")
        line.append(f"{'':>6}")
        rows.append(line)

    rows.append(rule())
    total = Text(" " * (WIDTH - 13))
    total.append("SCORE ", style="bold")
    total.append(f"{score.total:>5}", style="bold cyan" if score.total > 0 else "bold bright_black")
    rows.append(total)
    return Group(*rows)


CONFIDENCE_LABELS = {
    1: '"gone in a week"',
    2: '"shaky"',
    3: '"good"',
    4: '"solid"',
}


# --- death screen (spec §5) ------------------------------------------------


def death_screen(
    run: Run,
    standing: RunStanding | None,
    all_runs: Sequence[Run],
    leaderboard_size: int = 5,
) -> RenderableType:
    heavy = Text("  " + "═" * (WIDTH - 4), style="bright_black")
    num = standing.run_number if standing else 1
    title = f"RUN #{num}  —  {run.local_date}  —  ENDED {run.local_end_time}"
    head = Text(" " * max(0, (WIDTH - len(title)) // 2) + title, style="bold")

    def pair(l1: str, v1: str, l2: str = "", v2: str = "") -> Text:
        t = Text("   ")
        t.append(f"{l1:<14}", style="bright_black")
        t.append(f"{v1:<15}")
        if l2:
            t.append(f"{l2:<14}", style="bright_black")
            t.append(v2)
        return t

    best = f"{standing.best_score}  (run #{standing.best_run_number})" if standing else "—"
    rows: list[RenderableType] = [
        heavy,
        head,
        heavy,
        pair("problems", f"{run.finished} / {run.planned_n}", "clean solves", str(run.clean_solves)),
        pair("total time", fmt_duration(run.total_active_seconds), "hints used", str(run.hints_used)),
        pair("score", str(run.score), "best ever", best),
    ]
    if standing and standing.percentile is not None:
        rows.append(
            pair("percentile", f"{ordinal(round(standing.percentile))}  of {standing.total_runs} runs")
        )
    rows.append(rule())

    # You vs. your past self: this run's neighbourhood in the all-time ranking.
    numbered = {r.session_id: i for i, r in enumerate(all_runs, start=1)}
    ranked = sorted(all_runs, key=lambda r: r.score, reverse=True)
    if ranked:
        top = max(r.score for r in ranked) or 1
        pos = next((i for i, r in enumerate(ranked) if r.session_id == run.session_id), 0)
        half = leaderboard_size // 2
        start = max(0, min(pos - half, len(ranked) - leaderboard_size))
        for r in ranked[start : start + leaderboard_size]:
            is_this = r.session_id == run.session_id
            line = Text("   ")
            line.append(f"▸ #{numbered[r.session_id]:<4}", style="bright_black")
            line.append(f"{r.score:>4}  ", style="bold" if is_this else "")
            line.append(bar(r.score / top, width=20, empty=" "), style="cyan" if is_this else "bright_black")
            if is_this:
                line.append("  ← this run", style="cyan")
            rows.append(line)
        rows.append(rule())

    if run.session_note:
        note = Text("   ")
        note.append("note  ", style="bright_black")
        note.append(run.session_note.splitlines()[0][: WIDTH - 12], style="italic")
        rows.append(note)

    return Group(*rows)


# --- percentile screen (spec §6) -------------------------------------------


def distribution_panel(dist: Distribution, show_tail: bool = True) -> RenderableType:
    head = Text("  ")
    head.append(dist.label, style="bold")
    right = f"n={dist.n}"
    if dist.window_days:
        right += f"   last {dist.window_days}d"
    head.append(" " * max(1, WIDTH - 2 - len(dist.label) - len(right)))
    head.append(right, style="bright_black")

    rows: list[RenderableType] = [head, rule()]

    scale = max([v for v in (dist.p99, dist.par_seconds) if v] or [1])
    for name, value in (("p50", dist.p50), ("p90", dist.p90), ("p99", dist.p99)):
        line = Text("  ")
        # p99 on a thin slice is one sample and is noise — grey it rather than
        # pretend it means something (spec §6).
        dim = name == "p99" and not dist.reliable
        line.append(f"{name:<6}", style="bright_black")
        line.append(f"{fmt_duration(value):<8}", style="bright_black" if dim else "bold")
        line.append(
            f"{bar(value / scale if value else 0, width=25):<26}",
            style="bright_black" if dim else "cyan",
        )
        if name == "p99":
            if dist.par_seconds:
                line.append(f"par {fmt_duration(dist.par_seconds)}", style="bright_black")
            if dim:
                line.append(f"  (n<{dist.min_samples}: noise)", style="bright_black")
        rows.append(line)

    rows.append(rule())

    if dist.clean_rate is not None:
        line = Text("  ")
        line.append(f"{'clean solve rate':<21}", style="bright_black")
        line.append(f"{dist.clean_rate * 100:.0f}%".ljust(11))
        delta = dist.clean_rate_delta
        if delta is not None and dist.window_days:
            arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
            line.append("trend  ", style="bright_black")
            line.append(f"{arrow} {delta * 100:+.0f}%", style="green" if delta > 0 else ("red" if delta < 0 else "bright_black"))
            line.append(f" vs prior {dist.window_days}d", style="bright_black")
        rows.append(line)

    if show_tail and dist.tail_drivers:
        first = True
        for driver in dist.tail_drivers:
            line = Text("  ")
            line.append(f"{'tail drivers:' if first else '':<15}", style="bright_black")
            line.append(f"{driver.slug!r} ", style="yellow")
            line.append(f"({fmt_duration(driver.active_seconds)}, {driver.reason})", style="bright_black")
            rows.append(line)
            first = False

    return Group(*rows)


def empty_state(message: str, hint: str = "") -> RenderableType:
    rows: list[RenderableType] = [Text(""), Text("  " + message, style="bright_black")]
    if hint:
        rows.append(Text("  " + hint, style="bright_black italic"))
    return Group(*rows)


# --- the queue (spec §10) --------------------------------------------------


def queue_panel(queue, show_rationale: bool = True) -> RenderableType:
    """Today's queue: what to do, in what order, and why.

    Reviews are marked and their overdue age shown, because "3 reviews" is a
    number and "this one has been sitting for nine days" is a reason to start.
    """
    if queue is None or not queue.items:
        return empty_state(
            "Nothing queued.",
            "Seed the catalog and log a run — the queue builds itself from there.",
        )

    rows: list[RenderableType] = [rule()]
    header = Text("  ")
    header.append(f"{'problem':<34}", style="bright_black")
    header.append(f"{'pattern':<20}", style="bright_black")
    header.append("when", style="bright_black")
    rows.append(header)

    for n, item in enumerate(queue.items, 1):
        line = Text("  ")
        line.append(f"{n} ", style="bright_black")
        title = item.title if len(item.title) <= 29 else item.title[:28] + "…"
        line.append(f"{title:<30}", style="bold" if item.is_review else "")
        line.append(
            f"{(item.difficulty or '?')[:1].upper():<3}",
            style=DIFFICULTY_STYLE.get((item.difficulty or "").lower(), ""),
        )
        line.append(f"{(item.pattern or '—'):<20}", style="bright_black")
        if item.is_review:
            when = f"review · {item.overdue_days}d overdue" if item.overdue_days else "review · due"
            line.append(when, style="yellow")
        else:
            line.append("new", style="bright_black")
        rows.append(line)

    rows.append(rule())
    counts = Text("  ")
    counts.append(f"{queue.due_count} review", style="yellow")
    counts.append(" · ", style="bright_black")
    counts.append(f"{queue.new_count} new", style="bright_black")
    counts.append(f"   {queue.generated_by}", style="bright_black")
    rows.append(counts)

    if show_rationale and queue.rationale:
        rows.append(Text(""))
        for chunk in _wrap(queue.rationale, WIDTH - 6):
            rows.append(Text("  " + chunk, style="bright_black italic"))
    return Group(*rows)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


# --- history ---------------------------------------------------------------


def history_table(runs: Sequence[Run], limit: int | None = None) -> RenderableType:
    if not runs:
        return empty_state(
            "No runs logged yet.",
            f"Start one with `{branding.COMMAND}` — the gate is 20 sessions before Phase 2.",
        )

    ranked = sorted(runs, key=lambda r: r.score, reverse=True)
    rank_of = {r.session_id: i for i, r in enumerate(ranked, start=1)}
    numbered = {r.session_id: i for i, r in enumerate(runs, start=1)}
    top = max((r.score for r in runs), default=1) or 1

    head = Text("  ")
    for label, w in (("run", 6), ("date", 12), ("solved", 8), ("clean", 7), ("time", 10), ("score", 7), ("rank", 6)):
        head.append(f"{label:<{w}}", style="bright_black")
    rows: list[RenderableType] = [head, rule()]

    ordered = list(reversed(runs))
    if limit:
        ordered = ordered[:limit]
    for r in ordered:
        line = Text("  ")
        line.append(f"{'#' + str(numbered[r.session_id]):<6}", style="bold")
        line.append(f"{r.local_date:<12}")
        line.append(f"{str(r.solved) + '/' + str(r.planned_n):<8}")
        line.append(f"{r.clean_solves:<7}")
        line.append(f"{fmt_duration(r.total_active_seconds):<10}")
        line.append(f"{r.score:<7}", style="cyan")
        rank = rank_of[r.session_id]
        line.append(f"{ordinal(rank):<6}", style="bold green" if rank == 1 else "bright_black")
        line.append(bar(r.score / top, width=12, empty=" "), style="bright_black")
        rows.append(line)

    rows.append(rule())
    total = sum(r.score for r in runs)
    footer = Text("  ")
    footer.append(f"{len(runs)} runs", style="bright_black")
    footer.append(f"   total {total}", style="bright_black")
    footer.append(f"   best {max(r.score for r in runs)}", style="bright_black")
    gate = PHASE_2_GATE - len(runs)
    if gate > 0:
        footer.append(f"   ·  {gate} more session{'s' if gate != 1 else ''} to the Phase 2 gate", style="yellow")
    rows.append(footer)
    return Group(*rows)
