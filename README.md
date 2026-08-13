# p99

A local-first TUI that turns interview prep into timed, scored, permanently-recorded runs.

p99 is the 99th percentile — the tail of the latency distribution, and the thing
you're trying to become. It's also a real feature: solve times are reported as
distributions, not averages, because the interview is a single sample and the
tail is what kills you.

**This repo is Phases 1 and 2** of [the design spec](#spec). Phase 1 was the
pathetic MVP: record everything, permanently, in a form later phases can read.
Phase 2 is scheduling — problems come back on an FSRS schedule derived from your
own history, and a queue screen tells you what to do today and why. Still no
LLM and no workers; those are Phase 3.

## Install

```sh
make build
```

That creates the virtualenv, installs the package into it, links the command
onto your `PATH` and installs the man page. Defaults are `PREFIX=~/.local` and
`VENV=.venv`; override either (`make PREFIX=/usr/local build`). `make` on its
own lists every target, `make uninstall` removes the command and the man page
and leaves your runs alone.

The install is editable, so day-to-day source edits need no rebuild — only a
change to `pyproject.toml` does.

## Use

```sh
p99                 # the TUI — start a run
p99 queue           # what to do today, and why
p99 stats           # percentile distributions, sliceable
p99 history         # run rankings, you vs. your past self
p99 fetch           # cache the problem list for offline use
p99 replay          # rebuild every projection from the event log
p99 doctor          # paths, catalog, editor, cache, config
```

`p99 stats` slices:

```sh
p99 stats --pattern sliding-window
p99 stats --difficulty medium --days 30
p99 stats --by pattern            # every pattern, ranked by volume
p99 stats --tag graph
```

## The queue

`d` from home. Every problem you finish gets an [FSRS](https://github.com/open-spaced-repetition/py-fsrs)
card, graded from what actually happened — the verdict, the hint tier, and your
time against par, with the confidence you reported only breaking the tie at the
top. Giving up scores zero and still schedules a review; that is the whole
bargain.

The verdict records **how much help you needed**, not what the judge said:
solved with no help, with hints, after seeing the written description, after
seeing the pseudocode, after reading the implementation — then gave up, and not
graded. It is the same 0–4 scale as the hint tiers and is priced by the same
multipliers, so the two ways of getting help can never be double-charged and the
worse of the two always wins. Every rung still counts as solved and still earns
credit: reading the implementation after a real fight beats not logging it.

The queue puts due reviews first but never lets them take more than ~40% of it,
because falling behind on new coverage is how you end up excellent at fifteen
problems. Nothing you attempted in the last three days comes back unless it is
due, the difficulty mix targets 20/60/20, and **no pattern ever runs three deep**
— interleaving beats blocking for transfer, and it is the highest-value
scheduling rule in here. `enter` starts a run over it, `ctrl+r` rebuilds it.

Reviews score 1.25×. Retention is the thing being trained.

The cards are a projection, like everything else derived: `p99 replay` rebuilds
every one of them from the event log, so the schedule seeds itself retroactively
from history you logged before any of this existed. Swapping the parameter file
in settings and replaying reschedules all of it.

## A run

1. **Start** — pick how many problems. Selection is random-from-list or manual.
   `ctrl+a` turns [speech mode](#speech-mode) on or off for this run.
2. **Solve** — timer runs, `o` opens the problem on leetcode.com, `p` pauses,
   `?` reveals the next hint tier, `s` logs a submission, `f` finishes.
   You solve in the browser and self-report the verdict; LeetCode is the judge —
   except [offline](#offline), where there isn't one.
3. **Finish** — how much help you needed, how well it would stick cold in a
   month, and what you say the solution costs: a time complexity and a space
   complexity, each with its own *was it optimal?* answer. Two axes rather than
   one because the trade is the whole point — the hash map that buys O(n) time
   pays O(n) space for it, and one answer cannot record a decision made in two
   directions. The cost claims are recorded and shown back in history and on the
   run summary; none of it is scored and none of it moves a review, because a
   bonus set before there is data to set it from is a guess with a number on it.
   Both optimality answers default to *not sure*, separately — being certain
   about time and having never thought about space is the normal state, and the
   flattering answer is never the default here, for the same reason the verdict
   starts on the worst thing already on the record.
4. **Capture** — two `$EDITOR` handoffs: your solution, then a reflection note
   pre-filled with three questions. Both skippable with `:q!`, and skipping
   costs nothing. `s` opens a third one on the spot, for the code that just got
   rejected — a wrong answer is the only artifact of an attempt that stops
   existing the moment you fix it, and the diff against what finally passed is
   the lesson. Turn it off in settings if you would rather not be asked.
5. **Summary** — the run's death screen, scored and ranked against every past run.

A run does not have to end the day it starts. `z` **suspends** it: the problem
on screen keeps its clock reading, its hint tier and its failed-submit count,
and keeps no verdict at all, so nothing is scored and no review is scheduled for
a problem you are still in the middle of. The home screen then offers it back —
`c`, or the entry at the top of the menu, which says which problem you were on
and how long ago you put it down. Quitting hard does the same thing: closing the
terminal mid-problem suspends the run rather than recording a `gave_up` you
never meant.

Resuming hands the problem back with **the clock stopped**, because reading
yourself into a problem you last saw eight hours ago is not solve time. `p`
starts it. The time you were away is kept as its own number, next to the pause
it is not: an overnight break and four minutes at the kettle are different facts
about how a solve went, and history shows them separately.

### Keys

Navigation is vim's, on every screen. Arrows, Home/End and PageUp/PageDown
still work.

| | |
|---|---|
| `j` `k` | down, up |
| `h` `l` | left, right — between panes, between the time and space ladders at the finish prompt, or through the values of a setting |
| `gg` `G` | top, bottom |
| `ctrl+d` `ctrl+u` | half a screen |
| `ctrl+f` `ctrl+b` | a full screen |

Because those are motions everywhere, **no screen binds `j` `k` `gg` `G` to an
action**, and `h`/`l` only ever move sideways through what a screen already
shows — whatever `l` does, `h` undoes. That is the whole reason the hint key is
`?` and not `h`: a reflex `h` that reveals a hint tier is irreversible and costs
real points.

| | |
|---|---|
| `n` | new run (home) |
| `c` | resume the suspended run, on the problem it was left on (home) |
| `d` | today's queue — due reviews and new coverage (home) |
| `r` | runs — the history screen (home) |
| `t` | stats (home) |
| `s` | settings (home); `h`/`l` change a value, `x` puts it back to `config.toml` |
| `/` `i` | filter the problem list (setup); `esc` leaves the box, `esc` again leaves the screen |
| `space` | pick a problem (setup) |
| `ctrl+a` | speech mode on / off for this run (setup) |
| `o` | open the problem in the browser |
| `p` | pause / resume (paused time is logged, not hidden) |
| `c` | show / hide the problem's pattern and tags (hidden by default) |
| `r` | show / hide your past attempts at this problem — when, how long, how it ended (solve) |
| `?` | reveal next hint tier (monotonic, irreversible) |
| `s` | log a failed submit, then paste the code behind it (solve) |
| `f` | finish — verdict, confidence, time and space cost, optimality, then capture |
| `ctrl+x` | throw the attempt away from the finish prompt (nothing is recorded) |
| `x` | give up (scores 0; the attempt is still recorded) |
| `z` | suspend the run — put it down now, pick it up with `c` later (solve) |
| `d` | delete the highlighted run (history) |
| `q` | end the run / back |

## How it stores things

Two layers. An **append-only event log** is the source of truth; `sessions` and
`attempts` are **projections** rebuilt from it by `p99 replay`. The app only ever
appends. Any bug in projection logic is therefore fixable retroactively — fix
`events.apply`, replay, and all history is corrected.

Deleting is the same trick. Throwing an attempt away (`ctrl+x` at the finish
prompt) and deleting a run (`d` in history) append a **tombstone** rather than
erasing anything: the log still records exactly what happened, and the replay
skips every event addressed to the dead attempt or session. That skip is load
bearing — a tombstone sits at the *end* of the log, so applying its events and
deleting the rows afterwards would leave `fsrs_cards` shaped by a run that no
longer exists. Archived code and notes stay on disk; run numbers are positional
and close the gap.

Scores are never stored. `attempts` holds measured facts only, and the scalar is
a pure function over a versioned weights file (`src/core/data/scoring/v1.toml`),
computed at read time. Editing the weights rescores all history instantly.

`fsrs_cards` and `queues` are projections too. A card is a fold over the ratings
your finished attempts imply, so replaying the log rebuilds every one of them —
which is also why the FSRS scheduler is constructed with fuzzing **off**. It is
on by default in `py-fsrs`, and it randomizes intervals, which would make two
replays of one log disagree. `src/core/data/srs/v1.toml` holds the parameters,
versioned the same way the weights are.

Code and notes live on disk, not in SQLite, so you can `grep`, `diff`, and open
them in vim:

```
~/.config/p99/config.toml
~/.local/share/p99/p99.db
~/.local/share/p99/code/<slug>/<attempt_id>.<ext>
~/.local/share/p99/code/<slug>/<attempt_id>-wrong<n>.<ext>
~/.local/share/p99/notes/<slug>/<attempt_id>.md
~/.local/share/p99/audio/<slug>/<attempt_id>.opus
```

Settings changed in the app are `settings_changed` events layered on top of
`config.toml`, which is never rewritten — so the file keeps the comments that
explain every knob, and `x` on a settings row drops the override and takes the
file's answer back.

Environment: `P99_HOME` relocates all of the above (useful for a throwaway
profile), `P99_DB` points at a specific database, `P99_EDITOR` overrides
`$VISUAL`/`$EDITOR` for the capture steps, `P99_BROWSER` overrides how `o`
opens a problem, `P99_FFMPEG` points at the recorder speech mode spawns, and
`P99_LEETCODE_SESSION` carries the cookie that unlocks premium problems for
`p99 fetch`. `p99 doctor` prints what it resolved. The `P99_` prefix and the
directory names are both derived from one constant — see [Renaming](#renaming).

**No problem content is ever in the database.** The catalog holds title, slug,
URL, difficulty, tags and pattern — nothing else. The one copy of problem
content that exists is the offline cache below, which lives in its own directory
as files, is never read by a projection, and can be deleted at any time.

## Offline

`p99 fetch` downloads the active list's problem statements into
`~/.local/share/p99/cache/` as self-contained HTML — images inlined, official
hints folded away behind `<details>`, the starter snippet for your language,
nothing left to request. Then flip `offline` in settings and `o` opens the
cached copy instead of leetcode.com.

The whole list, every time. Not a subset you pick beforehand, because the queue
cannot know what you will need on day two of a trip — it reads live card state,
and cards move as you solve — and `n` can hand you anything in the list. A cache
missing the problem you were handed fails at the one moment nothing can be done
about it. All 150 problems cost about 3 MB, so there is nothing to ration; the
`[cache] max_mb` ceiling exists so that pointing this at a far larger catalog
truncates in a defined order rather than filling the disk.

Seven neetcode150 entries are premium-only. Without a LeetCode subscription they
can be neither cached nor opened, and `p99 doctor` names them. With one:

```sh
p99 fetch --session      # prompts for your LEETCODE_SESSION cookie, then fetches
```

Read it out of your browser's cookies for `leetcode.com` (devtools →
Application → Cookies → `LEETCODE_SESSION`). It is stored at
`~/.config/p99/session`, mode 0600, or supply it as `P99_LEETCODE_SESSION`
instead. It goes in neither `config.toml` nor the settings table on purpose: the
settings table is a projection of an append-only log, so a credential written
there could never be deleted and would survive every replay. It is never
printed, never logged, and never written into a cached page — `p99 doctor`
reports only whether one exists, and complains if the file is readable by
anyone else.

Nothing auto-detects. Captive-portal wifi answers DNS and resolves nothing, so a
guess would be wrong at exactly the moment it mattered; offline is a setting you
flip when you board.

The catch is that offline there is no judge, so a solved verdict would be a claim
nothing checked. The finish prompt defaults to the `ungraded` verdict instead:
it scores zero, counts as no kind of solve, and — alone among the verdicts —
schedules no review at all. Rating a card on an outcome nobody established is
worse than having no card, so the problem stays in the queue as if unseen, and
the cooldown still keeps it from coming back tomorrow.

## Speech mode

Off by default. Turn it on in settings, or with `ctrl+a` on the setup screen for
one run, and the app records what you say while each problem is running — one
Opus file per attempt, hung on the attempt like the archived code and the note.

```
~/.local/share/p99/audio/<slug>/<attempt_id>.opus
```

The recording tracks the clock exactly. It pauses when you press `p`, when the
`$EDITOR` handoff opens to paste a wrong answer, and while the finish prompt is
up — so the file is your solve and not the bookkeeping around it. `● REC` sits
on the clock line the whole time, because a microphone that can be on without
the screen saying so is not a thing to ship. Throwing an attempt away
(`ctrl+x`) deletes its recording; that is the one artifact a discard does not
leave on disk, since nothing would ever point at it again.

Nothing transcribes it and nothing scores it. Two of the four hint tiers already
tell you to say the answer out loud and the thing an interview actually judges
is how you talk through a problem — the cadence is the artifact.

It shells out to `ffmpeg`, which is the only requirement; without one on `PATH`
a run says so once in yellow and carries on unrecorded. `p99 doctor` reports
what it resolved. A pause stops the current segment and a resume starts a new
one rather than suspending the encoder — a stopped `ffmpeg` keeps being handed
samples it never reads, and what comes back after a resume is an overrun rather
than a continuation. The segments are joined without re-encoding at the end.
Suspending a run leaves those segments on disk unjoined, and resuming it adopts
them and keeps numbering, so a solve split across two sittings still comes back
as one recording.

Size is set by `[audio] bitrate_kbps`, also a settings knob. Mono Opus at
constrained VBR, so the number is a ceiling: 24 kbps is 10.8 MB an hour
(24000 / 8 = 3000 bytes a second), 12 kbps is half that and still perfectly
intelligible. Constrained rather than plain VBR because the two are
indistinguishable on real audio — measured over a minute of pink noise both
landed at 23.4 kbps against a 24k target — but on a sustained tone plain VBR ran
to 38.3 kbps while constrained held 24.9, and the size you were promised should
not depend on what the microphone picked up.

## Three places this deviates from the spec

Each is a case where the spec contradicts itself or contradicts this design's
own rules; the resolution is documented in the code at the point of the decision.

1. **Submission penalty.** §5 gives `submit_pen = 2 * max(0, submissions - 1)`,
   written as if `submissions` were the total number of submits. §4's schema
   defines it as *failed* submits before an accept, so the `- 1` would hand you
   one free wrong answer — invisible in the score, and invisible in the "clean
   solves" count. Every failed submit costs, which is also what §5's own worked
   example shows (`submits 2 → −4`). A clean solve therefore means zero failed
   submits, not one.

2. **Hint tiers are real, their text is not.** §15 says hints are "stubbed out"
   in Phase 1, but §13's tier-4 contract — reveal ends the attempt as `gave_up`
   — is a scoring rule, not a text-generation rule. The mechanism ships now:
   monotonic tiers, irreversible within an attempt, the event written before the
   text renders, and tier 4 ending the attempt. Only the hint text is a
   placeholder, so `max_hint_tier` is honest in history from day one.

3. **Tag mastery is computed, not stored.** §4 gives `tag_mastery` a table with
   an `ema_score` column. But that score is a function of the scoring weights,
   and this design's first rule is that scores are never stored — swap
   `v1.toml` for `v2.toml` and a projected mastery table would quietly disagree
   with every screen that recomputes. The table is gone; `stats.tag_mastery`
   computes it at read time like everything else derived.

   One thing the spec is right about and worth restating: tags get a score and
   not an FSRS card, because every problem review is already a review of all
   its tags, and scheduling on both double-counts.

## Write the notes

Notes are collected from day one even though nothing reads them until Phase 3.
Metrics can be recomputed from the event log forever; reflections cannot be
written retroactively. By the time the coach exists you want a hundred of these
sitting there, not zero.

Two sentences is plenty.

## The gate

**20 logged sessions before Phase 2.** It exists to catch the dominant failure
mode — building the tool becoming the procrastination — and `p99 history` still
counts down to it.

Phase 2 was built at 5. That was a deliberate call and it is recorded here
rather than quietly dropped, because the gate was the honest part of the plan
and the countdown is still on the screen. Nothing about the timing was load
bearing: cards are derived from the event log, so the schedule seeded itself
from the sessions that already existed the moment `p99 replay` ran, and it will
keep doing that for sessions logged from here.

The thing the gate was actually protecting is still true. If you can't hit 20
sessions, more features won't fix that.

## Renaming

The name lives in exactly two places:

- `src/core/branding.py` — display name, command name, on-disk slug, tagline,
  one-line description, and the ASCII banner.
- `pyproject.toml` — the distribution `name` and the `[project.scripts]` entry
  that installs the command.

They have to agree on the command name, and `make build` refuses to run if they
don't rather than linking a command that was never installed.

Nothing else in the package spells it out. The import package is `core`, the
stylesheet is `app.tcss`, the app class is `CoreApp`, and every user-visible
string, XDG directory and `<PREFIX>_*` environment variable is derived from
`branding.SLUG` at runtime. So a rebrand is: edit those two files, redraw the
banner, `uv pip install -e .`.

One thing a rename does *not* do for you: `branding.SLUG` is the name of
`~/.config/<slug>/` and `~/.local/share/<slug>/`, so changing it points the app
at a fresh, empty data directory. Move the old one across by hand — the
database, the `code/` archive and the `notes/` tree all live there.

The Makefile and the man page are generated from the same two files: the man
page is a template at `man/app.1.in` with `@PLACEHOLDER@` fields, rendered into
`build/<command>.1` at install time. Edit the template, never the output.

`p50`/`p90`/`p99` in `stats.py` and `render.py` are *not* branding. Those are
the 50th, 90th and 99th percentiles, and they stay correct under any project
name.

## Development

```sh
make test     # pytest
make lint     # pyflakes
make clean    # caches and the rendered man page
```

## Notes on the catalog

`src/core/data/neetcode150.json` is a hand-maintained list of the NeetCode 150:
150 problems across 18 pattern groups, tagged and difficulty-labelled, with
`blind75` membership marked where it overlaps. Blind 75 membership is
approximate — correct it in place and re-run `p99 seed`, which upserts and never
touches attempt history.

## Reviews

Problems come back on an FSRS-6 schedule, folded out of the event log. What each
setting is set to, why the finish prompt asks what it asks, and what was measured
to decide: [`docs/spaced-repetition.md`](docs/spaced-repetition.md).

## Spec

The full design lives in `~/Documents/Obsidian/personal/specs/`. Phase 1 covers
§4 (data model), §5 (scoring), §6 (percentiles), §7 (post-solve capture), and
§15.1 (build order). Phase 2 adds §8 (FSRS) and §10 stage 1 (deterministic queue
generation, no LLM). §9 and §11–§13 — coach memory, the nightly coach, the
review pipeline, real hints — are deliberately absent, and the `coach_memory`
and `jobs` tables sit empty waiting for them.
