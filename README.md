# p99

A local-first TUI that turns interview prep into timed, scored, permanently-recorded runs.

p99 is the 99th percentile — the tail of the latency distribution, and the thing
you're trying to become. It's also a real feature: solve times are reported as
distributions, not averages, because the interview is a single sample and the
tail is what kills you.

**This repo is Phase 1** of [the design spec](#spec): the pathetic MVP. No LLM,
no FSRS, no workers. What it does do is record everything, permanently, in a
form later phases can read.

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
p99 stats           # percentile distributions, sliceable
p99 history         # run rankings, you vs. your past self
p99 replay          # rebuild every projection from the event log
p99 doctor          # paths, catalog, editor, config
```

`p99 stats` slices:

```sh
p99 stats --pattern sliding-window
p99 stats --difficulty medium --days 30
p99 stats --by pattern            # every pattern, ranked by volume
p99 stats --tag graph
```

## A run

1. **Start** — pick how many problems. Selection is random-from-list or manual.
2. **Solve** — timer runs, `o` opens the problem on leetcode.com, `p` pauses,
   `?` reveals the next hint tier, `s` logs a submission, `f` finishes.
   You solve in the browser and self-report the verdict; LeetCode is the judge.
3. **Capture** — two `$EDITOR` handoffs: your solution, then a reflection note
   pre-filled with three questions. Both skippable with `:q!`, and skipping
   costs nothing.
4. **Summary** — the run's death screen, scored and ranked against every past run.

### Keys

Navigation is vim's, on every screen. Arrows, Home/End and PageUp/PageDown
still work.

| | |
|---|---|
| `j` `k` | down, up |
| `h` `l` | left, right — between panes where a screen has two |
| `gg` `G` | top, bottom |
| `ctrl+d` `ctrl+u` | half a screen |
| `ctrl+f` `ctrl+b` | a full screen |

Because those are motions everywhere, **no screen binds them to an action.**
That is the whole reason the hint key is `?` and not `h`: a reflex `h` that
reveals a hint tier is irreversible and costs real points.

| | |
|---|---|
| `n` | new run (home) |
| `r` | runs — the history screen (home) |
| `t` | stats (home) |
| `/` `i` | filter the problem list (setup); `esc` leaves the box, `esc` again leaves the screen |
| `space` | pick a problem (setup) |
| `o` | open the problem in the browser |
| `p` | pause / resume (paused time is logged, not hidden) |
| `?` | reveal next hint tier (monotonic, irreversible) |
| `s` | log a submission |
| `f` | finish — verdict, confidence, then capture |
| `x` | give up (scores 0; the attempt is still recorded) |
| `q` | end the run / back |

## How it stores things

Two layers. An **append-only event log** is the source of truth; `sessions` and
`attempts` are **projections** rebuilt from it by `p99 replay`. The app only ever
appends. Any bug in projection logic is therefore fixable retroactively — fix
`events.apply`, replay, and all history is corrected.

Scores are never stored. `attempts` holds measured facts only, and the scalar is
a pure function over a versioned weights file (`src/core/data/scoring/v1.toml`),
computed at read time. Editing the weights rescores all history instantly.

Code and notes live on disk, not in SQLite, so you can `grep`, `diff`, and open
them in vim:

```
~/.config/p99/config.toml
~/.local/share/p99/p99.db
~/.local/share/p99/code/<slug>/<attempt_id>.<ext>
~/.local/share/p99/notes/<slug>/<attempt_id>.md
```

Environment: `P99_HOME` relocates all of the above (useful for a throwaway
profile), `P99_DB` points at a specific database, `P99_EDITOR` overrides
`$VISUAL`/`$EDITOR` for the capture steps, and `P99_BROWSER` overrides how `o`
opens a problem. `p99 doctor` prints what it resolved. The `P99_` prefix and the
directory names are both derived from one constant — see [Renaming](#renaming).

**No problem content is ever stored.** The catalog holds title, slug, URL,
difficulty, tags and pattern — nothing else.

## Two places this deviates from the spec

Both are cases where the spec contradicts itself; the resolution is documented
in the code at the point of the decision.

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

## Write the notes

Notes are collected from day one even though nothing reads them until Phase 3.
Metrics can be recomputed from the event log forever; reflections cannot be
written retroactively. By the time the coach exists you want a hundred of these
sitting there, not zero.

Two sentences is plenty.

## The gate

**20 logged sessions before Phase 2.** Not negotiable. It exists to catch the
dominant failure mode — building the tool becoming the procrastination — and
`p99 history` counts down to it. If you can't hit 20 sessions with Phase 1, more
features won't fix that.

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

## Spec

The full design lives in `~/Documents/Obsidian/personal/specs/`. Phase 1 covers
§4 (data model), §5 (scoring), §6 (percentiles), §7 (post-solve capture), and
§15.1 (build order). §8–§13 — FSRS, coach memory, the nightly coach, the review
pipeline, real hints — are deliberately absent.
