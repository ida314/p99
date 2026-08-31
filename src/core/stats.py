"""Read models: percentile distributions (spec §6) and run history (spec §5).

Everything here reads projections and computes on the fly. No derived numbers
are stored, so changing the scoring weights or fixing a bug in this file
retroactively corrects every screen.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

from . import methods, scoring, strategies
from .scoring import Weights, fmt_duration

ATTEMPT_SELECT = """
SELECT a.*, p.title, p.difficulty, p.tags, p.pattern, s.started_at AS session_started_at,
       (SELECT COUNT(*) FROM resolves r WHERE r.attempt_uuid = a.uuid) AS resolves
FROM attempts a
JOIN problems p ON p.slug = a.slug
JOIN sessions s ON s.id = a.session_id
"""


def _cutoff(days: int | None) -> str | None:
    if not days:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def load_attempts(
    conn: sqlite3.Connection,
    *,
    finished_only: bool = True,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    tag: str | None = None,
    pattern: str | None = None,
    difficulty: str | None = None,
    slug: str | None = None,
    session_id: int | None = None,
    strategy: str | None = None,
) -> list[dict[str, Any]]:
    """Attempts joined with their catalog metadata, newest last."""
    where: list[str] = []
    params: list[Any] = []
    if finished_only:
        where.append("a.ended_at IS NOT NULL")
    lower = since or _cutoff(days)
    if lower:
        where.append("a.started_at >= ?")
        params.append(lower)
    if until:
        where.append("a.started_at < ?")
        params.append(until)
    if pattern:
        where.append("p.pattern = ?")
        params.append(pattern)
    if difficulty:
        where.append("p.difficulty = ?")
        params.append(difficulty)
    if slug:
        where.append("a.slug = ?")
        params.append(slug)
    if session_id is not None:
        where.append("a.session_id = ?")
        params.append(session_id)

    sql = ATTEMPT_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.started_at, a.id"

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for row in rows:
        row["tags"] = json.loads(row["tags"]) if row["tags"] else []
    if tag:
        rows = [r for r in rows if tag in r["tags"]]
    _hydrate_strategies(conn, rows)
    if strategy:
        # After hydration, like the `tag` filter above it and for the same
        # reason: the answer is not a column on `attempts`. Matched on the
        # `used` role only -- an approach you named but did not write says
        # nothing about how long writing it takes.
        rows = [r for r in rows if strategy in (r.get("used_keys") or ())]
    return rows


def _hydrate_strategies(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Attach the strategy and method answers, and the quality they imply.

    A handful of queries for the whole result set rather than a handful per row. The second
    one is deliberately unfiltered: `solution_quality` compares an attempt to
    your *previous* route to the same problem, and that attempt is usually
    outside whatever window the caller asked for -- a run summary showing
    tonight's session still has to know what you did in March.

    Every key it sets is derived. Nothing here is stored, so a change to
    `scoring.solution_quality` corrects every screen at once.
    """
    if not rows:
        return

    answers: dict[str, dict[str, list[str]]] = {}
    keys: dict[str, dict[str, list[str]]] = {}
    for r in conn.execute(
        "SELECT a.attempt_uuid AS uuid, a.role AS role, a.key AS key, s.name AS name "
        "FROM attempt_strategies a JOIN strategies s ON s.key = a.key "
        "ORDER BY s.name"
    ).fetchall():
        answers.setdefault(r["uuid"], {}).setdefault(r["role"], []).append(r["name"])
        keys.setdefault(r["uuid"], {}).setdefault(r["role"], []).append(r["key"])

    # Every `used` answer ever recorded, oldest first per problem, so "the route
    # you took last time" is a scan rather than a query per row.
    history: dict[str, list[tuple[int, frozenset[str]]]] = {}
    for r in conn.execute(
        "SELECT slug, attempt_id, key FROM attempt_strategies "
        "WHERE role = ? AND attempt_id IS NOT NULL ORDER BY attempt_id",
        (strategies.USED,),
    ).fetchall():
        seq = history.setdefault(r["slug"], [])
        if seq and seq[-1][0] == r["attempt_id"]:
            seq[-1] = (r["attempt_id"], seq[-1][1] | {r["key"]})
        else:
            seq.append((r["attempt_id"], frozenset({r["key"]})))

    # Which methods each attempt wrote, and which problems have an optimal
    # method recorded. Two queries for the whole result set, because `saw_better`
    # asks both of every row.
    written: dict[str, dict[str, list[str]]] = {}
    for r in conn.execute(
        "SELECT am.attempt_uuid AS uuid, am.key AS key, m.name AS name "
        "FROM attempt_methods am "
        "LEFT JOIN problem_methods m ON m.slug = am.slug AND m.key = am.key "
        "ORDER BY m.name"
    ).fetchall():
        entry = written.setdefault(r["uuid"], {"keys": [], "names": []})
        entry["keys"].append(r["key"])
        if r["name"]:
            entry["names"].append(r["name"])

    optimal: dict[str, set[str]] = {}
    for r in conn.execute(
        "SELECT slug, key FROM problem_methods WHERE optimality = ?",
        (methods.OPTIMAL,),
    ).fetchall():
        optimal.setdefault(r["slug"], set()).add(r["key"])

    for row in rows:
        answer = answers.get(row["uuid"], {})
        row["strategies_used"] = answer.get(strategies.USED, [])
        row["strategies_worth_learning"] = answer.get(strategies.WORTH_LEARNING, [])
        row["used_keys"] = frozenset(keys.get(row["uuid"], {}).get(strategies.USED, ()))
        wrote = written.get(row["uuid"], {"keys": [], "names": []})
        row["methods_used"] = list(wrote["names"])
        row["method_keys"] = frozenset(wrote["keys"])
        # The same two doors `srs.grade_attempt` reads, so the screen and the
        # scheduler can never disagree about whether you knew there was better.
        row["saw_better"] = bool(row["strategies_worth_learning"]) or bool(
            optimal.get(row["slug"], set()) - row["method_keys"]
        )
        # None, not the empty set, when there is no earlier answer: a first solve
        # cannot be an alternative to anything.
        prior = None
        for attempt_id, used in history.get(row["slug"], ()):
            if attempt_id >= row["id"]:
                break
            prior = used
        row["solution_quality"] = scoring.solution_quality(row, prior_used=prior)


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile. `p` in 0..100.

    Nearest-rank, not interpolated: at the sample sizes involved here an
    interpolated p99 invents a number that no attempt ever produced, and the
    entire point of §6 is that the tail values are real attempts you can name.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), -(-int(p) * len(ordered) // 100)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class TailDriver:
    slug: str
    title: str
    active_seconds: int
    reason: str

    @property
    def line(self) -> str:
        return f"{self.slug!r} ({fmt_duration(self.active_seconds)}, {self.reason})"


@dataclass(frozen=True)
class Distribution:
    """One slice of solve times, reported as a distribution rather than a mean."""

    label: str
    n: int
    p50: float | None
    p90: float | None
    p99: float | None
    par_seconds: int | None
    clean_rate: float | None
    prior_clean_rate: float | None
    tail_drivers: tuple[TailDriver, ...] = ()
    window_days: int | None = None
    min_samples: int = 20

    @property
    def reliable(self) -> bool:
        """Below the threshold p99 is one sample and is noise (spec §6)."""
        return self.n >= self.min_samples

    @property
    def clean_rate_delta(self) -> float | None:
        if self.clean_rate is None or self.prior_clean_rate is None:
            return None
        return self.clean_rate - self.prior_clean_rate


def _tail_reason(attempt: dict[str, Any]) -> str:
    bits = []
    verdict = attempt.get("verdict")
    if verdict == "gave_up":
        bits.append("gave up")
    elif verdict == "used_editorial":
        bits.append("used editorial")
    elif verdict == "ungraded":
        bits.append("not graded")
    elif verdict in scoring.VERDICT_HELP_LABELS:
        bits.append(scoring.VERDICT_HELP_LABELS[verdict])
    tier = int(attempt.get("max_hint_tier") or 0)
    if tier:
        bits.append(f"tier-{tier} hint")
    subs = int(attempt.get("submissions") or 0)
    if subs > 1:
        bits.append(f"{subs} submits")
    if not bits:
        bits.append("slow, clean")
    return ", ".join(bits)


def _clean_rate(attempts: Iterable[dict[str, Any]]) -> float | None:
    attempts = list(attempts)
    if not attempts:
        return None
    return sum(1 for a in attempts if scoring.is_clean_solve(a)) / len(attempts)


def distribution(
    conn: sqlite3.Connection,
    *,
    label: str = "ALL",
    tag: str | None = None,
    pattern: str | None = None,
    difficulty: str | None = None,
    days: int | None = 60,
    min_samples: int = 20,
    weights: Weights | None = None,
    n_tail_drivers: int = 3,
) -> Distribution:
    """Build one distribution slice, including the attempts sitting in its tail.

    Naming the tail drivers is the whole feature: a percentile without the
    attempts that produced it is a number you can't act on (spec §6).
    """
    w = weights or scoring.load_weights()
    attempts = load_attempts(
        conn, days=days, tag=tag, pattern=pattern, difficulty=difficulty
    )
    timed = [a for a in attempts if a.get("active_seconds")]
    times = [int(a["active_seconds"]) for a in timed]

    p90 = percentile(times, 90)
    drivers: list[TailDriver] = []
    if p90 is not None:
        tail = sorted(
            (a for a in timed if int(a["active_seconds"]) >= p90),
            key=lambda a: int(a["active_seconds"]),
            reverse=True,
        )
        drivers = [
            TailDriver(a["slug"], a["title"], int(a["active_seconds"]), _tail_reason(a))
            for a in tail[:n_tail_drivers]
        ]

    # Same-length window immediately before this one, for the trend arrow.
    prior_rate = None
    if days:
        prior = load_attempts(
            conn,
            since=_cutoff(days * 2),
            until=_cutoff(days),
            tag=tag,
            pattern=pattern,
            difficulty=difficulty,
        )
        prior_rate = _clean_rate(prior)

    # Par is only meaningful for a single-difficulty slice.
    par = w.par_for(difficulty) if difficulty else None

    return Distribution(
        label=label,
        n=len(timed),
        p50=percentile(times, 50),
        p90=p90,
        p99=percentile(times, 99),
        par_seconds=par,
        clean_rate=_clean_rate(attempts),
        prior_clean_rate=prior_rate,
        tail_drivers=tuple(drivers),
        window_days=days,
        min_samples=min_samples,
    )


def distributions_by(
    conn: sqlite3.Connection,
    dimension: str,
    *,
    days: int | None = 60,
    min_samples: int = 20,
    weights: Weights | None = None,
    limit: int | None = None,
) -> list[Distribution]:
    """Every non-empty slice along `dimension`.

    'pattern', 'difficulty', 'tag' — and 'strategy', which is the one slice the
    catalog did not supply. A pattern is where a problem sits in someone else's
    list; a strategy is what you actually reached for, and "how long do my
    top-down solves take" is a question only the second one can be asked.
    """
    attempts = load_attempts(conn, days=days)
    keys: list[str]
    if dimension == "strategy":
        used: dict[str, int] = {}
        for a in attempts:
            for key in a.get("used_keys") or ():
                used[key] = used.get(key, 0) + 1
        keys = [k for k, _ in sorted(used.items(), key=lambda kv: (-kv[1], kv[0]))]
    elif dimension == "tag":
        seen: dict[str, int] = {}
        for a in attempts:
            for t in a["tags"]:
                seen[t] = seen.get(t, 0) + 1
        keys = [k for k, _ in sorted(seen.items(), key=lambda kv: -kv[1])]
    elif dimension == "difficulty":
        order = {"easy": 0, "medium": 1, "hard": 2}
        keys = sorted({a["difficulty"] for a in attempts}, key=lambda d: order.get(d, 9))
    else:
        counts: dict[str, int] = {}
        for a in attempts:
            if a["pattern"]:
                counts[a["pattern"]] = counts.get(a["pattern"], 0) + 1
        keys = [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    if limit:
        keys = keys[:limit]

    kwarg = {
        "tag": "tag",
        "pattern": "pattern",
        "difficulty": "difficulty",
        "strategy": "strategy",
    }[dimension]
    return [
        distribution(
            conn,
            label=key.replace("-", " ").upper(),
            days=days,
            min_samples=min_samples,
            weights=weights,
            **{kwarg: key},
        )
        for key in keys
    ]


# --- run history -----------------------------------------------------------


@dataclass
class Run:
    session_id: int
    # The log's own name for this run. `session_id` is a projection rowid and is
    # not stable across a replay; the uuid is what an event can address.
    session_uuid: str
    started_at: str
    ended_at: str | None
    planned_n: int
    outcome: str | None
    session_note: str | None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    score: int = 0
    #: Set while the run is put down mid-flight and waiting to be resumed. Such a
    #: run is real and its finished problems count -- it just isn't over, which
    #: is why the history screen says so instead of showing a blank end time.
    suspended_at: str | None = None

    @property
    def suspended(self) -> bool:
        return bool(self.suspended_at)

    @property
    def solved(self) -> int:
        """Reached an answer, at any rung of the help ladder."""
        return sum(1 for a in self.attempts if a.get("verdict") in scoring.CLEAN_VERDICTS)

    @property
    def clean_solves(self) -> int:
        return sum(1 for a in self.attempts if scoring.is_clean_solve(a))

    @property
    def hints_used(self) -> int:
        """Attempts that needed help — a revealed hint or an admitted one."""
        return sum(scoring.help_tier(a) > 0 for a in self.attempts)

    @property
    def total_active_seconds(self) -> int:
        return sum(int(a.get("active_seconds") or 0) for a in self.attempts)

    @property
    def finished(self) -> int:
        return sum(1 for a in self.attempts if a.get("ended_at"))

    @property
    def local_date(self) -> str:
        return to_local(self.started_at).strftime("%Y-%m-%d")

    @property
    def local_end_time(self) -> str:
        if not self.ended_at:
            return "--:--"
        return to_local(self.ended_at).strftime("%H:%M")


def to_local(iso_ts: str) -> datetime:
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def fmt_ago(iso_ts: str, *, now: datetime | None = None) -> str:
    """How long ago, in the coarsest unit that still says something.

    "12d ago" is the answer to "when did I last see this"; the exact timestamp
    is not, and a date makes you do the subtraction yourself.
    """
    seconds = ((now or datetime.now(timezone.utc)) - to_local(iso_ts)).total_seconds()
    minutes = seconds / 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 60:
        return f"{int(days)}d ago"
    months = days / 30.44
    if months < 24:
        return f"{int(months)}mo ago"
    return f"{int(days / 365.25)}y ago"


def load_runs(
    conn: sqlite3.Connection,
    *,
    weights: Weights | None = None,
    include_empty: bool = False,
) -> list[Run]:
    """Every session with its attempts and its score under the current weights."""
    w = weights or scoring.load_weights()
    sessions = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    by_session: dict[int, list[dict[str, Any]]] = {}
    for attempt in load_attempts(conn, finished_only=False):
        by_session.setdefault(attempt["session_id"], []).append(attempt)

    runs: list[Run] = []
    for s in sessions:
        attempts = by_session.get(s["id"], [])
        if not attempts and not include_empty:
            continue
        run = Run(
            session_id=s["id"],
            session_uuid=s["uuid"],
            started_at=s["started_at"],
            ended_at=s["ended_at"],
            planned_n=s["planned_n"],
            outcome=s["outcome"],
            session_note=s["session_note"],
            attempts=attempts,
            suspended_at=s["suspended_at"],
        )
        run.score = sum(
            scoring.score_attempt(a, a["difficulty"], w).total
            for a in attempts
            if a.get("ended_at")
        )
        runs.append(run)
    return runs


def run_number(runs: Sequence[Run], session_id: int) -> int:
    """1-based position in chronological order — the '#47' on the death screen."""
    for i, run in enumerate(runs, start=1):
        if run.session_id == session_id:
            return i
    return len(runs) + 1


@dataclass(frozen=True)
class RunStanding:
    run_number: int
    rank: int              # 1 = best score ever
    total_runs: int
    percentile: float | None
    best_score: int
    best_run_number: int


def standing(runs: Sequence[Run], session_id: int) -> RunStanding | None:
    """Where this run sits against every past run — you vs. your past self."""
    if not runs:
        return None
    numbered = {r.session_id: i for i, r in enumerate(runs, start=1)}
    if session_id not in numbered:
        return None
    ranked = sorted(runs, key=lambda r: r.score, reverse=True)
    rank = next(i for i, r in enumerate(ranked, start=1) if r.session_id == session_id)
    total = len(runs)
    # Fraction of runs this one beat or tied.
    this = next(r for r in runs if r.session_id == session_id)
    beaten = sum(1 for r in runs if r.score < this.score)
    pct = (beaten / (total - 1) * 100) if total > 1 else None
    best = ranked[0]
    return RunStanding(
        run_number=numbered[session_id],
        rank=rank,
        total_runs=total,
        percentile=pct,
        best_score=best.score,
        best_run_number=numbered[best.session_id],
    )


# --- one problem's own history ---------------------------------------------


@dataclass(frozen=True)
class PastAttempt:
    """A finished attempt at one problem, as the solve screen plays it back.

    When it was, how long it took, how it ended, what it scored — and nothing
    else. There is deliberately no `code_path`, no `note_path` and no note
    text on this record: reopening a problem you have seen before is supposed
    to still be the problem, and your own old solution is the one spoiler this
    app could hand you for free. `history` shows those, on purpose, after the
    fact; this is the read model for the screen you sit on while solving.
    """

    ended_at: str
    active_seconds: int
    verdict: str | None
    max_hint_tier: int
    submissions: int
    self_confidence: int | None
    is_review: bool
    score: int
    #: How many further passes you took at the problem in the same sitting.
    #: A count and not the passes themselves: this screen says how the last
    #: attempt went, and what you wrote on the second run is history's to show.
    resolves: int = 0

    @property
    def ago(self) -> str:
        return fmt_ago(self.ended_at)

    @property
    def label(self) -> str:
        """The verdict, in the words it was recorded in — legacy ones included."""
        return scoring.VERDICT_LABELS.get(self.verdict or "", "UNRESOLVED")

    @property
    def _understated(self) -> bool:
        """Did the hints go further than the verdict admits to?"""
        return self.max_hint_tier > scoring.VERDICT_HELP_TIER.get(self.verdict or "", 0)

    @property
    def result(self) -> str:
        """The label, plus the hint tier when the hints went past what it admits.

        Same rule as the stat line's help label (`scoring._help_label`): a
        tier-2 hint followed by a `solved_unaided` verdict is not an unaided
        solve, and the line that reports it back to you should not say it was.
        """
        if self._understated:
            return f"{self.label} (tier {self.max_hint_tier})"
        return self.label

    @property
    def style_label(self) -> str:
        """The rung this attempt actually landed on, for `VERDICT_LABEL_STYLE`.

        The ladder is colour-coded and fades as the help increases, so an
        attempt that revealed hints it never confessed to has to fade with
        them — otherwise "SOLVED, NO HELP (tier 2)" prints in the green
        reserved for the solve you want, which is the one thing this line is
        supposed to make you notice.
        """
        if self._understated and self.verdict in scoring.CLEAN_VERDICTS:
            rung = scoring.HELP_TIER_VERDICT[min(self.max_hint_tier, 4)]
            return scoring.VERDICT_LABELS[rung]
        return self.label


def problem_history(
    conn: sqlite3.Connection,
    slug: str,
    *,
    weights: Weights | None = None,
    limit: int | None = None,
) -> list[PastAttempt]:
    """Every finished attempt at one problem, most recent first.

    The attempt currently on screen is never in here: it has no `ended_at`
    yet, and `load_attempts` only returns finished ones.
    """
    w = weights or scoring.load_weights()
    rows = list(reversed(load_attempts(conn, slug=slug)))
    if limit:
        rows = rows[:limit]
    return [
        PastAttempt(
            ended_at=row["ended_at"],
            active_seconds=int(row.get("active_seconds") or 0),
            verdict=row.get("verdict"),
            max_hint_tier=int(row.get("max_hint_tier") or 0),
            submissions=int(row.get("submissions") or 0),
            self_confidence=row.get("self_confidence"),
            is_review=bool(row.get("is_review")),
            score=scoring.score_attempt(row, row["difficulty"], w).total,
            resolves=int(row.get("resolves") or 0),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class StrategyCoverage:
    """One pattern, across every solve that reached for it.

    `problems` is how many distinct problems you have named it on and `solves` is
    how many attempts did. Breadth and depth of the same fact: a pattern you can
    name on six problems is one you see everywhere, and one you have reached for
    eleven times on two problems is one those two problems keep teaching you.
    """

    key: str
    name: str
    problems: int
    solves: int


def strategy_coverage(conn: sqlite3.Connection) -> list[StrategyCoverage]:
    """Every pattern in the vocabulary, widest first.

    Breadth and depth of the same fact: how many problems you have reached for
    this pattern on, and how many solves that took. A pattern you have named on
    six problems is one you recognise everywhere; one you have named eleven times
    across two problems is one you keep coming back to.

    Derived at read time from the projections, like everything else here -- there
    is no counter anywhere that a replay could leave stale.
    """
    return [
        StrategyCoverage(
            key=r["key"], name=r["name"], problems=r["problems"], solves=r["solves"]
        )
        for r in conn.execute(
            "SELECT s.key AS key, s.name AS name, "
            "COUNT(DISTINCT a.slug) AS problems, COUNT(*) AS solves "
            "FROM attempt_strategies a JOIN strategies s ON s.key = a.key "
            "WHERE a.role = ? "
            "GROUP BY s.key, s.name ORDER BY problems DESC, solves DESC, s.name ASC",
            (strategies.USED,),
        ).fetchall()
    ]


@dataclass(frozen=True)
class MethodCoverage:
    """The whole methods list in four numbers.

    Sits under the strategy table rather than in it, because these count problems
    and methods while that counts patterns -- putting a different unit in the same
    column would make the table lie.

    `ways - written` is the number worth reading: a route you have recorded and
    never written is one you recognise rather than one you can produce, and an
    interview asks for the second thing.
    """

    problems: int
    single_route: int
    ways: int
    written: int

    @property
    def unwritten(self) -> int:
        return self.ways - self.written


def method_coverage(conn: sqlite3.Connection) -> MethodCoverage:
    """How many ways you have recorded, and how many of them you have written."""
    row = conn.execute(
        "SELECT COUNT(*) AS ways, "
        "SUM(CASE WHEN code_path IS NULL THEN 0 ELSE 1 END) AS written, "
        "COUNT(DISTINCT slug) AS problems FROM problem_methods"
    ).fetchone()
    single = conn.execute(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT slug FROM problem_methods GROUP BY slug HAVING COUNT(*) = 1"
        ")"
    ).fetchone()
    return MethodCoverage(
        problems=int(row["problems"] or 0),
        single_route=int(single["n"] or 0) if single else 0,
        ways=int(row["ways"] or 0),
        written=int(row["written"] or 0),
    )


@dataclass(frozen=True)
class Mastery:
    """How well one slice is going. Spec §4 gives this a table; it does not get one.

    `ema_score` is a function of the score, and the score is a function of a
    versioned weights file that you are expected to edit (see the note at the
    top of `scoring`). Storing it would be the one denormalization this design
    does not allow -- swap `v1.toml` for `v2.toml` and a projected mastery table
    would quietly disagree with every screen that recomputes. So it is computed
    here, at read time, like everything else derived.
    """

    #: The tag or the pattern, depending on which reader built it. Named for
    #: neither, because the arithmetic below is the same either way and a field
    #: called `tag` holding `sliding-window` is a comment that lies.
    name: str
    attempts: int
    solved_clean: int
    ema_score: float
    last_seen: str | None

    @property
    def clean_rate(self) -> float:
        return self.solved_clean / self.attempts if self.attempts else 0.0


#: Weight of the newest attempt in the mastery EMA. 0.3 puts roughly half the
#: signal in the last two attempts, which is the point: mastery is meant to
#: track where you are now, not average away a bad month six months ago.
MASTERY_ALPHA = 0.3


def _mastery(
    conn: sqlite3.Connection,
    weights: Weights | None,
    keys_of: Callable[[dict[str, Any]], Iterable[str]],
    *,
    min_attempts: int,
    days: int | None,
) -> list[Mastery]:
    """The shared arithmetic behind `tag_mastery` and `pattern_mastery`.

    One attempt feeds every key its problem carries -- that is the reason these
    slices get a score and not an FSRS card (spec §8): every problem review is
    also a review of all its tags, and scheduling on both would double-count.
    """
    w = weights or scoring.load_weights()
    attempts = load_attempts(conn, days=days)  # oldest first, which the EMA needs

    ema: dict[str, float] = {}
    counts: dict[str, int] = {}
    clean: dict[str, int] = {}
    seen: dict[str, str] = {}

    for a in attempts:
        score = scoring.score_attempt(a, a.get("difficulty") or "medium", w).total
        is_clean = scoring.is_clean_solve(a)
        for key in keys_of(a):
            counts[key] = counts.get(key, 0) + 1
            clean[key] = clean.get(key, 0) + (1 if is_clean else 0)
            ema[key] = score if key not in ema else MASTERY_ALPHA * score + (1 - MASTERY_ALPHA) * ema[key]
            if a.get("ended_at"):
                seen[key] = a["ended_at"]

    out = [
        Mastery(
            name=key,
            attempts=counts[key],
            solved_clean=clean.get(key, 0),
            ema_score=ema[key],
            last_seen=seen.get(key),
        )
        for key in counts
        if counts[key] >= min_attempts
    ]
    # Ties broken by name so the ordering is stable — a queue built from this
    # has to be reproducible.
    return sorted(out, key=lambda m: (m.ema_score, m.name))


def tag_mastery(
    conn: sqlite3.Connection,
    weights: Weights | None = None,
    *,
    min_attempts: int = 1,
    days: int | None = None,
) -> list[Mastery]:
    """Per-tag mastery, weakest first. Spec §10 stage 1's `weak_tags`."""
    return _mastery(
        conn,
        weights,
        lambda a: a.get("tags") or (),  # already decoded by `load_attempts`
        min_attempts=min_attempts,
        days=days,
    )


def pattern_mastery(
    conn: sqlite3.Connection,
    weights: Weights | None = None,
    *,
    min_attempts: int = 1,
    days: int | None = None,
) -> list[Mastery]:
    """Per-pattern mastery, weakest first.

    The slice the queue actually wants to fill against. A pattern is the unit
    the answer transfers along -- knowing that a problem is a monotonic-stack
    problem is most of solving it -- where a tag like `array` spans a dozen
    unrelated approaches and averages out to nothing. Each problem carries
    exactly one, so unlike `tag_mastery` this partitions rather than overlaps.
    """
    return _mastery(
        conn,
        weights,
        lambda a: (a["pattern"],) if a.get("pattern") else (),
        min_attempts=min_attempts,
        days=days,
    )



@dataclass(frozen=True)
class Overview:
    total_runs: int
    total_attempts: int
    clean_solves: int
    total_active_seconds: int
    distinct_slugs: int
    catalog_size: int
    best_score: int
    last_run_at: str | None


def overview(conn: sqlite3.Connection, weights: Weights | None = None) -> Overview:
    runs = load_runs(conn, weights=weights)
    attempts = [a for r in runs for a in r.attempts if a.get("ended_at")]
    catalog_size = int(conn.execute("SELECT COUNT(*) AS n FROM problems").fetchone()["n"])
    return Overview(
        total_runs=len(runs),
        total_attempts=len(attempts),
        clean_solves=sum(1 for a in attempts if scoring.is_clean_solve(a)),
        total_active_seconds=sum(int(a.get("active_seconds") or 0) for a in attempts),
        distinct_slugs=len({a["slug"] for a in attempts}),
        catalog_size=catalog_size,
        best_score=max((r.score for r in runs), default=0),
        last_run_at=runs[-1].started_at if runs else None,
    )
