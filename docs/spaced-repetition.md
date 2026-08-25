# Spaced repetition

How p99 decides when a problem comes back, what each setting is set to, and why.

The code is `src/core/srs.py`; the parameters are `src/core/data/srs/*.toml`.

## The model

p99 schedules with **FSRS-6**, through `py-fsrs`. FSRS models memory as three
quantities — stability (how long a memory lasts), difficulty (how hard this item
is for you), and retrievability (probability you can recall it right now) — and
schedules each review for the moment retrievability falls to a target you set.
It is the best-validated open scheduler available, benchmarked against several
million reviews, and there is no reason to write another one.

It is worth being clear about what it was fitted on, though: flashcard recall, in
events lasting seconds. p99 uses it for 30-minute constructive problem solving.
The *shape* transfers — forgetting curves, stability growth, difficulty — but the
constants were never fitted on anything like this, and that assumption is behind
most of the choices below.

## Cards are a projection

`fsrs_cards` is derived, never authoritative. `p99 replay` truncates it and
rebuilds it by folding the event log, so **card state is a pure function of the
events in order**. That is what makes a parameter change safe: edit a file, replay,
and every card reschedules as if the new settings had always been in force.

Three `py-fsrs` defaults break that guarantee and all three are overridden in
`srs.new_card` / `srs.scheduler`: interval fuzzing (calls `random()`), clock-seeded
card ids, and clock-seeded due dates. `tests/test_srs.py` guards each one. Nothing
in `srs.py` may read the clock — every timestamp arrives from the event being applied.

## The rating map

FSRS wants one of four grades per review. p99 derives it from what was measured,
in `srs.rate`, in this order:

| Condition | Grade |
|---|---|
| Not solved, or solved after seeing pseudocode or the implementation (help tier ≥ 3) | `Again` |
| Any help at all (tier ≥ 1), or slower than 1.5× par | `Hard` |
| You said the solution was beaten on time, and named no better approach | `Hard` |
| You said you'd have no idea / would struggle in a month (confidence ≤ 2) | `Hard` |
| Faster than 0.6× par, no help at all, optimal on time, priced on both axes | `Easy` |
| Everything else | `Good` |

Not a chain of early returns: each row is a floor, and the worst one wins. With
five conditions that can each cost a grade, an early return is a promotion that
skips whatever came after it — spotting the better approach yourself must not
also launder away a slow solve.

Par is difficulty-relative — 900s easy, 1800s medium, 2700s hard (`scoring/v1.toml`),
so difficulty is already priced in before FSRS sees anything.

Three properties are deliberate:

**Performance decides; the self-report only ever costs you.** A confidence rating
is given seconds after solving, with the answer still on screen. That is the
condition under which self-assessment is least reliable and most flattering —
immediate judgments of learning are measurably weak predictors next to ones made
after a delay, because in the moment you are reading working memory rather than
anything durable. So a *high* rating runs with that bias and buys nothing: it can
no longer promote anything. A *low* rating runs against it — volunteering that a
solve won't stick, right after producing one, is the case the bias does not
explain — so it is allowed to demote. This is why the finish prompt asks *"if this
came up cold in a month?"* rather than *"how well will this stick?"*: naming the
retrieval condition is the standard correction for the bias, and it costs nothing.

**Correctness, cost and implementation are three questions, not one.** Nothing
here compares your solution to a reference, because there is no reference: p99
stores no editorial and no canonical implementation, so a different route to the
same complexity costs exactly nothing and always did. What was missing was the
other half — a solution that passes every test with the wrong asymptotics has not
learned the pattern, and the pattern is what the card is scheduling. So the
optimality you answer at the finish prompt is now read, in one direction and with
one exception:

- **`suboptimal` demotes; `optimal` promotes only in combination.** Volunteering
  that you were beaten is a report worth acting on. `unsure` is the default
  answer, is the honest state of most solves, and costs nothing — if it did,
  it would stop being honest.
- **The exception is naming the better approach.** "I wrote the O(n²) and then
  saw the sliding window" is a solve that found the pattern late; "I wrote the
  O(n²) and that is where I stopped" is a solve that missed it. Only the second
  needs the problem back soon. The difference is a strategy flagged `worth
  learning` at the prompt after the verdict, and it is the whole reason that
  prompt has more than one role.
