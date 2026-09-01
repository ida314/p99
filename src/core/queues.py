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
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import catalog, events, srs, stats
from .scoring import Weights

GENERATED_BY = "deterministic-v1"

#: Reviews lead the queue but never take it over. Falling behind on new coverage
#: because reviews piled up is how you end up excellent at fifteen problems
#: (spec §8).
#:
#: A flat count, not the share of the queue this used to be. A share cannot say
#: "one": at the queue size actually used, `ceil(0.4 * 3)` is 2, so two thirds of
#: every day went to problems already seen while 131 of the 150 had never been
#: opened. The budget the schedule is built around is 2 new and 1 review, and
#: that is a number, so it is stored as one. `session.reviews_per_day` overrides
#: it; `_select` still relaxes it rather than handing back a short queue.
REVIEWS_PER_DAY = 1

#: Days a problem is off the table after an attempt, unless FSRS says otherwise.
COOLDOWN_DAYS = 3

#: Target difficulty mix (spec §10 stage 1).
DIFFICULTY_MIX = {"easy": 0.2, "medium": 0.6, "hard": 0.2}

#: Interleaving beats blocking for transfer, and the spec calls this the single
#: highest-value scheduling constraint in the system. Two in a row is a pair;
#: three is a block.
MAX_CONSECUTIVE_PATTERN = 2

#: How many due cards reach the pool, as a multiple of the review budget. More
#: than the budget on purpose: the selector may skip the weakest card because
#: taking it would run a pattern three deep, and with a pool of exactly one it
#: would then take no review at all. Three gives it somewhere to go.
DUE_POOL_MULT = 3

#: Attempts on a pattern before its mastery score is worth ranking on. Lower
#: than the tag threshold because each problem carries exactly one pattern and
#: a dozen tags, so pattern counts climb roughly a twelfth as fast.
MIN_PATTERN_ATTEMPTS = 2


