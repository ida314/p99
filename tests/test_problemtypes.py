"""Problem types: what a type asks, and what the answers do to a review."""

from __future__ import annotations

import json

import pytest
from fsrs import Rating

from core import capture, events, paths, problemtypes, render, scoring, srs
from core.catalog import Problem, ProblemSet
from core.engine import RunEngine

WEIGHTS = scoring.load_weights()

SOLVED = {"verdict": "solved_unaided", "max_hint_tier": 0, "self_confidence": 3}


def _write_type(name: str, text: str) -> None:
    paths.types_dir().mkdir(parents=True, exist_ok=True)
    problemtypes.user_path(name).write_text(text)


# --- the bundled types -----------------------------------------------------


def test_leetcode_asks_what_the_finish_prompt_always_asked():
    """The form moved into a file. What it asks did not move at all."""
    leetcode = problemtypes.load("leetcode")
    assert [f.key for f in leetcode.fields] == list(problemtypes.COLUMN_KEYS)
    assert [g.label for g in leetcode.groups()] == [
        "what your solution costs — optional",
        "how did it come out?",
        "leetcode percentiles — optional",
    ]
    assert leetcode.judge and leetcode.fetch == "leetcode"
    assert leetcode.screens == problemtypes.Screens()  # every one of them on
    assert leetcode.verdicts == scoring.VERDICTS
    assert not leetcode.problems


def test_every_ladder_defaults_to_not_sure():
    """The flattering answer is never the default — in either bundled type."""
    for name in problemtypes.bundled():
        for entry in problemtypes.load(name).fields:
            if entry.kind == problemtypes.CHOICE:
                assert entry.values[entry.default_index] == "unsure", (name, entry.key)


def test_a_ladder_with_no_default_starts_on_its_last_rung():
    entry = problemtypes.parse(
        "x", {"fields": [{"key": "went", "kind": "choice", "options": ["well", "badly"]}]}
    ).field("went")
    assert entry.values[entry.default_index] == "badly"


def test_no_answer_on_either_form_is_too_long_to_fit():
    """A third column of the box leaves fourteen characters; longer is cut off."""
    for name in problemtypes.bundled():
        for entry in problemtypes.load(name).fields:
            for option in entry.options:
                assert len(option.label) <= problemtypes.MAX_OPTION_LABEL
                # And what the stat line says fits its own column.
                assert len(entry.report(option.value)) <= problemtypes.MAX_STAT_DETAIL
            # As does what the stat line calls the field. "requirements" is
            # twelve, which is how this assertion came to exist.
            if entry.key not in problemtypes.COLUMN_KEYS:
                assert len(entry.label) <= problemtypes.MAX_STAT_LABEL, (name, entry.key)
        assert not problemtypes.load(name).problems


def test_system_design_asks_about_coverage_not_cost():
    design = problemtypes.load("system-design")
    keys = {f.key for f in design.fields}
    assert "requirements" in keys
    assert not keys & set(problemtypes.COLUMN_KEYS)
    assert not design.judge and not design.fetch
    assert design.screens.patterns and not design.screens.methods
    assert design.language == "markdown"
    assert "solved_after_pseudocode" not in design.verdicts


def test_a_field_id_is_its_key_hyphenated():
    assert problemtypes.load("leetcode").field("time_optimality").widget_id == "time-optimality"


# --- finding one -----------------------------------------------------------


def test_your_own_file_wins_over_the_bundled_one(isolated_home):
    _write_type("leetcode", 'label = "mine"\n[[fields]]\nkey = "felt"\nkind = "text"\n')
    mine = problemtypes.load("leetcode")
    assert mine.title == "mine"
    assert [f.key for f in mine.fields] == ["felt"]
    assert mine.source == str(problemtypes.user_path("leetcode"))


def test_an_edit_is_picked_up_without_a_restart(isolated_home):
    _write_type("drills", 'label = "first"\n')
    assert problemtypes.load("drills").title == "first"
    _write_type("drills", 'label = "second, and longer"\n')
    assert problemtypes.load("drills").title == "second, and longer"


def test_a_file_that_will_not_parse_falls_back_and_says_so(isolated_home):
    """A broken type must never stop a run."""
    _write_type("leetcode", "this is [not toml\n")
    fallen = problemtypes.load("leetcode")
    assert fallen.source == "bundled"
    assert [f.key for f in fallen.fields] == list(problemtypes.COLUMN_KEYS)
    assert any("could not be read" in line for line in fallen.problems)