- **The third role is invisible here, on purpose.** `also works` names an equal
  alternative — "there is a monotonic stack solution and mine is a heap and both
  are fine." `srs.rate` never reads it. It is not a diagnosis of a gap, so it
  cannot buy back the `suboptimal` demote the way `worth learning` does; and it
  is not an admission of one, so it cannot cost a grade either. It goes to the
  approach library and nowhere near the scheduler.
- **Easy asks you to price it.** A solution you cannot cost is one you
  pattern-matched, and pattern-matching is what a long interval will not
  survive. Both axes, because answering time and skipping space is answering
  half the question.

**A first failure is not a lapse.** `Again` on an existing card means you forgot
something you knew. Failing a problem you have never seen is not forgetting, it is
not knowing yet. It still rates `Again` — a day-scale interval is right either way
— but it no longer increments `lapses`. Before this, the column read as a
forgetting rate while mostly counting first contact: 9 "lapses" in 18 attempts,
almost all on problems seen once.

### What the second axis cost the existing history

Measured on a copy of the real database before the change shipped — 18 runs, 35
attempts, 19 cards, on 2026-08-22:

- **Run scores: byte-identical.** `scoring.py` was not touched. The score is a
  function of measured facts, and what you claim a solution costs is not one.
- **Attempt rows: byte-identical.**
- **5 of the 19 cards moved**, every one of them Easy → Good, every one of them
  written before the question existed:

  | Problem | Due | Stability |
  |---|---|---|
  | `binary-tree-level-order-traversal` | 2026-08-16 → 2026-08-04 | 21.00 → 9.00 |
  | `climbing-stairs` | 2026-08-24 → 2026-08-12 | 21.00 → 9.00 |
  | `rotate-image` | 2026-08-21 → 2026-08-09 | 21.00 → 9.00 |
  | `remove-nth-node-from-end-of-list` | 2026-09-12 → 2026-08-27 | 38.58 → 22.93 |
  | `min-cost-to-connect-all-points` | 2026-08-25 → 2026-08-24 | 2.80 → 1.62 |

  21.00 → 9.00 is `weights[3]` → `weights[2]` exactly: the entry interval for
  Easy replaced by the one for Good. Their `rungs_left` went 1 → 2 with it, so
  each owes one more recall before it masters. Nothing was un-mastered, because
  nothing had mastered yet.

This is the regrade working as designed rather than a migration bug: `time_optimality`
is NULL on all 35 attempts, so no demote fires anywhere, and the only change is
that Easy now requires a claim nobody had been asked to make. Of the three
attempts that *were* asked, one said `optimal` and two said `not sure`.

## The parameters

Current default is **v3**. `v1` and `v2` remain selectable and unmodified.

| Setting | Value | Why |
|---|---|---|
| `weights[0..3]` | **2, 5, 9, 21** | The entry intervals in days, set by hand. See below |
| `weights[4..20]` | the published FSRS-6 values | The shape of forgetting, and it is fitted. Change only by running the optimizer |
| `desired_retention` | **0.90, flat** | What makes those four weights mean days. v2's per-difficulty table is dropped; see below |
| `learning_steps` | **empty** | See below |
| `relearning_steps` | **empty** | See below |
| `maximum_interval` | 365 days | A coverage guarantee, and now nearly moot — `[mastery]` masters cards long before anything reaches it |
| `[mastery]` | **4 / 3 / 2 / 1** | Recalls owed before a problem is mastered, by the rating it entered on. See below |

### What was wrong with v2

Not the model. The first four weights.

`weights[0..3]` are the initial stabilities for Again / Hard / Good / Easy, and
the published values are 0.212, 1.293, 2.307, 8.296 — after rounding, 1d, 1d, 2d
and 8d. So under v2 **every failure came back tomorrow**, every time, forever.
That is correct for a flashcard. For a 30-minute constructive problem attempted
three a day it is a treadmill, and with a verdict history dominated by `gave_up`
and `solved_with_hints` almost every card sat on it. Measured on the real
database on 2026-08-20: **19 cards, 18 of them due**, 15 with stability under 1.5
days, against 150 problems in the list of which 131 had never been opened.

