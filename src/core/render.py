"""Rich renderables shared by the CLI and the TUI.

One implementation per screen, rendered into a terminal by the `stats` command
and into a widget by the Textual app, so the two can never drift.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from . import branding, scoring

from rich.console import Group, RenderableType
from rich.text import Text

from .scoring import Score, fmt_duration, ordinal
from .stats import Distribution, PastAttempt, Run, RunStanding

WIDTH = 62

DIFFICULTY_STYLE = {"easy": "green", "medium": "yellow", "hard": "red"}

#: Goes in front of the name of a problem that has been mastered, everywhere one
#: is listed. One character and one style, in one place, so the four screens that
#: draw a problem name cannot each invent their own way of saying it.
MASTERED_MARK = "★"
MASTERED_STYLE = "yellow"


def mastered_prefix(mastered: bool, *, width: int = 2) -> Text:
    """`★ ` if mastered, blank of the same width if not.

    Padded rather than omitted: the star has to sit in its own column, or every
    unmastered title on the screen shifts left by one and the list stops being a
    list. `width` is the whole column including the trailing space.
    """
    mark = MASTERED_MARK if mastered else ""
    return Text(f"{mark:<{width}}", style=MASTERED_STYLE if mastered else "")
# keyed by the rendered label, since that is what the stat line carries.
# The solve ladder fades as the help increases — green for the one you want,
# dimming rung by rung, so a column of stat lines reads at a glance.
VERDICT_LABEL_STYLE = {
    "SOLVED, NO HELP": "bold green",
    "SOLVED WITH HINTS": "green",
    "SOLVED AFTER DESCRIPTION": "yellow",
    "SOLVED AFTER PSEUDOCODE": "bright_black",
    "SOLVED AFTER IMPLEMENTATION": "bright_black",
    "GAVE UP": "bright_black",
    # Yellow, not the grey the surrenders get: this one is an open loop, not a
    # closed one. You still owe this problem a verdict.
    "NOT GRADED": "yellow",
    # legacy labels, still in the log — see scoring.LEGACY_VERDICTS
    "ACCEPTED": "bold green",
    "WRONG ANSWER": "red",
    "TIME LIMIT EXCEEDED": "red",
    "USED EDITORIAL": "bright_black",
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


def stat_line(
    title: str,
    difficulty: str,
    score: Score,
    confidence: int | None = None,
    approach: Sequence[tuple[str, str]] = (),
) -> RenderableType:
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

    # Passed in rather than built by `score_attempt`, like `confidence` above it.
    # Two reasons: nothing here is scored, and `score_attempt`'s component list
    # ends with a guard that folds the rounding residual into the last entry --
    # a zero-delta row appended there would silently absorb it.
    for label, detail in approach:
        line = Text("  ")
        line.append(f"{label:<9}", style="bright_black")
        line.append(f"{detail:<26}")
        line.append(f"{'':<14}")
        line.append(f"{'':>6}")
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


# The finish modal's four answers, short enough to sit in a stat line. Same 1..4
# the log has always stored, so an attempt recorded under the old wording still
# renders at the rung it was given.
CONFIDENCE_LABELS = {
    1: '"no idea"',
    2: '"I\'d struggle"',
    3: '"I\'d get there"',
    4: '"I\'d nail it"',
}

# The finish modal's optimality answers, short enough to sit beside a complexity.
# One set of words for both axes, and for the axeless answer the log still holds:
# the three rungs never changed, only how many times you are asked to pick one.
OPTIMALITY_LABELS = {
    "optimal": "optimal",
    "suboptimal": "not optimal",
    "unsure": "not sure",
}


def _cost(complexity: Any, optimality: Any) -> str:
    """`O(n log n)  ·  optimal` — what you said it costs, and whether it was the one."""
    parts = [
        (complexity or "").strip(),
        OPTIMALITY_LABELS.get(optimality or "", ""),
    ]
    return "  ·  ".join(p for p in parts if p)


# What `scoring.solution_quality` derived, in words. Short enough for a stat
# line's 26-column detail field, and plain enough to be read by someone who has
# never seen the four identifiers behind them.
#
# `alternative_valid_solution` says "optimal" first on purpose: it is not a
# lesser grade than `optimal`, it is the same grade reached a different way, and
# a label that led with "different" would read as a correction.
QUALITY_LABELS = {
    scoring.QUALITY_OPTIMAL: "optimal",
    scoring.QUALITY_ALTERNATIVE: "optimal, a different route",
    scoring.QUALITY_SUBOPTIMAL: "beaten, but you saw better",
    scoring.QUALITY_BRUTEFORCE: "brute force only",
}


def quality_label(attempt: Mapping[str, Any]) -> str:
    """The quality row's text, or "" when nothing was claimed.

    Empty rather than "unknown": an unanswered optimality question is not a
    claim, and a row saying so would be a row about the prompt rather than about
    the solve.
    """
    return QUALITY_LABELS.get(attempt.get("solution_quality") or "", "")


def quality_reason(attempt: Mapping[str, Any]) -> str:
    """The inputs that decided the quality, so the derivation can be checked.

    This exists because the value is derived and nothing stores it. A label on
    its own is a claim you have to take on trust; the same label followed by
    "not optimal · 1 better approach named" is a claim you can audit from the
    screen it appears on.
    """
    if not attempt.get("solution_quality"):
        return ""
    bits = [OPTIMALITY_LABELS.get(attempt.get("time_optimality") or "", "")]
    worth = list(attempt.get("strategies_worth_learning") or ())
    used = list(attempt.get("strategies_used") or ())
    # Two ways of having known better, and the line says which one it read.
    # `worth` is the retired role, still on old attempts and still counted in
    # the words it was given in; `saw_better` without it is the problem's own
    # list of methods carrying an optimal one you did not write.
    if worth:
        bits.append(f"{len(worth)} better approach{'es' if len(worth) != 1 else ''} named")
    elif attempt.get("saw_better"):
        bits.append("an optimal method is recorded that you did not write")
    elif attempt.get("time_optimality") == "suboptimal":
        bits.append("no optimal method recorded")
    if attempt.get("solution_quality") == scoring.QUALITY_ALTERNATIVE and used:
        bits.append("not the route you took last time")
    return "  ·  ".join(b for b in bits if b)


def attempt_rows(attempt: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Everything a stat line says about a solution: what it cost, and how.

    One call rather than two concatenated at three sites, so the summary, the
    history detail and `p99 stats` cannot end up showing different halves of the
    same answer.
    """
    rows = approach_rows(attempt) + strategy_rows(attempt)
    resolves = int(attempt.get("resolves") or 0)
    if resolves:
        rows.append(("re-solves", f"{resolves} more pass{'es' if resolves > 1 else ''}"))
    return rows