def test_a_type_nobody_wrote_asks_nothing(isolated_home):
    missing = problemtypes.load("nope")
    assert missing.missing
    assert missing.fields == ()
    assert missing.verdicts == scoring.VERDICTS  # and can still be finished
    assert any("no type called `nope`" in line for line in missing.problems)


def test_available_lists_the_bundled_types_and_yours(isolated_home):
    _write_type("drills", 'label = "drills"\n')
    assert problemtypes.available() == ["drills", "leetcode", "system-design"]


def test_the_name_is_read_the_way_a_file_is_named(isolated_home):
    assert problemtypes.load("System Design").name == "system-design"
    assert problemtypes.load(None).name == problemtypes.DEFAULT_TYPE


# --- a file that is partly wrong -------------------------------------------


def test_a_field_that_makes_no_sense_is_dropped_and_the_rest_stands():
    parsed = problemtypes.parse(
        "x",
        {
            "fields": [
                {"key": "verdict", "kind": "text"},          # the app's own
                {"key": "Bad Key", "kind": "text"},          # not a key
                {"key": "shape", "kind": "hexagon"},         # not a kind
                {"key": "lonely", "kind": "choice", "options": ["only"]},
                {"key": "kept", "kind": "text"},
                {"key": "kept", "kind": "number"},           # twice
            ]
        },
    )
    assert [f.key for f in parsed.fields] == ["kept"]
    assert parsed.field("kept").kind == problemtypes.TEXT
    assert len(parsed.problems) == 5


def test_a_rule_that_cannot_apply_is_said_and_ignored():
    parsed = problemtypes.parse(
        "x",
        {
            "fields": [
                {
                    "key": "went",
                    "kind": "choice",
                    "options": ["well", "badly"],
                    "review": {"hard_on": ["terribly"], "hard_below": 0.5},
                },
                {"key": "said", "kind": "text", "review": {"again_below": 1}},
            ]
        },
    )
    assert parsed.field("went").review.hard_below is None
    assert parsed.field("said").review.again_below is None
    assert len(parsed.problems) == 3


def test_a_ladder_you_cannot_succeed_on_is_refused():
    """Every attempt at such a type would fail, whatever happened."""
    parsed = problemtypes.parse("x", {"verdicts": ["gave_up", "ungraded"]})
    assert parsed.verdicts == scoring.VERDICTS
    assert parsed.problems


def test_a_label_too_long_for_the_stat_line_is_said():
    parsed = problemtypes.parse(
        "x", {"fields": [{"key": "requirements", "kind": "fraction"}]}
    )
    assert parsed.field("requirements") is not None  # kept, and drawn cut off
    assert any("cut off on the stat line" in line for line in parsed.problems)


def test_a_row_wraps_at_three():
    fields = [
        {"key": f"q{i}", "kind": "choice", "group": "how", "options": ["a", "b"]}
        for i in range(4)
    ]
    rows = problemtypes.parse("x", {"fields": fields}).groups()
    assert [len(r.fields) for r in rows] == [3, 1]
    assert [r.labelled for r in rows] == [True, False]


def test_a_field_with_no_group_gets_a_row_of_its_own():
    rows = problemtypes.parse(
        "x",
        {"fields": [{"key": "one", "kind": "text"}, {"key": "two", "kind": "text"}]},
    ).groups()
    assert [r.label for r in rows] == ["one", "two"]


# --- reading an answer -----------------------------------------------------


@pytest.mark.parametrize(
    "typed, share",
    [
        ("7/10", 0.7),
        ("7 of 10", 0.7),
        ("7 out of 10", 0.7),
        ("70%", 0.7),
        ("0.7", 0.7),
        ("10/10", 1.0),
        ("11/10", 1.0),   # counted one the reference did not list
        ("0/4", 0.0),
    ],
)
def test_a_share_is_read_however_it_was_typed(typed, share):
    assert problemtypes.fraction(typed) == pytest.approx(share)


@pytest.mark.parametrize("typed", ["7", "lots", "", None, "3/0", "-1/4"])
def test_a_share_that_cannot_be_read_is_not_guessed_at(typed):
    """Seven of how many is the half of the answer that was left out."""
    assert problemtypes.fraction(typed) is None