Two things were compounding. The scheduler kept re-serving the same nineteen
problems, and `queues.DUE_SHARE = 0.4` gave those reviews `ceil(0.4 × 3) = 2` of
the three daily slots — so one new problem a day at best, against a review
backlog that regenerated itself every night.

### Why the entry intervals are set by hand

Stability is *defined* as the interval at which recall probability falls to 0.9.
So at `desired_retention = 0.9`, `weights[0..3]` are literally the first-review
intervals in days, and writing 2 / 5 / 9 / 21 there is a statement about this
domain rather than a guess at the model:

| First attempt | v2 | v3 |
|---|---|---|
| Failed — needed the solution, or a major hint | 1, 2, 6, 17, 44 | **2, 6, 18, 45, 104** |
| Hard — meaningful help, or slower than 1.5× par | 1, 5, 17, 50, 133 | **5, 17, 51, 135, 325** |
| Okay — independent, but slow or shaky | 2, 11, 46, 163 | **9, 39, 140, 365** |
| Easy — recognised the pattern, clean at interview pace | 8, 39, 153, 365 | **21, 89, 316, 365** |

(Each row is the first interval and the four that follow it under on-time `Good`
reviews. `tests/test_srs.py` asserts the four entry points.)

This contradicts the rule the other two files state — "change these only by
running the optimizer" — and the narrowness is the point. Indices 4 through 20
govern difficulty, stability growth and the decay term: they describe the *shape*
of forgetting, which is the part that transfers from flashcards, and they are
untouched. The four that were replaced are the ones that set the scale, the ones
that were fitted on sub-10-second retrieval events, and the ones that governed
every card in the database because nearly every card was still on its first rep.
When there are ~400 reviews in review state, `fsrs.Optimizer` fits all twenty-one
and supersedes this.

### Why v3 asks the same retention of every difficulty

v2 aimed at 0.92 for hard problems, which multiplies an interval by ~0.73. Under
that table `weights[0] = 2.0` would round back to 1 day for exactly the problems
this version exists to stop scheduling for tomorrow. The ladder is stated in days
and it is only those days at 0.90.

The property is also already bought elsewhere: par is difficulty-relative in
`scoring/v1.toml` — 900s easy, 1800s medium, 2700s hard — and par is what
`srs.rate` compares the clock against. A hard problem solved at hard-problem pace
should not be charged for it twice. v2 keeps its table and keeps behaving exactly
as it did.

### Mastery

A problem leaves the rotation once it has been recalled across its whole ladder.
`[mastery]` in the toml says how many recalls that is, keyed by the rating the
current run started on:

| Entered on | Recalls owed | The ladder as specified |
|---|---|---|
| Failed | 4 | 2 → 5 → 12 → 30 → mastered |
| Hard | 3 | 5 → 12 → 30 → mastered |
| Okay | 2 | 9 → 25 → mastered |
| Easy | 1 | 21 → mastered |

The day figures are the ladder as it was written down; the intervals actually
served are FSRS's, from the table above, and they run longer. So the **counter**
is the mechanism, not the days: `rungs_left` is set on the way in from the
rating, decremented by every graded review that is not a failure, and reset to
the failed ladder by one that is. Forgetting a problem is not a smaller version
of remembering it, and a card that lapses on its last rung should not be
mastered on the next solve.

Mastered is hidden, not deleted. `due_cards` stops offering it, `counts` reports
it separately, and `m` from home lists the lot; the card and its due date stay
where they were, so failing one in a mixed run puts it back on the failed ladder
with a live schedule already underneath it.

Two properties are worth naming. **This is parameters, not code** — v1 and v2
have no `[mastery]` table, `Params.rungs_for` answers `None`, and replaying under
either masters nothing, which is what made mastery addable without rescheduling
history. And **one recall masters an easy solve**, which is short: a
60-day threshold with a two-rep floor was the alternative and was declined in
favour of the ladder as specified. The safety valve is that mixed runs can still
serve a mastered problem, and losing one there un-masters it.