@dataclass(frozen=True)
class Item:
    slug: str
    title: str
    difficulty: str
    pattern: str | None
    #: Why it was picked: due | tail | pattern-transfer | weak-pattern |
    #: weak-tag | unseen.
    source: str
    #: Whether starting it counts as a review. Deliberately *not* `source ==
    #: "due"`: a problem pulled in as a tail driver still has a card, and the
    #: engine will record `is_review` from the card either way. Keying this off
    #: the reason it was picked would let the queue screen say "new" about
    #: something the summary then scores at 1.25x.
    is_review: bool = False
    #: Already mastered. Almost never true on a freshly generated queue --
    #: `srs.due_cards` drops mastered problems -- but a queue *reloaded* from an
    #: earlier day can hold one that has been mastered since. Carried so
    #: `render.queue_row` can star it rather than re-querying the card per row.
    mastered: bool = False
    #: Worked since this queue was drawn up. A queue is a plan for a day, and
    #: the day goes on after you open it: without this the rows you have already
    #: solved come back checked and the next run restarts them. Read at hydrate
    #: time rather than stored, for the same reason `is_review` is -- the stored
    #: row is the plan, and what you did about it is not part of the plan.
    done: bool = False
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

    @property
    def remaining(self) -> tuple[Item, ...]:
        return tuple(i for i in self.items if not i.done)

    @property
    def finished(self) -> bool:
        """Every row worked. An empty queue is not finished, it is empty."""
        return bool(self.items) and not self.remaining


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
    reviews_per_day: int = REVIEWS_PER_DAY,
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
                mastered=srs.is_mastered(card),
                due=due,
                overdue_days=overdue,
            )
        )

    # Computed before the tail drivers, which now need it: a mastered tail
    # driver is answered with an unseen problem rather than with itself.
    #
    # Every fill below walks the catalog spread across patterns, so the pool the
    # selector sees can actually satisfy the interleaving rule and the
    # difficulty mix rather than being nine arrays-hashing problems in a row.
    unseen = spread_by_pattern([p for slug, p in problems.items() if slug not in attempted])

    def add_unseen_in_pattern(pattern: str | None, source: str) -> bool:
        """Take the first unseen problem in `pattern`. True if one was added.

        The whole of the transfer rule in four lines: the thing a weak pattern
        needs is another problem of that shape, not another pass over the one
        problem of that shape you have already mastered.
        """
        if not pattern:
            return False
        for p in unseen:
            if p.pattern != pattern:
                continue
            before = len(pool)
            add(p.slug, source)
            if len(pool) > before:
                return True
        return False

    # 1. Due reviews, weakest first — `srs.due_cards` has already dropped
    #    everything mastered. `_select` enforces the actual cap; more than the
    #    budget reaches the pool for two reasons. The selector may skip its first
    #    choice because taking it would run a pattern three deep, and needs
    #    somewhere else to go. And once the catalog is exhausted there is nothing
    #    but reviews left to fill a queue with, `_select`'s third pass drops the
    #    cap accordingly, and a pool of one candidate would hand back a queue of
    #    one -- so `n` is the floor.
    for row in srs.due_cards(conn, now)[: max(reviews_per_day * DUE_POOL_MULT, n)]:
        overdue = max(0, (now - srs.parse_ts(row["due"])).days)
        add(row["slug"], "due", due=row["due"], overdue=overdue)

    # 2. Tail drivers — slow or ugly solves from the last 60 days. A mastered
    #    one is not re-served: it has been recalled across its whole ladder, and
    #    the fact that it was once slow is a fact about a pattern, not about a
    #    problem you now know. Spend the slot on an unseen problem of the same
    #    shape instead, which is the transfer this is all for.
    for slug in _tail_driver_slugs(conn, weights):
        if srs.is_mastered(srs.card_row(conn, slug)):
            driver = problems.get(slug)
            add_unseen_in_pattern(driver.pattern if driver else None, "pattern-transfer")
            continue
        add(slug, "tail")

    # 3. Unattempted problems in your weakest *patterns*. Ahead of the tag fill
    #    below because a pattern is the unit an answer transfers along, where a
    #    tag like `array` spans a dozen unrelated approaches.
    for pattern in weak_patterns(conn, weights):
        add_unseen_in_pattern(pattern, "weak-pattern")

    # 4. Unattempted problems carrying your weakest tags.
    for tag in weak_tags(conn, weights):
        for p in unseen:
            if tag in p.tags:
                add(p.slug, "weak-tag")

    # 5. Anything never seen, so a fresh catalog still fills a queue.
    for p in unseen:
        add(p.slug, "unseen")

    return pool[: 3 * n]


def weak_patterns(conn: sqlite3.Connection, weights: Weights) -> list[str]:
    """Patterns you are worst at, weakest first."""
    return [
        m.name
        for m in stats.pattern_mastery(conn, weights, min_attempts=MIN_PATTERN_ATTEMPTS)
    ]


def weak_tags(conn: sqlite3.Connection, weights: Weights) -> list[str]:
    return [m.name for m in stats.tag_mastery(conn, weights, min_attempts=3)]


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