def test_a_number_is_clamped_rather_than_refused():
    pct = problemtypes.load("leetcode").field("lc_runtime_pct")
    assert pct.clean("91") == 91.0
    assert pct.clean("140") == 100.0
    assert pct.clean("-3") == 0.0
    assert pct.clean("fast") is None
    assert pct.clean("  ") is None


def test_the_form_sorts_its_answers_into_columns_and_the_rest():
    columns, rest = problemtypes.split(
        {
            "verdict": "solved_unaided",
            "self_confidence": 3,
            "time_optimality": "optimal",
            "claimed_complexity": None,
            "requirements": "7/10",
            "bottlenecks": None,
        }
    )
    assert columns == {"time_optimality": "optimal", "claimed_complexity": None}
    assert rest == {"requirements": "7/10"}  # unanswered is left out, not stored


def test_an_answer_is_found_wherever_it_is_kept():
    assert problemtypes.answer({"requirements": "7/10"}, "requirements") == "7/10"
    assert problemtypes.answer({"answers": {"requirements": "7/10"}}, "requirements") == "7/10"
    stored = {"answers": json.dumps({"requirements": "7/10"})}
    assert problemtypes.answer(stored, "requirements") == "7/10"
    assert problemtypes.answer({"answers": "not json"}, "requirements") is None


# --- what the answers do to a review ---------------------------------------


DESIGN = {**SOLVED, "type": "system-design", "active_seconds": 2000}


@pytest.mark.parametrize(
    "requirements, rating",
    [
        ("3/10", Rating.Again),   # missed the problem
        ("6/10", Rating.Hard),
        ("8/10", Rating.Good),
        (None, Rating.Good),      # an unanswered question is not an answer
        ("most", Rating.Good),    # and neither is one that cannot be read
    ],
)
def test_coverage_moves_a_system_design_review(requirements, rating):
    attempt = {**DESIGN, "answers": {"requirements": requirements}}
    assert srs.rate(attempt, "medium", WEIGHTS) == rating


def test_easy_asks_for_everything_the_type_requires_of_it():
    fast = {**DESIGN, "active_seconds": 1200}
    full = {"requirements": "9/10", "design": "sound"}
    assert srs.rate({**fast, "answers": full}, "medium", WEIGHTS) == Rating.Easy
    # Any one requirement short, and it is Good.
    for short in ({"requirements": "9/10"}, {"design": "sound"}, {**full, "design": "unsure"}):
        assert srs.rate({**fast, "answers": short}, "medium", WEIGHTS) == Rating.Good


def test_a_flawed_design_is_hard_however_fast_it_was():
    attempt = {**DESIGN, "active_seconds": 600, "answers": {"design": "flawed"}}
    assert srs.rate(attempt, "medium", WEIGHTS) == Rating.Hard


def test_an_answer_cannot_rescue_an_attempt_that_failed():
    gave_up = {**DESIGN, "verdict": "gave_up", "answers": {"requirements": "10/10"}}
    assert srs.rate(gave_up, "medium", WEIGHTS) == Rating.Again


def test_par_is_the_types_own():
    """Fifty minutes is slow for a LeetCode medium and ordinary for a design."""
    fifty = {**SOLVED, "active_seconds": 3000}
    assert srs.rate(fifty, "medium", WEIGHTS) == Rating.Hard
    assert srs.rate({**fifty, "type": "system-design"}, "medium", WEIGHTS) == Rating.Good

    assert scoring.for_type(WEIGHTS, "system-design").par_for("medium") == 2700
    assert scoring.for_type(WEIGHTS, "leetcode") is WEIGHTS  # nothing laid over it
    # And what the type leaves out it inherits.
    assert scoring.for_type(WEIGHTS, "system-design").base_for("hard") == WEIGHTS.base_for("hard")


def test_the_score_is_measured_against_the_types_par():
    attempt = {**SOLVED, "active_seconds": 2700}
    plain = scoring.score_attempt(attempt, "medium", WEIGHTS)
    design = scoring.score_attempt({**attempt, "type": "system-design"}, "medium", WEIGHTS)
    assert design.par_seconds == 2700 and plain.par_seconds == 1800
    assert design.total > plain.total


def test_an_answer_is_a_claim_and_the_score_does_not_read_it():
    """Claims move reviews. The score stays a function of what was measured."""
    bare = scoring.score_attempt(DESIGN, "medium", WEIGHTS).total
    for answers in ({"requirements": "10/10", "design": "sound"}, {"requirements": "1/10"}):
        assert scoring.score_attempt({**DESIGN, "answers": answers}, "medium", WEIGHTS).total == bare