### The daily budget

`queues.DUE_SHARE` is gone and `session.reviews_per_day` replaces it, defaulting
to 1. A share cannot say "one": at the queue size actually used, `ceil(0.4 × 3)`
is 2. The budget the schedule is built around is two new problems and one review,
and that is a count, so it is stored as one — it does not grow with the queue,
because a share of a bigger queue was still a bigger backlog-clearing session.

When several cards are due, the slot goes to the one with the lowest
**retrievability** rather than the oldest due date. `due` alone cannot tell a card
three days past a four-day interval from one three days past a hundred-day
interval, and the first is much further gone. The rest are deferred and the queue
says how many, out loud.

The cap relaxes rather than shortening a queue: once the unseen pool is
exhausted, `_select`'s third pass drops it and the queue fills with reviews,
which is the only sensible answer when there is nothing new left to serve.

Simulated forward from the real card states on 2026-08-20, at three problems a
day, varying only the rate at which reviews grade `Again` (new problems held at
50%, which is roughly the recorded first-encounter rate):

| review `Again` rate | day 30 | day 66 | day 120 | day 250 | day 400 |
|---|---|---|---|---|---|
| 20% | 57 / 0 | 125 / 0 | 75 / 2 | 30 / 84 | **0 / 115** |
| 35% | 59 / 0 | 131 / 0 | 106 / 1 | 91 / 10 | 30 / 78 |
| 50% | 62 / 0 | 132 / 0 | 135 / 0 | 116 / 4 | 117 / 14 |

(backlog / mastered.) Three things to read out of it. All 131 unopened problems
are opened by **day 66** — that is what the budget buys, and it was the goal.
The backlog *peaks* around there and is meant to: two new cards a day against one
review is not a steady state, and it is not supposed to be until coverage is
done. And mastery cannot rescue a 50% review failure rate — if reviews are
failing half the time the answer is fewer new problems, not different parameters.

### Why the learning steps are empty

`py-fsrs` ships `learning_steps = [1min, 10min]`, which is right for a flashcard
and nonsense here — you are not re-solving a 30-minute problem sixty seconds after
finishing it. The alternative to short steps is day-scale steps, and FSRS's own
guidance recommends against any step longer than 12–14 hours. That leaves no steps
at all, which hands the short-term schedule to FSRS itself.

Empty steps put every graded attempt straight into the review state on a day-scale
interval. Under v1 and v2 that first interval came out of the published weights —
`Again` → 1d, `Hard` → 1d, `Good` → 2d, `Easy` → 8d — which is the treadmill
described above. v3 sets it directly: 2 / 5 / 9 / 21.

### Why hard problems use retention, not an interval multiplier

*A v1-versus-v2 question. v3 drops the per-difficulty split entirely, for the
reason given above, but the measurement below is why it is not coming back as a
multiplier.*

v1 implemented spec §8's "hard problems come back sooner" by multiplying the
computed due date by 0.8 after the model had spoken, with a comment claiming this
shrank the interval and never the stability.

That claim is false, and measurably so. A card whose due date is pulled in gets
reviewed *above* its target retrievability, and FSRS grants less stability for an
early review — so the shrink leaks into the model's state and compounds every rep:

| rep | stability, unshrunk | stability, ×0.8 | ratio |
|---|---|---|---|
| 2 | 10.96 | 7.32 | 0.667 |
| 4 | 162.86 | 85.99 | 0.528 |
| 6 | 1345.53 | 657.60 | **0.489** |

v2 asks for the shorter interval by moving what the model aims at instead. Raising
desired retention is FSRS's documented way to shorten intervals, and it is the
mechanism Anki uses when one deck needs a tighter schedule than another. `due` and
`stability` stay consistent, and nothing compounds. 0.92 buys roughly 0.73× the
interval of 0.90 — close to what the multiplier was reaching for.

One wrinkle worth knowing: intervals are whole days, so on a first review both
0.90 and 0.92 round to 2 days. The split resolves from the second review on
(medium 11d against hard 8d) and widens until both hit the cap.

### Why the horizon is capped, and why the number is not load-bearing

