# Problem types and problem sets

How p99 knows what to ask after a solve, and where the problems come from.

The code is `src/core/problemtypes.py` and `src/core/catalog.py`; the bundled
types are `src/core/data/types/*.toml`.

## Two things, and how they fit

A **type** is what kind of thing a problem is: what the prompt after a solve
asks, which screens follow it, how the answers move a review, and what par is.
LeetCode is a type. System design is a type.

A **set** is a list of problems, and names the one type every problem in it is.
`neetcode150` is a set of LeetCode problems.

Everything else is shared, which is the point of having types at all. There is
one event log, one `attempts` table, one `fsrs_cards` table and one rating map.
A system design prompt gets an FSRS card exactly the way `two-sum` does, comes
due the same way and masters the same way. **A new type is a TOML file, not a
second scheduler.**

## Adding a new kind of problem, start to finish

Four steps, and the first is skipped when the type already exists — a second
list of LeetCode problems is steps 2 to 4. The example is distributed systems.

**1. Write the type.** Start from the one closest to what you want:

```sh
p99 types --new distributed-systems --from system-design
```

That writes `~/.config/p99/types/distributed-systems.toml`: a copy of the
`system-design` type, comments and all, with `label` changed to the new name.
Open it and make it ask what you would ask yourself afterwards — change the
[fields](#fields), switch [screens](#the-whole-format) on or off, set par. Then
read it back:

```sh
p99 types distributed-systems
```

which prints every field, where its answer is kept, what it does to a review,
and **anything in the file it could not make sense of**. Fix what it names and
run it again; it exits non-zero until the file is clean.

**2. Write the set.** A JSON list, wherever you like. Beside the config is the
obvious place — this one is `~/.config/p99/sets/distributed-systems.json`:

```json
[
  { "title": "Design a consensus log", "difficulty": "hard",
    "pattern": "consensus", "url": "prompts/consensus-log.md" },
  { "title": "Design a distributed lock", "pattern": "coordination" },
  { "title": "Design leader election", "difficulty": "easy",
    "pattern": "consensus" }
]
```

Only `title` is required. `url` is what `o` opens, and with no `://` it is a
file relative to the list — here `~/.config/p99/sets/prompts/consensus-log.md`.

**3. Name the set in `~/.config/p99/config.toml`**, and say which type it is:

```toml
[sets.distributed-systems]
type = "distributed-systems"
path = "sets/distributed-systems.json"
queue_n = 1
```

**4. Check it, and run it.**

```sh
p99 seed       # every set, how many problems it held, anything left out
p99 doctor     # the `sets` and `types` rows, and what is wrong in words
```

Then in the app: `q` opens the queue, `l` steps across to
`distributed-systems`, `enter` starts the run. To have the app *open* on the
new list instead, set *problem list* on the settings screen, or
`session.active_list` in the config.

Nothing here needs a restart of anything but the app, and nothing needs
`p99 replay` — until you change a `review` rule on a type you have already
logged attempts under. Then a replay is what regrades them.

The rest of this page is the reference for each of those steps.

## Adding a set

Name it under `[sets]` in `~/.config/p99/config.toml`:

```toml
[sets.system-design]
type = "system-design"            # a bundled type, or one in types/
path = "sets/system-design.json"  # relative to config.toml; ~ works too
queue_n = 1                       # optional: this set's own queue size
reviews_per_day = 1               # optional: and its own review budget
```

It is picked up the next time the app opens. `p99 seed` does it now and says
what it found — how many problems each set held, and any entry it left out.

Leave `path` out to use a list bundled with p99. There are two: `neetcode150`,
which is always there, and `system-design`, a dozen prompts to get started on.
So the shortest way to see a second type working is:

```toml
[sets.system-design]
type = "system-design"
```

### What a set's file holds

A JSON list. Only `title` is required:

```json
[
  {
    "slug": "rate-limiter",
    "title": "Design a rate limiter",
    "url": "prompts/rate-limiter.md",
    "difficulty": "easy",
    "pattern": "coordination",
    "tags": ["rate-limiting", "caching"]
  },
  { "title": "Design a parking lot" }
]
```

| Key | Default | |
|---|---|---|
| `title` | — | Required. |
| `slug` | the title, hyphenated | Lowercase letters, digits and hyphens. It names a directory under `code/` and `notes/`. |
| `url` | nothing to open | What `o` opens. With no `://` it is a **file**: `~` is your home, and anything relative is relative to the set's own file, so a set and its prompts can be moved together. |
| `difficulty` | `medium` | `easy`, `medium` or `hard`. |
| `pattern` | none | The unit the queue interleaves and ranks weakness along. |
| `tags` | none | |
| `lists` | the set's name | Other lists the problem is also in. The set's own name is always one of them. |

The catalog rule has not moved: **metadata only, never content.** A prompt's
text lives in the file `url` points at, not in the database.

### When two sets claim one slug

- **Same type:** one problem in two lists. One card, one history, the first
  set's wording. This is what `blind75` has always been to `neetcode150`.
- **Different types:** a collision. The first set keeps the slug; the second's
  entry is left out and named by `p99 seed` and `p99 doctor`. `design-twitter`
  is already a LeetCode problem, and letting a system design list take the slug
  would hand that problem's whole history to a different question. Give the
  entry a `slug` of its own.

Sets are read in order: the bundled one first, then yours in the order
`config.toml` names them.

### A set that will not load

Costs that set and nothing else. A path that does not exist, or a file that is
not JSON, is reported by `p99 doctor` and on launch — it never stops a run.

## Running from a set

Each set is its own track, with its own queue for the day.

- The queue (`q`) and the setup screen (`n`) open on `session.active_list`.
- **`h` and `l` step across the lists** on both. Nothing is written: stepping is
  a way of looking, and `session.active_list` — the settings screen's *problem
  list* — is still what the app opens on.
- `queue_n` and `reviews_per_day` on a set size its queue on their own. Three
  LeetCode problems is an evening; three system designs is not.
- On the setup screen your picks come with you across lists, so one run can
  hold a LeetCode warm-up and a design. The run loop asks each *problem* what
  type it is, not the run.

Anything that ranks — the slow tail, the weakest pattern, the stats screen —
ranks within one type. A forty-minute design and a twelve-minute LeetCode
problem are both `medium`, and a percentile across the two is a number about
neither. On the stats screen `y` steps to the next type.

## Writing a type

```sh
p99 types                          # every type, what it asks, where it came from
p99 types --new system-design      # copy the bundled one out to edit
p99 types --new behavioural --from system-design
```

`--new` writes `~/.config/p99/types/<name>.toml`, and refuses to write over one
that is already there. **A file there wins over a bundled type of the same
name**, which is how you change what LeetCode asks without touching the
package. Delete the file to go back.

With no `--from`, a name that is also a bundled type starts as that type and
any other name starts as `leetcode`. Copied under a *new* name, the file's
`label` is changed to match and nothing else is — two types that both called
themselves "system design" could not be told apart on the stats screen.

You do not have to use `--new` at all. Any `<name>.toml` in that directory is a
type called `<name>`.

Edits are picked up by the next thing that reads the type. No restart.

### The whole format

Every key is optional. An empty file is a type that asks for the verdict and
the recall question and nothing else.

```toml
label = "system design"          # how the type is named on screen

# Which rungs of the help ladder the verdict prompt offers, in radio order.
# The words are the ladder's own; at least one solved rung must be on it.
verdicts = ["solved_unaided", "solved_with_hints", "gave_up", "ungraded"]

[solve]
judge = false        # true: `s` logs a failed submit, and offline the verdict
                     #       defaults to "not graded"
fetch = "leetcode"   # where `p99 fetch` downloads statements from. Only
                     # "leetcode" exists; leave it out for nothing to cache
prompt = "design it on a whiteboard — f when you are done"

[par_seconds]        # what the clock is measured against, per difficulty.
easy = 1800          # Anything left out is inherited from the scoring weights.
medium = 2700
hard = 3600

[base]               # base points per difficulty. Same inheritance.
hard = 100

[screens]            # the prompts after the verdict. Each is on unless
patterns = true      # turned off here.
methods = false
solution = true      # the $EDITOR handoff that archives what you wrote
note = true          # the $EDITOR handoff for the reflection note
again = false        # the offer of a second pass

[capture]
language = "markdown"                   # what the solution is archived as.
                                        # Omit to follow [capture] language
prompt = "write the design up below."   # the last line of the buffer's header
note_template = """
<!-- {app} reflection | {slug} -->
## what would fall over first
"""

[ai]
copy = true                    # ctrl+y on the verdict prompt
prompt = "Here is my design…"  # omit to use [ai] post_solve_prompt
paste_into = "your assistant"
group = "what you covered — optional"   # the row the button joins; omit for
                                        # a row of its own

[[fields]]                     # one table per question, in the order asked
key = "requirements"
kind = "fraction"
label = "coverage"
group = "what you covered — optional"
```

`[strategy] enabled` and `[capture] enabled` in `config.toml` still switch their
screens off for every type at once. A type can only turn a screen off that the
config left on.

### Fields

In the order they are asked. Fields that share a `group` sit side by side under
that one label, three to a row.

```toml
[[fields]]
key = "requirements"          # how the answer is stored. Lowercase, digits, _
kind = "fraction"
label = "coverage"            # what the stat line calls it: 11 characters
group = "what you covered — optional"
placeholder = "requirements   7/10"
review = { again_below = 0.4, hard_below = 0.7, easy_from = 0.9 }
```

| `kind` | You type | Stored as | |
|---|---|---|---|
| `text` | a line | the text | |
| `number` | a number | a float | `min` and `max` clamp it rather than refuse it |
| `fraction` | `7/10`, `7 of 10`, `70%`, `0.7` | **what you typed** | Measured on read, so a fix to the parser corrects every answer already in the log. A bare `7` is not read: seven of how many is the half that was left out |
| `choice` | one rung of a ladder | the option's `value` | Needs `options`, at least two |

A `choice` field's options:

```toml
[[fields]]
key = "design"
kind = "choice"
default = "unsure"
options = [
  { value = "sound",  label = "sound",    report = "would hold up" },
  { value = "flawed", label = "flawed",   report = "has a hole in it" },
  { value = "unsure", label = "not sure" },
]
review = { hard_on = ["flawed"], easy_on = ["sound"] }
```

`label` is the word on the form and has **14 characters**; `report` is the
longer wording the stat line uses, and has 26. With no `default` the cursor
starts on the **last** option — the first rung of a ladder written best-to-worst
is the flattering one, and the flattering answer is never the default here.

### How an answer moves a review

`review` on a field. Leave it out and the answer is recorded, shown back, and
moves nothing — which is what most fields should do.

| Rule | On | Means |
|---|---|---|
| `again_on = [...]` | `choice` | these answers fail the attempt |
| `hard_on = [...]` | `choice` | these demote it to Hard |
| `easy_on = [...]` | `choice` | Easy requires one of these |
| `again_below = n` | `number`, `fraction` | under this fails the attempt |
| `hard_below = n` | `number`, `fraction` | under this is Hard |
| `easy_from = n` | `number`, `fraction` | Easy requires at least this |
| `easy_needs_answer = true` | any | Easy requires the field was answered |
| `unless_better_known = true` | any | waives the Hard demotion when the methods page holds an optimal method you did not write |

A `fraction` is held against its thresholds as 0–1; a `number` as itself.

These rules are the *second half* of one rating map. The first half is measured
and is the same for every type — see `srs.rate`:

| Grade | Reached by |
|---|---|
| `Again` | not solved, or help tier ≥ 3 — **or an answer the type says fails** |
| `Hard` | any help, slower than 1.5× par, recall confidence ≤ 2 — **or an answer the type says demotes** |
| `Easy` | faster than 0.6× par, no help — **and every answer the type requires of Easy** |
| `Good` | everything else |

Three properties are deliberate:

**An unanswered question is not an answer.** A field you left blank fires no
rule and meets no requirement. It cannot cost you a grade, and it cannot earn
Easy.

**Claims move reviews. They never move the score.** The run score stays a
function of what was measured — the verdict, the clock, the hints — for every
type. What you say about the result is a claim, and claims are what the
schedule is for.

**The type is read when the card is graded, not when the answer was given.**
`attempts` holds the answers; the type says what they mean. Edit a rule, run
`p99 replay`, and every card is regraded under it — the same bargain
`scoring/v1.toml` and `srs/v3.toml` make.

### When a file is partly wrong

A broken type must never stop a run.

- A file that will not parse falls back to the bundled type of that name, or to
  a bare one that asks nothing.
- A field that makes no sense — a reserved key, an unknown kind, a ladder with
  one option — is dropped, and the rest of the file stands.
- A label too long for its column is kept, and drawn cut off.

What was wrong is said by `p99 types` and `p99 doctor`, in words.

## Where the answers are kept

| | |
|---|---|
| `problems.type` | which type a problem is. Stamped by the set that seeded it |
| `attempts.answers` | a JSON object of everything the type asked, keyed by field |
| `resolves.answers` | the same, for a second pass |

One column rather than one per question, because the questions are a TOML file
you are expected to edit. **Adding a field never needs a migration.**

Seven keys are the exception. They are LeetCode's, they predate types, and each
has a column of its own on `attempts`:

```
claimed_complexity   claimed_space_complexity
time_optimality      space_optimality      code_style
lc_runtime_pct       lc_memory_pct
```

The log already holds events carrying them at the top level of the payload, so
they stay where they are. A type of your own may ask for one by name and gets
the column, and the stat line's `time` / `space` / `code style` rows, with it.

An answer outlives the field that asked for it. Drop a field from a type and
its answers stay in the log and stop being drawn; put it back and the rows come
back with it.
