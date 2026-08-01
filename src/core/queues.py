"""Queue generation (spec §10 stage 1): what to do today, and why.

Deterministic and model-free. Phase 3 bolts an LLM on top to *rank and narrate*
a pool this module chooses, but the constraints below stay here, in code, where
they can be tested -- a scheduler whose rules live in a prompt is a scheduler
that quietly stops following them.

The result goes into the log as a `queue_generated` event carrying the finished
slug list, so the queue you were given is reproducible even after the reasoning
that produced it is gone.

Named plural to leave `queue` to the standard library.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import catalog, events, srs, stats
from .scoring import Weights

GENERATED_BY = "deterministic-v1"

#: Reviews lead the queue but never take it over. Falling behind on new coverage
#: because reviews piled up is how you end up excellent at fifteen problems
#: (spec §8).
DUE_SHARE = 0.4

#: Days a problem is off the table after an attempt, unless FSRS says otherwise.
COOLDOWN_DAYS = 3

#: Target difficulty mix (spec §10 stage 1).
DIFFICULTY_MIX = {"easy": 0.2, "medium": 0.6, "hard": 0.2}

#: Interleaving beats blocking for transfer, and the spec calls this the single
#: highest-value scheduling constraint in the system. Two in a row is a pair;
#: three is a block.
MAX_CONSECUTIVE_PATTERN = 2


@dataclass(frozen=True)
class Item:
    slug: str
    title: str
    difficulty: str
    pattern: str | None
    #: Why it was picked: due | tail | weak-tag | unseen.
    source: str
    #: Whether starting it counts as a review. Deliberately *not* `source ==
    #: "due"`: a problem pulled in as a tail driver still has a card, and the
    #: engine will record `is_review` from the card either way. Keying this off
    #: the reason it was picked would let the queue screen say "new" about
    #: something the summary then scores at 1.25x.
    is_review: bool = False
    due: str | None = None
    overdue_days: int = 0


@dataclass(frozen=True)
class Queue:
    date: str
    items: tuple[Item, ...]
    rationale: str
    generated_by: str = GENERATED_BY

    @property
    def slugs(self) -> list[str]:
        return [i.slug for i in self.items]

    @property
    def due_count(self) -> int:
        return sum(1 for i in self.items if i.is_review)

    @property
    def new_count(self) -> int:
        return sum(1 for i in self.items if not i.is_review)


def today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone().date().isoformat()


# --- candidate pool --------------------------------------------------------


def _recent_slugs(conn: sqlite3.Connection, now: datetime) -> set[str]:
    cutoff = (now - timedelta(days=COOLDOWN_DAYS)).isoformat()
    return {
        r["slug"]
        for r in conn.execute(
            "SELECT DISTINCT slug FROM attempts WHERE started_at >= ?", (cutoff,)
        )
    }


def _attempted_slugs(conn: sqlite3.Connection) -> set[str]:
    """Problems something is known about, so they are no longer "unseen".

    An `ungraded` attempt does not count: nothing judged it, so it schedules no
    card. Were it counted here the problem would fall out of the unseen pool
    while never becoming due, and drop out of the queue entirely -- reachable
    only as a tail driver. A NULL verdict is an attempt still in progress and
    does count; `_recent_slugs` handles the cooldown either way, because having
    just seen a problem is true no matter how it ended.
    """
    return {
        r["slug"]
        for r in conn.execute(
            "SELECT DISTINCT slug FROM attempts WHERE verdict IS NULL OR verdict != 'ungraded'"
        )
    }


def _tail_driver_slugs(conn: sqlite3.Connection, weights: Weights, days: int = 60) -> list[str]:
    """Problems sitting in the p90+ tail of their difficulty slice.

    Spec §6: these are the highest-value review candidates in the system, which
    is exactly why a percentile is reported with the attempts that produced it
    rather than on its own.
    """
    out: list[str] = []
    for difficulty in ("easy", "medium", "hard"):
        dist = stats.distribution(
            conn, label=difficulty, difficulty=difficulty, days=days, weights=weights
        )
        out.extend(d.slug for d in dist.tail_drivers)
    return out


def spread_by_pattern(problems: list) -> list:
    """Reorder the catalog so a prefix of it is a usable candidate pool.

    Public because the offline cache walks the same order for the same reason:
    if a budget truncates it, what survives should be interleavable.

    Two constraints have to survive being truncated to `3n` entries, and the
    catalog's own order defeats both. It is grouped by pattern -- the first nine
    NeetCode entries are all `arrays-hashing` -- so a prefix in catalog order
    cannot be interleaved. And each group runs easy to hard, so taking the first
    problem of every pattern yields 18 problems with one hard among them and a
    difficulty mix that cannot be hit either.

    So: round-robin across patterns, and within each pattern round-robin across
    difficulties starting from a different one per pattern. The first pass over
    the patterns then picks up easy, medium and hard in rotation instead of 18
    easy problems.

    Fully determined by catalog order, so the result is reproducible.
    """
    order = tuple(DIFFICULTY_MIX)  # easy, medium, hard
    buckets: dict[str, list] = {}
    for p in problems:
        buckets.setdefault(p.pattern or "", []).append(p)

    for index, key in enumerate(buckets):
        by_difficulty: dict[str, list] = {}
        for p in buckets[key]:
            by_difficulty.setdefault((p.difficulty or "").lower(), []).append(p)
        # Each pattern leads with a different difficulty.
        rotation = [order[(index + k) % len(order)] for k in range(len(order))]
        rotation += [d for d in by_difficulty if d not in rotation]
        spread: list = []
        while any(by_difficulty.values()):
            for difficulty in rotation:
                if by_difficulty.get(difficulty):
                    spread.append(by_difficulty[difficulty].pop(0))
        buckets[key] = spread

    out: list = []
    while buckets:
        for key in list(buckets):
            out.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return out


def candidates(
    conn: sqlite3.Connection,
    *,
    n: int,
    active_list: str,
    weights: Weights,
    now: datetime,
) -> list[Item]:
    """The pool the selector chooses from, in priority order (spec §10 stage 1)."""
    problems = {p.slug: p for p in catalog.all_problems(conn, active_list)}
    recent = _recent_slugs(conn, now)
    attempted = _attempted_slugs(conn)
    seen: set[str] = set()
    pool: list[Item] = []

    def add(slug: str, source: str, *, due: str | None = None, overdue: int = 0) -> None:
        p = problems.get(slug)
        if p is None or slug in seen:
            return
        # The cooldown yields to FSRS: if the model says a problem is due, three
        # days is not a reason to skip it.
        if slug in recent and source != "due":
            return
        seen.add(slug)
        card = srs.card_row(conn, slug)
        if card is not None and due is None:
            due = card["due"]
            overdue = max(0, (now - srs.parse_ts(card["due"])).days)
        pool.append(
            Item(
                slug=slug,
                title=p.title,
                difficulty=p.difficulty,
                pattern=p.pattern,
                source=source,
                is_review=bool(card and card["state"] in srs.REVIEW_STATES),
                due=due,
                overdue_days=overdue,
            )
        )

    # 1. Due reviews, most overdue first, capped so they cannot eat the queue.
    cap = math.ceil(DUE_SHARE * n)
    for row in srs.due_cards(conn, now)[:cap]:
        overdue = max(0, (now - srs.parse_ts(row["due"])).days)
        add(row["slug"], "due", due=row["due"], overdue=overdue)

    # 2. Tail drivers — slow or ugly solves from the last 60 days.
    for slug in _tail_driver_slugs(conn, weights):
        add(slug, "tail")

    # Both fills below walk the catalog spread across patterns, so the pool the
    # selector sees can actually satisfy the interleaving rule and the
    # difficulty mix rather than being nine arrays-hashing problems in a row.
    unseen = spread_by_pattern([p for slug, p in problems.items() if slug not in attempted])

    # 3. Unattempted problems carrying your weakest tags.
    weak = [m.tag for m in stats.tag_mastery(conn, weights, min_attempts=3)]
    for tag in weak:
        for p in unseen:
            if tag in p.tags:
                add(p.slug, "weak-tag")

    # 4. Anything never seen, so a fresh catalog still fills a queue.
    for p in unseen:
        add(p.slug, "unseen")

    return pool[: 3 * n]


# --- selection -------------------------------------------------------------


def _mix_targets(n: int) -> dict[str, int]:
    """Apportion `n` slots across the difficulty mix, largest remainder first.

    Truncating instead would hand every slot to medium at small `n`: at n=3,
    int(0.2*3) is 0 easy and 0 hard, and the run you actually do most days would
    never contain either.
    """
    exact = {d: n * share for d, share in DIFFICULTY_MIX.items()}
    targets = {d: int(v) for d, v in exact.items()}
    # Hand out what truncation dropped, biggest fractional part first. Ties fall
    # to the mix's own order (easy, medium, hard) so this stays reproducible.
    order = sorted(exact, key=lambda d: (-(exact[d] - targets[d]), list(DIFFICULTY_MIX).index(d)))
    for d in order[: n - sum(targets.values())]:
        targets[d] += 1
    return targets


def _select(pool: list[Item], n: int) -> tuple[list[Item], bool]:
    """Take `n` from the pool honoring the review cap, the mix and interleaving.

    Returns the selection and whether interleaving had to be relaxed. Pool order
    is priority order and is never re-sorted: every constraint here only ever
    *skips* a candidate, so the highest-priority problem that fits is the one
    taken.

    The review cap is counted over the finished queue rather than over the
    due-sourced slice of the pool. Spec §8 caps what reviews "consume of the
    queue", and a tail driver you happen to have a card for consumes exactly as
    much of it as a due review does -- capping only the due slice lets reviews
    back in through the side door and hands you a queue with no new coverage in
    it, which is the outcome the cap exists to prevent.
    """
    targets = _mix_targets(n)
    review_cap = math.ceil(DUE_SHARE * n)
    taken: list[Item] = []
    used: set[str] = set()
    relaxed = False

    def blocked_by_pattern(item: Item, chosen: list[Item]) -> bool:
        if item.pattern is None:
            return False
        tail = [c.pattern for c in chosen[-MAX_CONSECUTIVE_PATTERN:]]
        return len(tail) == MAX_CONSECUTIVE_PATTERN and all(p == item.pattern for p in tail)

    def take(*, respect_reviews: bool, respect_mix: bool, respect_pattern: bool) -> None:
        nonlocal relaxed
        for item in pool:
            if len(taken) >= n:
                return
            if item.slug in used:
                continue
            if respect_reviews and item.is_review:
                if sum(1 for t in taken if t.is_review) >= review_cap:
                    continue
            if respect_mix and targets.get(item.difficulty.lower(), 0) <= 0:
                continue
            if blocked_by_pattern(item, taken):
                if respect_pattern:
                    continue
                relaxed = True
            taken.append(item)
            used.add(item.slug)
            targets[item.difficulty.lower()] = targets.get(item.difficulty.lower(), 0) - 1

    # Four passes, each dropping one constraint. The review cap and the mix are
    # targets; interleaving is nearly a law and goes last; none of them is worth
    # handing back a short queue for. A catalog you have barely started has more
    # reviews than new problems only for as long as that stays true.
    take(respect_reviews=True, respect_mix=True, respect_pattern=True)
    take(respect_reviews=True, respect_mix=False, respect_pattern=True)
    take(respect_reviews=False, respect_mix=False, respect_pattern=True)
    take(respect_reviews=False, respect_mix=False, respect_pattern=False)
    return taken, relaxed


def _rationale(items: list[Item], relaxed: bool, weak: list[str]) -> str:
    """The templated stand-in for Phase 3's written rationale (spec §10 stage 2).

    Phase 3 replaces the prose. It does not replace the decision.
    """
    if not items:
        return "Nothing to schedule — seed the catalog or log a run first."

    bits: list[str] = []
    due = [i for i in items if i.is_review]
    if due:
        oldest = max(due, key=lambda i: i.overdue_days)
        when = f"oldest overdue by {oldest.overdue_days}d" if oldest.overdue_days else "none overdue yet"
        bits.append(f"{len(due)} review{'s' if len(due) != 1 else ''} lead ({when})")
    fresh = [i for i in items if not i.is_review]
    if fresh:
        bits.append(f"{len(fresh)} new")
    tail = [i for i in items if i.source == "tail"]
    if tail:
        bits.append(f"{len(tail)} from the slow tail of the last 60 days")
    weak_picks = [i for i in items if i.source == "weak-tag"]
    if weak_picks and weak:
        bits.append(f"{len(weak_picks)} on your weakest tags ({', '.join(weak[:3])})")

    counts: dict[str, int] = {}
    for i in items:
        counts[i.difficulty.lower()] = counts.get(i.difficulty.lower(), 0) + 1
    bits.append("/".join(f"{counts.get(d, 0)}{d[0]}" for d in ("easy", "medium", "hard")))

    if relaxed:
        # Said out loud rather than swallowed: a constraint that silently stops
        # applying is worse than one that never existed.
        bits.append("the pool was too thin to interleave patterns fully")
    else:
        bits.append("no pattern runs three deep")
    return "; ".join(bits) + "."


# --- entry points ----------------------------------------------------------


def generate(
    conn: sqlite3.Connection,
    *,
    n: int,
    active_list: str,
    weights: Weights,
    now: datetime | None = None,
    date: str | None = None,
) -> Queue:
    """Build today's queue and append the `queue_generated` event.

    Takes no `rng`, unlike `catalog.pick_random`: there is nothing random left.
    Ties fall to catalog order, which is stable, so the same database and the
    same `now` always produce the same queue.
    """
    now = now or datetime.now(timezone.utc)
    date = date or today(now)
    pool = candidates(conn, n=n, active_list=active_list, weights=weights, now=now)
    items, relaxed = _select(pool, n)
    weak = [m.tag for m in stats.tag_mastery(conn, weights, min_attempts=3)][:3]
    rationale = _rationale(items, relaxed, weak)

    events.append(
        conn,
        events.QUEUE_GENERATED,
        {
            "date": date,
            "slugs": [i.slug for i in items],
            "rationale": rationale,
            "generated_by": GENERATED_BY,
            "created_at": now.isoformat(timespec="seconds"),
        },
    )
    return Queue(date=date, items=tuple(items), rationale=rationale)


def _hydrate(conn: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> Queue:
    """Rebuild a Queue from its stored row, re-reading live card state.

    The slug list is fixed by the event; the review markers are not, because a
    problem's card moves when you solve it. That keeps a queue you already
    started from claiming everything on it is still due.
    """
    problems = {p.slug: p for p in catalog.all_problems(conn)}
    items: list[Item] = []
    for slug in json.loads(row["slugs"]):
        p = problems.get(slug)
        if p is None:
            continue
        card = srs.card_row(conn, slug)
        is_review = bool(card and card["state"] in srs.REVIEW_STATES)
        overdue = 0
        if card and card["due"]:
            overdue = max(0, (now - srs.parse_ts(card["due"])).days)
        items.append(
            Item(
                slug=slug,
                title=p.title,
                difficulty=p.difficulty,
                pattern=p.pattern,
                # The reason a slug was picked is not in the event — only the
                # slug list is. Reconstructing it would mean re-running the
                # selector against today's cards and getting a different answer,
                # so the honest thing is to say the queue is what it is.
                source="queued",
                is_review=is_review,
                due=card["due"] if card else None,
                overdue_days=overdue,
            )
        )
    return Queue(
        date=row["date"],
        items=tuple(items),
        rationale=row["rationale"],
        generated_by=row["generated_by"],
    )


def load(conn: sqlite3.Connection, date: str, now: datetime | None = None) -> Queue | None:
    row = conn.execute("SELECT * FROM queues WHERE date = ?", (date,)).fetchone()
    return _hydrate(conn, row, now or datetime.now(timezone.utc)) if row else None


def ensure(
    conn: sqlite3.Connection,
    *,
    n: int,
    active_list: str,
    weights: Weights,
    now: datetime | None = None,
    regenerate: bool = False,
) -> Queue:
    """Today's queue, generating it if today has none.

    The morning queue is never empty and never a blank screen asking you to
    press a key first (spec §10 stage 2) — that rule is the difference between a
    coach you trust and one you stop opening.
    """
    now = now or datetime.now(timezone.utc)
    date = today(now)
    if not regenerate:
        existing = load(conn, date, now)
        if existing is not None and existing.items:
            return existing
    return generate(conn, n=n, active_list=active_list, weights=weights, now=now, date=date)