v1 inherited `maximum_interval = 36500` — a hundred years, which is the library
declining to have an opinion. Under it, six clean solves reviewed on time reach a
1346-day interval and rep eight reaches 7398. A problem that far out has left the
rotation permanently.

The temptation is to treat the cap as a workload dial. It isn't. Simulated across
all 150 problems to steady state, review demand in reviews/day:

| Again rate | uncapped | 730d | 365d | 180d | 120d |
|---|---|---|---|---|---|
| 5% | 0.37 | 0.53 | 0.75 | 1.38 | 1.79 |
| 10% | 0.74 | 1.14 | 1.54 | 2.08 | 3.16 |
| 15% | 2.65 | 2.62 | 2.79 | 5.16 | 5.31 |
| 25% | 14.48 | 14.67 | 16.90 | 18.61 | 19.55 |
| 40% | 75.05 | 74.87 | 76.64 | 75.10 | 76.14 |

Read across a row: the cap moves demand by a fraction of a review per day. Read
down a column: **the rate at which attempts grade `Again` moves it by two orders
of magnitude.** Load is driven by lapses and by cards still climbing the early
ladder, not by the tail.

So the cap answers exactly one question — how long may a problem you have nailed
go unseen? — and 365 says "a year." If review load ever needs managing, the levers
are the rating map, `session.reviews_per_day`, and `session.planned_n`. Not this.

Under v3 the cap is nearly moot: `[mastery]` masters a card after one to four
recalls, and almost nothing survives long enough to reach a 365-day interval.

## What was considered and left alone

- **The scheduler itself.** FSRS-6 is right; the determinism work around it is right.
  A fixed Leitner ladder was the alternative considered against v3 — 2→5→12→30 and
  so on, exactly as written — and was declined: FSRS reproduces the entry intervals
  exactly through `weights[0..3]`, grows them further than the ladder did afterwards,
  and keeps the stability/difficulty state that the optimizer will eventually fit.
- **Interleaving.** `queues.MAX_CONSECUTIVE_PATTERN = 2` stops the queue blocking
  by pattern. Interleaved practice reliably beats blocked practice for problem
  solving specifically, by training you to pair a problem with the right approach
  rather than rehearsing an approach you were already handed. Already correct.
- **The verdict ladder.** `solved_unaided` through `solved_after_implementation`
  feeds the map through `scoring.help_tier` and needs no branch of its own.
- **Scoring the optimality answer.** The rating moves; the score does not. A
  score is a function of measured facts, and what you say a solution costs is a
  claim rather than a measurement — a points bonus on it would be a number
  invented to look like one. The scheduler is the right consumer because it is
  already in the business of acting on self-reports it cannot verify, and it
  already reads them in one direction only.
- **Asking `solution_quality` outright.** It is derived from the optimality
  ladder and the strategy roles, at read time, like every score. A fifth radio
  set between you and the next problem would be a new question that mostly
  repeats two you just answered.
- **Fitting the weights.** `py-fsrs` ships an optimizer that trains parameters on
  your own review history, which is the right long-term answer to "these constants
  were fitted on flashcards." It is not wired up, because it cannot help yet: it
  needs `pip install "fsrs[optimizer]"` (which pulls in torch), a few hundred
  reviews to fit parameters, and at least 512 review logs to compute an optimal
  retention. There were 18 graded attempts when this was written and 27 on
  2026-08-20. Running it now would produce parameters that look precise and are
  overfit to noise — which is also the reason `weights[0..3]` are set by reasoning
  about what a day means here rather than by fitting nine reviews.

## When to revisit

At **~400 reviews in review state**, fitting is worth doing. Feed the log to
`fsrs.Optimizer`, take `compute_optimal_parameters()`, and write the result out as
`v4.toml` — including the first four, which v3 sets by hand precisely because
there was nothing to fit them on; at 512+ review logs with durations recorded, `compute_optimal_retention()`
becomes meaningful too. p99 already records everything needed — rating, timestamp
and duration are all on `attempts`. Then replay, and every card reschedules against
a model fitted on how *you* actually forget, which is the only real answer to the
caveat this document opens with.