def test_the_waiver_is_the_fields_to_ask_for():
    """`unless_better_known` is what LeetCode's time ladder opts in to."""
    beaten = {**SOLVED, "active_seconds": 900, "time_optimality": "suboptimal"}
    assert srs.rate(beaten, "medium", WEIGHTS) == Rating.Hard
    assert srs.rate({**beaten, "saw_better": True}, "medium", WEIGHTS) == Rating.Good
    # A field that did not ask for it is not waived by it.
    flawed = {**DESIGN, "answers": {"design": "flawed"}, "saw_better": True}
    assert srs.rate(flawed, "medium", WEIGHTS) == Rating.Hard


def test_changing_a_type_regrades_what_was_already_answered(isolated_home):
    """The type is read at grade time, so an edit is one replay from applying."""
    attempt = {**DESIGN, "answers": {"requirements": "6/10"}}
    assert srs.rate(attempt, "medium", WEIGHTS) == Rating.Hard
    _write_type(
        "system-design",
        '[[fields]]\nkey = "requirements"\nkind = "fraction"\n'
        "review = { hard_below = 0.5 }\n",
    )
    assert srs.rate(attempt, "medium", WEIGHTS) == Rating.Good


# --- through the log -------------------------------------------------------


@pytest.fixture
def designs(conn):
    from core import catalog

    catalog.sync(conn, [ProblemSet("system-design", "system-design")])
    return conn


def _finish(conn, slug, verdict="solved_unaided", **kw):
    eng = RunEngine(conn)
    eng.start_session([slug])
    attempt = eng.start_problem(slug)
    eng.finish(verdict, **kw)
    eng.advance()
    eng.end_session()
    return attempt


def test_answers_land_on_the_attempt_and_survive_a_replay(designs):
    _finish(designs, "rate-limiter", answers={"requirements": "7/10", "design": "sound"})
    row = designs.execute("SELECT answers FROM attempts").fetchone()
    assert json.loads(row["answers"]) == {"requirements": "7/10", "design": "sound"}

    events.replay(designs)
    again = designs.execute("SELECT answers FROM attempts").fetchone()
    assert again["answers"] == row["answers"]


def test_the_card_is_graded_by_the_types_own_rules(designs):
    """Same verdict, same clock, same confidence — the coverage decides."""
    _finish(designs, "rate-limiter", self_confidence=3, answers={"requirements": "9/10"})
    _finish(designs, "url-shortener", self_confidence=3, answers={"requirements": "2/10"})
    covered = srs.card_row(designs, "rate-limiter")
    missed = srs.card_row(designs, "url-shortener")
    assert covered["due"] > missed["due"]
    # A miss restarts the failed ladder: four recalls to master, not two.
    assert missed["rungs_left"] == 4 and covered["rungs_left"] < 4

    before = [dict(r) for r in designs.execute("SELECT * FROM fsrs_cards ORDER BY slug")]
    events.replay(designs)
    after = [dict(r) for r in designs.execute("SELECT * FROM fsrs_cards ORDER BY slug")]
    assert after == before


def test_a_leetcode_finish_is_the_shape_it_has_always_been(conn):
    """No `answers` key on a payload that has nothing to put in one."""
    _finish(conn, "two-sum", time_optimality="optimal", claimed_complexity="O(n)")
    payload = json.loads(
        conn.execute("SELECT payload FROM events WHERE type = 'problem_finished'").fetchone()[
            "payload"
        ]
    )
    assert "answers" not in payload
    assert payload["time_optimality"] == "optimal"
    assert conn.execute("SELECT answers FROM attempts").fetchone()["answers"] is None


def test_a_second_pass_keeps_its_own_answers(designs):
    eng = RunEngine(designs)
    eng.start_session(["rate-limiter"])
    eng.start_problem("rate-limiter")
    eng.finish("solved_with_hints", answers={"requirements": "5/10"})
    eng.solve_again()
    eng.finish("solved_unaided", answers={"requirements": "9/10"})
    eng.advance()
    eng.end_session()

    first = designs.execute("SELECT answers FROM attempts").fetchone()
    second = designs.execute("SELECT answers FROM resolves").fetchone()
    assert json.loads(first["answers"]) == {"requirements": "5/10"}
    assert json.loads(second["answers"]) == {"requirements": "9/10"}


