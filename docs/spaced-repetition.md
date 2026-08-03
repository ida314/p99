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
| You said you'd have no idea / would struggle in a month (confidence ≤ 2) | `Hard` |
| Faster than 0.6× par, no help at all | `Easy` |
| Everything else | `Good` |

Par is difficulty-relative — 900s easy, 1800s medium, 2700s hard (`scoring/v1.toml`),
so difficulty is already priced in before FSRS sees anything.

Two properties are deliberate:

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

**A first failure is not a lapse.** `Again` on an existing card means you forgot
something you knew. Failing a problem you have never seen is not forgetting, it is
not knowing yet. It still rates `Again` — a day-scale interval is right either way
— but it no longer increments `lapses`. Before this, the column read as a
forgetting rate while mostly counting first contact: 9 "lapses" in 18 attempts,
almost all on problems seen once.

## The parameters

Current default is **v2**. `v1` remains selectable and unmodified.

| Setting | Value | Why |
|---|---|---|
| `weights` | the 21 published FSRS-6 values | A fitted model, not something to tune by hand. Change only by running the optimizer |
| `desired_retention` | 0.90 easy/medium, **0.92 hard** | 0.90 is the recommended default; hard problems aim higher, which is FSRS's own lever for a tighter schedule |
| `learning_steps` | **empty** | See below |
| `relearning_steps` | **empty** | See below |
| `maximum_interval` | **365 days** | A coverage guarantee. See below |

### Why the learning steps are empty

`py-fsrs` ships `learning_steps = [1min, 10min]`, which is right for a flashcard
and nonsense here — you are not re-solving a 30-minute problem sixty seconds after
finishing it. The alternative to short steps is day-scale steps, and FSRS's own
guidance recommends against any step longer than 12–14 hours. That leaves no steps
at all, which hands the short-term schedule to FSRS itself.

Empty steps put every graded attempt straight into the review state on a day-scale
interval. On a first encounter: `Again` → 1d, `Hard` → 1d, `Good` → 2d, `Easy` → 8d.

### Why hard problems use retention, not an interval multiplier

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
are the rating map, `queues.DUE_SHARE`, and `session.planned_n`. Not this.

## What was considered and left alone

- **The scheduler itself.** FSRS-6 is right; the determinism work around it is right.
- **Interleaving.** `queues.MAX_CONSECUTIVE_PATTERN = 2` stops the queue blocking
  by pattern. Interleaved practice reliably beats blocked practice for problem
  solving specifically, by training you to pair a problem with the right approach
  rather than rehearsing an approach you were already handed. Already correct.
- **The verdict ladder.** `solved_unaided` through `solved_after_implementation`
  feeds the map through `scoring.help_tier` and needs no branch of its own.
- **Fitting the weights.** `py-fsrs` ships an optimizer that trains parameters on
  your own review history, which is the right long-term answer to "these constants
  were fitted on flashcards." It is not wired up, because it cannot help yet: it
  needs `pip install "fsrs[optimizer]"` (which pulls in torch), a few hundred
  reviews to fit parameters, and at least 512 review logs to compute an optimal
  retention. There are currently 18 graded attempts. Running it now would produce
  parameters that look precise and are overfit to noise.

## When to revisit

At **~400 reviews in review state**, fitting is worth doing. Feed the log to
`fsrs.Optimizer`, take `compute_optimal_parameters()`, and write the result out as
`v3.toml`; at 512+ review logs with durations recorded, `compute_optimal_retention()`
becomes meaningful too. p99 already records everything needed — rating, timestamp
and duration are all on `attempts`. Then replay, and every card reschedules against
a model fitted on how *you* actually forget, which is the only real answer to the
caveat this document opens with.