def strategy_rows(attempt: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The stat line's rows for the two post-solve prompts, and what they came to.

    Same build-the-rows-or-emit-nothing rule as `approach_rows`, and for the
    same reason -- a stat line should not grow three blank lines to say that you
    skipped a prompt which was optional in the first place.

    Two subjects, two rows. `patterns` is what you reached for, from the shared
    vocabulary; `method` is the route you took through this problem, in this
    problem's own words. They are separate lines because they are separate facts,
    and a line that ran them together would be the conflation `methods` exists to
    undo.

    Labels are short because the stat line's label column is nine wide.
    """
    rows = [
        ("patterns", ", ".join(attempt.get("strategies_used") or ())),
        ("method", ", ".join(attempt.get("methods_used") or ())),
        # Legacy only. Nothing writes this role any more, and the row is here so
        # that attempts recorded under it still read back the way they were
        # given -- see `strategies`.
        ("better", ", ".join(attempt.get("strategies_worth_learning") or ())),
        ("quality", quality_label(attempt)),
    ]
    return [row for row in rows if row[1]]


def approach_rows(attempt: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The stat line's cost rows: one per axis, labelled `time` and `space`.

    Empty when you answered nothing, which is why the rows are built rather than
    always emitted: a stat line should not grow a blank line to say nothing.

    An attempt recorded before the question had axes gets its single unqualified
    row back, labelled `approach` exactly as it was written. Nothing here reads
    that answer as the time one — it was given to a question that did not ask.
    """
    time_row = _cost(attempt.get("claimed_complexity"), attempt.get("time_optimality"))
    space_row = _cost(attempt.get("claimed_space_complexity"), attempt.get("space_optimality"))
    if any(
        attempt.get(k)
        for k in ("time_optimality", "space_optimality", "claimed_space_complexity")
    ):
        return [row for row in (("time", time_row), ("space", space_row)) if row[1]]

    legacy = _cost(attempt.get("claimed_complexity"), attempt.get("optimality"))
    return [("approach", legacy)] if legacy else []


# --- the ways a problem can be solved --------------------------------------


#: What a recorded method costs, in the words the rest of the app already uses.
#: `None` is not "not sure": one is a question nobody has answered, the other is
#: an answer. The dash says the first and `OPTIMALITY_LABELS` says the second.
METHOD_OPTIMALITY_STYLE = {
    "optimal": "green",
    "suboptimal": "yellow",
    "unsure": "bright_black",
}


def method_row(
    name: str,
    optimality: str | None,
    *,
    wrote_it: bool = False,
    complexity: str | None = None,
    written: bool = False,
    attempt_id: int | None = None,
) -> Text:
    """One way of solving one problem, as a row.

    Shared by the prompt after a solve and the browsable screen, so the list you
    edit and the list you read back can never drift into two different shapes.

    Four columns and every one of them can be empty. A method you have only ever
    heard of is a name and three dashes, and that row is the reason the screen
    exists -- a list of just the things you have already done would have nothing
    to tell you.

    The name column is wide, because a method name is a sentence -- "sort, then
    two pointers from both ends" says what the route is and "sort, then two poi"
    says nothing. Still under 78 columns in total, because this renders in
    `p99 methods` too and a wrapped row loses the last column, which is the one
    that says whether the code is there.
    """
    line = Text("  ")
    # A pointer, not a checkbox: nothing here is toggled by pressing it, and the
    # column exists only to say which of these you wrote tonight.
    line.append("→ " if wrote_it else "  ", style="bold green" if wrote_it else "")
    line.append(name[:32], style="bold" if written else "")
    line.append(" " * max(1, 34 - len(name[:32])))

    line.append(f"{(complexity or '—')[:11]:<12}", style="bright_black")

    label = OPTIMALITY_LABELS.get(optimality or "", "—")
    line.append(f"{label:<13}", style=METHOD_OPTIMALITY_STYLE.get(optimality or "", "bright_black"))

    if written:
        line.append("code", style="green")
        if attempt_id:
            line.append(f"  #{attempt_id}", style="bright_black")
    else:
        line.append("—", style="bright_black")
    return line


def method_list(ways: Sequence[Any]) -> list[Text]:
    """A problem's whole list, for the screen that only reads it."""
    rows = [
        method_row(
            name=m.name,
            optimality=m.optimality,
            complexity=m.complexity,
            written=m.written,
            attempt_id=m.attempt_id,
        )
        for m in ways
    ]
    if not rows:
        rows.append(Text("  no methods recorded for this problem yet", style="bright_black"))
    return rows


def strategy_coverage_table(coverage: Sequence[Any], methods: Any) -> Group:
    """How wide each pattern reaches, and how much of your methods list is written.

    The table counts patterns: how many problems you have reached for each one on
    and how many solves that took. The lines underneath count problems and
    methods, which is why they sit under it rather than in it -- a different unit
    in the same column would make the table lie.

    `methods.unwritten` is the number worth reading. A route you have recorded
    and never written is one you recognise rather than one you can produce, and
    an interview asks for the second thing.
    """
    rows: list[RenderableType] = [Text("  patterns", style="bold")]
    if not coverage:
        rows.append(Text("  nothing named yet", style="bright_black"))
    else:
        header = Text("  ")
        header.append(f"{'':<28}{'problems':>9}{'solves':>9}", style="bright_black")
        rows.append(header)
        for entry in coverage:
            line = Text("  ")
            line.append(f"{entry.name[:27]:<28}")
            line.append(f"{entry.problems:>9}", style="bright_black")
            line.append(f"{entry.solves:>9}", style="bright_black")
            rows.append(line)

    rows.append(Text(""))
    if not methods or not methods.ways:
        rows.append(
            Text(
                "  no methods recorded yet — the prompt after your next solve "
                "is where this fills up",
                style="bright_black italic",
            )
        )
        return Group(*rows)

    summary = Text("  ")
    summary.append(
        f"{methods.ways} methods recorded across {methods.problems} problems, ",
        style="bright_black italic",
    )
    summary.append(
        f"{methods.unwritten} never written.",
        style="yellow italic" if methods.unwritten else "bright_black italic",
    )
    rows.append(summary)
    rows.append(
        Text(
            f"  {methods.single_route} problems have exactly one method recorded.",
            style="bright_black italic",
        )
    )
    return Group(*rows)


# --- what happened last time -----------------------------------------------
#
# Both of these render `stats.PastAttempt`, which carries no code path, no note
# path and no note text — see its docstring. Nothing here may start showing
# them: the whole point is that you can check your record on a problem without
# being handed your own old answer to it.


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def last_attempt_line(past: Sequence[PastAttempt]) -> Text:
    """One line: have I seen this, when, and how did it go.

    Always rendered, including the "no" — "first time on this one" is the same
    question answered, and a line that appears and disappears would move the
    clock underneath it every problem.
    """
    line = Text("  ")
    if not past:
        line.append("first time on this one", style="bright_black")
        return line

    last = past[0]
    line.append(f"seen {_times(len(past))} before", style="bright_black")
    line.append("  ·  ", style="bright_black")
    line.append(last.ago)
    line.append("  ·  ", style="bright_black")
    line.append(last.result, style=VERDICT_LABEL_STYLE.get(last.style_label, ""))
    line.append(f" in {fmt_duration(last.active_seconds)}", style="bright_black")
    return line


def past_attempts_panel(past: Sequence[PastAttempt]) -> RenderableType:
    """Every past attempt at the problem on screen, most recent first."""
    if not past:
        return empty_state("First time on this one — no past attempts to show.")

    head = Text("  ")
    for label, w in (("when", 10), ("result", 29), ("time", 8)):
        head.append(f"{label:<{w}}", style="bright_black")
    head.append(f"{'submits':>7}  {'score':>5}", style="bright_black")
    rows: list[RenderableType] = [head, rule()]

    for attempt in past:
        line = Text("  ")
        line.append(f"{attempt.ago:<10}")
        line.append(
            f"{attempt.result:<29}", style=VERDICT_LABEL_STYLE.get(attempt.style_label, "")
        )
        line.append(f"{fmt_duration(attempt.active_seconds):<8}")
        submits = str(attempt.submissions) if attempt.submissions else "—"
        line.append(
            f"{submits:>7}  ", style="red" if attempt.submissions else "bright_black"
        )
        line.append(
            f"{attempt.score:>5}", style="cyan" if attempt.score > 0 else "bright_black"
        )
        # Everything that has no column of its own, in the margin past the
        # score: the two facts that are worth reading and never worth a header.
        notes = ["review"] if attempt.is_review else []
        if attempt.resolves:
            # How many times you sat the problem that evening, not what you
            # wrote on the later runs -- the same line `PastAttempt` draws.
            notes.append(f"solved {attempt.resolves + 1}×")
        if attempt.self_confidence:
            notes.append(CONFIDENCE_LABELS.get(attempt.self_confidence, ""))
        if notes:
            line.append(f"  {' · '.join(notes)}", style="bright_black")
        rows.append(line)

    rows.append(rule())
    rows.append(
        Text(
            "  time and result only — the code and the note are in your history",
            style="bright_black italic",
        )
    )
    return Group(*rows)


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


def queue_row(n: int, item) -> Text:
    """One queue line: what it is, and when it was owed.

    Its own function because the queue screen hands these to a `SelectionList`
    as prompts, and a row that drifted from the one `p99 queue` prints would be
    two descriptions of the same queue.
    """
    line = Text("  ")
    line.append(f"{n} ", style="bright_black")
    line.append(mastered_prefix(getattr(item, "mastered", False)))
    title = item.title if len(item.title) <= 27 else item.title[:26] + "…"
    line.append(f"{title:<28}", style="bold" if item.is_review else "")
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
    return line


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
        rows.append(queue_row(n, item))

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


# --- mastered problems -----------------------------------------------------


def _days_ago(iso: str | None, now: datetime) -> str:
    if not iso:
        return "—"
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return "—"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    days = max(0, (now - when).days)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


#: (label, width). Wider than `WIDTH` because six columns will not fit in 62 and
#: the rules are drawn to match rather than the columns squeezed to the rule.
MASTERED_COLUMNS = (
    ("", 2),
    ("problem", 26),
    ("", 3),
    ("pattern", 18),
    ("solves", 8),
    ("lapses", 8),
    ("mastered", 10),
)
MASTERED_WIDTH = 2 + sum(w for _, w in MASTERED_COLUMNS)


def mastered_table(
    rows: Sequence[Mapping[str, Any]], catalog_size: int, now: datetime
) -> RenderableType:
    """Everything you have mastered, and what it cost to get there.

    Mastery is the one thing the scheduler does that you cannot see happen: a
    problem simply stops being offered. So it gets a page — the alternative is a
    queue that quietly narrows for reasons you have to take on faith.
    """
    if not rows:
        return empty_state(
            "Nothing mastered yet.",
            "A problem is mastered once you have recalled it across its whole "
            "ladder — one clean recall after an easy solve, four after a failed one.",
        )

    head = Text("  ")
    for label, w in MASTERED_COLUMNS:
        head.append(f"{label:<{w}}", style="bright_black")
    out: list[RenderableType] = [head, rule(width=MASTERED_WIDTH)]

    by_pattern: dict[str, int] = {}
    for row in rows:
        title = row["title"] or row["slug"]
        line = Text("  ")
        line.append(mastered_prefix(True))
        line.append(f"{title if len(title) <= 25 else title[:24] + chr(8230):<26}")
        difficulty = (row["problem_difficulty"] or "").lower()
        line.append(
            f"{difficulty[:1].upper():<3}", style=DIFFICULTY_STYLE.get(difficulty, "")
        )
        pattern = row["pattern"] or "—"
        line.append(f"{pattern if len(pattern) <= 17 else pattern[:16] + chr(8230):<18}",
                    style="bright_black")
        line.append(f"{row['reps'] or 0:<8}")
        lapses = int(row["lapses"] or 0)
        line.append(f"{lapses:<8}", style="bright_black" if not lapses else "yellow")
        line.append(f"{_days_ago(row['mastered_at'], now):<10}", style="bright_black")
        out.append(line)
        by_pattern[pattern] = by_pattern.get(pattern, 0) + 1

    out.append(rule(width=MASTERED_WIDTH))
    footer = Text("  ")
    footer.append(f"{len(rows)} mastered", style="bold")
    if catalog_size:
        footer.append(f" of {catalog_size}", style="bright_black")
    footer.append("   ", style="bright_black")
    # The patterns you have actually finished, which is the question this page
    # gets opened to answer.
    leaders = sorted(by_pattern.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
    footer.append(
        "  ".join(f"{name} {count}" for name, count in leaders), style="bright_black"
    )
    out.append(footer)
    # Kept inside `MASTERED_WIDTH` so it does not wrap under the table it
    # belongs to on an 80-column terminal.
    out.append(
        Text(
            "  Out of the daily queue for good — losing one in a mixed run puts it back.",
            style="bright_black italic",
        )
    )
    return Group(*out)


# --- history ---------------------------------------------------------------


def history_table(runs: Sequence[Run], limit: int | None = None) -> RenderableType:
    if not runs:
        return empty_state(
            "No runs logged yet.",
            f"Start one with `{branding.COMMAND}` — the first run is the baseline for every one after it.",
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
    rows.append(footer)
    return Group(*rows)