def _select(
    pool: list[Item], n: int, reviews_per_day: int = REVIEWS_PER_DAY
) -> tuple[list[Item], bool]:
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

    The cap is now a flat count rather than a share of `n`, and one a day is the
    intended figure. Due dates are priorities, not deadlines: when six things are
    due, the answer is to spend the slot on the weakest of them -- which is the
    order `srs.due_cards` hands the pool over in -- and let the other five wait,
    not to spend the day clearing a backlog instead of covering new ground.
    """
    targets = _mix_targets(n)
    review_cap = max(0, reviews_per_day)
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


def _rationale(items: list[Item], relaxed: bool, weak: list[str], deferred: int = 0) -> str:
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
    if deferred > 0:
        # Said out loud for the same reason the relaxed interleaving is: a
        # backlog you cannot see reads as a scheduler that has lost track of it.
        bits.append(f"{deferred} more due and deferred — they keep")
    fresh = [i for i in items if not i.is_review]
    if fresh:
        bits.append(f"{len(fresh)} new")
    tail = [i for i in items if i.source == "tail"]
    if tail:
        bits.append(f"{len(tail)} from the slow tail of the last 60 days")
    transfer = [i for i in items if i.source == "pattern-transfer"]
    if transfer:
        bits.append(
            f"{len(transfer)} standing in for a mastered problem of the same shape"
        )
    weak_picks = [i for i in items if i.source in ("weak-pattern", "weak-tag")]
    if weak_picks and weak:
        bits.append(f"{len(weak_picks)} on your weakest patterns ({', '.join(weak[:3])})")

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
    reviews_per_day: int = REVIEWS_PER_DAY,
) -> Queue:
    """Build today's queue and append the `queue_generated` event.

    Takes no `rng`, unlike `catalog.pick_random`: there is nothing random left.
    Ties fall to catalog order, which is stable, so the same database and the
    same `now` always produce the same queue.
    """
    now = now or datetime.now(timezone.utc)
    date = date or today(now)
    pool = candidates(
        conn,
        n=n,
        active_list=active_list,
        weights=weights,
        now=now,
        reviews_per_day=reviews_per_day,
    )
    items, relaxed = _select(pool, n, reviews_per_day)
    weak = (weak_patterns(conn, weights) or weak_tags(conn, weights))[:3]
    # Everything the scheduler wanted today minus what the budget let through,
    # so the rationale can own the backlog instead of hiding it.
    deferred = max(0, len(srs.due_cards(conn, now)) - sum(1 for i in items if i.is_review))
    rationale = _rationale(items, relaxed, weak, deferred)

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


def _worked_since(conn: sqlite3.Connection, since: str) -> set[str]:
    """Problems finished since `since` -- the queue rows you have already done.

    `ungraded` does not count, on the same reasoning as `_attempted_slugs`:
    nothing judged it, and a crashed run sealed by `engine.recover_crashed_runs`
    must not tick a row off a queue you never worked.
    """
    return {
        r["slug"]
        for r in conn.execute(
            "SELECT DISTINCT slug FROM attempts "
            "WHERE started_at >= ? AND ended_at IS NOT NULL "
            "  AND verdict IS NOT NULL AND verdict != 'ungraded'",
            (since,),
        )
    }


def _hydrate(conn: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> Queue:
    """Rebuild a Queue from its stored row, re-reading live card state.

    The slug list is fixed by the event; the review markers are not, because a
    problem's card moves when you solve it. That keeps a queue you already
    started from claiming everything on it is still due. `done` is read the same
    way and for the same reason -- a plan drawn up this morning does not know
    what you did this afternoon.
    """
    problems = {p.slug: p for p in catalog.all_problems(conn)}
    worked = _worked_since(conn, row["created_at"])
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
                mastered=srs.is_mastered(card),
                done=slug in worked,
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
    reviews_per_day: int = REVIEWS_PER_DAY,
) -> Queue:
    """Today's queue, generating it if today has none.

    The morning queue is never empty and never a blank screen asking you to
    press a key first (spec §10 stage 2) — that rule is the difference between a
    coach you trust and one you stop opening. A queue you have finished is held
    to the same rule: finishing your work is not a reason to be shown no work.
    """
    now = now or datetime.now(timezone.utc)
    date = today(now)
    if not regenerate:
        existing = load(conn, date, now)
        # A queue you have worked through is as spent as one that was never
        # drawn: handing it back would be a page of ticked rows and nothing to
        # start. Generating here is what `ctrl+r` already does, and it settles
        # immediately -- the fresh rows are not done, so the next open returns
        # them rather than rolling again.
        if existing is not None and existing.items and not existing.finished:
            return existing
    return generate(
        conn,
        n=n,
        active_list=active_list,
        weights=weights,
        now=now,
        date=date,
        reviews_per_day=reviews_per_day,
    )