def test_giving_up_carries_no_answers(designs):
    """Nothing you claim about a solution survives not having reached one."""
    _finish(designs, "rate-limiter", verdict="gave_up", answers={"requirements": "9/10"})
    assert designs.execute("SELECT answers FROM attempts").fetchone()["answers"] is None


# --- on the stat line ------------------------------------------------------


def test_the_stat_line_says_what_the_type_asked():
    attempt = {
        "type": "system-design",
        "answers": {"requirements": "7/10", "design": "flawed", "delivery": "unsure"},
    }
    assert render.field_rows(attempt) == [
        ("coverage", "7/10  ·  70%"),
        ("design", "has a hole in it"),
        ("delivery", "not sure"),
    ]
    # Nothing answered, nothing drawn.
    assert render.field_rows({"type": "system-design"}) == []


def test_leetcode_gets_no_rows_it_did_not_already_have():
    """Its fields all have columns, and the rows for those are drawn elsewhere."""
    attempt = {"time_optimality": "optimal", "claimed_complexity": "O(n)", "code_style": "rough"}
    assert render.field_rows(attempt) == []
    assert render.attempt_rows(attempt) == [
        ("time", "O(n)  ·  optimal"),
        ("code style", "needs a rewrite"),
    ]


def test_an_answer_outlives_the_field_that_asked_for_it(isolated_home):
    """Dropped from the type, it is not drawn. Put back, so is the row."""
    attempt = {"type": "system-design", "answers": {"requirements": "7/10"}}
    _write_type("system-design", '[[fields]]\nkey = "delivery"\nkind = "text"\n')
    assert render.field_rows(attempt) == []
    problemtypes.user_path("system-design").unlink()
    assert render.field_rows(attempt) == [("coverage", "7/10  ·  70%")]


# --- capture ---------------------------------------------------------------


DESIGN_PROBLEM = Problem(
    slug="rate-limiter",
    title="Design a rate limiter",
    url="",
    difficulty="easy",
    tags=(),
    pattern="coordination",
    lists=("system-design",),
    type="system-design",
)


def test_a_write_up_is_archived_as_markdown_whatever_you_solve_in():
    assert capture.language_for(DESIGN_PROBLEM, "rust") == "markdown"
    leetcode = Problem("two-sum", "Two Sum", "u", "easy", (), None, ("neetcode150",))
    assert capture.language_for(leetcode, "rust") == "rust"


def test_a_markdown_header_is_a_comment_not_a_title():
    header = capture.solution_header(DESIGN_PROBLEM, {"verdict": "solved_unaided"}, "md")
    lines = [line for line in header.splitlines() if line]
    assert all(line.startswith("<!--") and line.endswith("-->") for line in lines)
    assert "write the design up below." in header


def test_a_written_design_lands_beside_the_code(monkeypatch):
    monkeypatch.setattr(capture, "editor_available", lambda: True)

    def write(path):
        path.write_text(path.read_text() + "## components\n\na token bucket per key\n")
        return True

    monkeypatch.setattr(capture, "spawn_editor", write)
    result = capture.capture_solution(DESIGN_PROBLEM, {}, 7)
    assert result.saved
    assert result.path == paths.code_path("rate-limiter", 7, "md")


def test_an_untouched_design_is_not_archived(monkeypatch):
    monkeypatch.setattr(capture, "editor_available", lambda: True)
    monkeypatch.setattr(capture, "spawn_editor", lambda path: True)
    assert not capture.capture_solution(DESIGN_PROBLEM, {}, 7).saved


def test_a_type_can_ask_its_own_questions_in_the_note(isolated_home):
    _write_type(
        "system-design",
        '[capture]\nnote_template = """\n<!-- {app} | {slug} -->\n'
        '## what would fall over first\n"""\n',
    )
    body = capture.note_template(DESIGN_PROBLEM.slug, problemtypes.load("system-design").note_template)
    assert "## what would fall over first" in body
    assert "rate-limiter" in body and "{slug}" not in body
    # And untouched, it still counts as nothing written.
    assert capture._strip_note_scaffolding(body) == ""


def test_a_stray_brace_in_a_template_is_just_a_brace():
    assert capture.note_template("x", "## what about {this}\n") == "## what about {this}\n"
