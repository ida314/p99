"""Catalog integrity and the `$EDITOR` capture contract (spec §1, §7)."""

from __future__ import annotations

import json

from core import branding, capture, catalog, paths
from core.capture import _strip_note_scaffolding, note_template
from core.catalog import Problem


def test_catalog_seeds_150_problems(conn):
    assert catalog.count(conn) == 150


def test_catalog_stores_no_problem_content(conn):
    """The whole ToS position rests on this: metadata only, never content."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(problems)")}
    assert columns == {"slug", "title", "url", "difficulty", "tags", "pattern", "lists"}


def test_catalog_entries_are_well_formed(conn):
    for problem in catalog.all_problems(conn):
        assert problem.difficulty in {"easy", "medium", "hard"}
        assert problem.url.startswith("https://leetcode.com/problems/")
        assert problem.tags
        assert problem.pattern
        assert "neetcode150" in problem.lists


def test_seeding_is_an_idempotent_upsert(conn):
    catalog.seed(conn)
    catalog.seed(conn)
    assert catalog.count(conn) == 150


def test_random_pick_prefers_unseen(conn):
    from core.engine import RunEngine

    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.finish("accepted")
    eng.advance()
    eng.end_session()

    # 149 unseen problems exist, so a pick of 149 must exclude the seen one.
    picks = catalog.pick_random(conn, 149)
    assert "two-sum" not in {p.slug for p in picks}


def test_random_pick_respects_the_active_list(conn):
    picks = catalog.pick_random(conn, 30, active_list="blind75")
    assert all("blind75" in p.lists for p in picks)


# --- capture ---------------------------------------------------------------


PROBLEM = Problem(
    slug="longest-repeating-character-replacement",
    title="Longest Repeating Character Replacement",
    url="https://leetcode.com/problems/longest-repeating-character-replacement/",
    difficulty="medium",
    tags=("string", "sliding-window"),
    pattern="sliding-window",
    lists=("neetcode150",),
)


def test_solution_header_carries_the_run_facts():
    header = capture.solution_header(
        PROBLEM,
        {"started_at": "2026-07-25T21:03:00+00:00", "active_seconds": 872,
         "max_hint_tier": 1, "verdict": "accepted"},
        "py",
    )
    assert header.startswith(
        f"# {branding.NAME} | longest-repeating-character-replacement | MEDIUM"
    )
    assert "14:32" in header
    assert "hints: tier 1" in header
    assert "ACCEPTED" in header
    assert ":q! to skip" in header


def test_solution_header_uses_the_language_comment_syntax():
    assert capture.solution_header(PROBLEM, {}, "go").startswith(f"// {branding.NAME}")
    assert capture.solution_header(PROBLEM, {}, "sql").startswith(f"-- {branding.NAME}")


def test_note_template_asks_the_three_questions():
    body = note_template(PROBLEM.slug)
    assert "## what made it hard" in body
    assert "## what unstuck you" in body
    assert "## if you saw this cold in 6 months" in body


def test_an_untouched_note_template_counts_as_empty():
    """A blank buffer produces an empty file; the template must not fake one."""
    assert _strip_note_scaffolding(note_template("x")) == ""


def test_a_note_with_two_sentences_is_kept():
    written = note_template("x") + "\nI knew it was a heap within a minute.\n"
    assert "heap" in _strip_note_scaffolding(written)


def test_skipping_the_editor_saves_nothing(monkeypatch, tmp_path):
    """`:q!` leaves the buffer untouched — nothing is archived, nothing penalised."""
    monkeypatch.setattr(capture, "editor_available", lambda: True)
    monkeypatch.setattr(capture, "spawn_editor", lambda path: True)  # writes nothing

    result = capture.capture_note(PROBLEM, 42)
    assert not result.saved
    assert result.reason == "skipped"
    assert not paths.note_path(PROBLEM.slug, 42).exists()


def test_a_written_note_lands_at_the_spec_path(monkeypatch):
    monkeypatch.setattr(capture, "editor_available", lambda: True)

    def write_something(path):
        path.write_text(note_template(PROBLEM.slug) + "\nshrink condition again.\n")
        return True

    monkeypatch.setattr(capture, "spawn_editor", write_something)

    result = capture.capture_note(PROBLEM, 42)
    assert result.saved
    assert result.path == paths.note_path(PROBLEM.slug, 42)
    assert result.path.exists()
    assert "shrink condition" in result.path.read_text()


def test_a_solution_with_only_the_header_is_not_archived(monkeypatch):
    monkeypatch.setattr(capture, "editor_available", lambda: True)
    monkeypatch.setattr(capture, "spawn_editor", lambda path: True)
    assert not capture.capture_solution(PROBLEM, {}, 7, "python").saved


def test_an_archived_solution_lands_at_the_spec_path(monkeypatch):
    monkeypatch.setattr(capture, "editor_available", lambda: True)

    def paste(path):
        path.write_text(path.read_text() + "class Solution:\n    pass\n")
        return True

    monkeypatch.setattr(capture, "spawn_editor", paste)

    result = capture.capture_solution(PROBLEM, {}, 7, "python")
    assert result.saved
    assert result.path == paths.code_path(PROBLEM.slug, 7, "py")
    assert result.path.read_text().endswith("class Solution:\n    pass\n")


def test_missing_editor_is_not_an_error(monkeypatch):
    monkeypatch.setattr(capture, "editor_available", lambda: False)
    assert not capture.capture_note(PROBLEM, 1).saved


def test_paths_follow_the_spec(isolated_home):
    assert paths.code_path("two-sum", 12, "py").as_posix().endswith("code/two-sum/12.py")
    assert paths.note_path("two-sum", 12).as_posix().endswith("notes/two-sum/12.md")


def test_bundled_catalog_json_is_unique_and_complete():
    entries = catalog.bundled_catalog()
    slugs = [e["slug"] for e in entries]
    assert len(slugs) == len(set(slugs)) == 150
    assert all(json.dumps(e["tags"]) for e in entries)


def test_editor_handoff_runs_a_real_subprocess(monkeypatch, tmp_path, env_editor):
    """`spawn_editor` + `$EDITOR` resolution, exercised for real."""
    fake_editor = tmp_path / "fake-editor"
    fake_editor.write_text(
        "#!/bin/sh\nprintf 'the shrink condition, again\\n' >> \"$1\"\n"
    )
    fake_editor.chmod(0o755)
    monkeypatch.setenv(env_editor, str(fake_editor))

    assert capture.editor_available()

    result = capture.capture_note(PROBLEM, 99)
    assert result.saved
    assert "shrink condition" in result.path.read_text()

    code = capture.capture_solution(PROBLEM, {"verdict": "accepted"}, 99, "python")
    assert code.saved
    assert code.path.name == "99.py"


def test_editor_that_writes_nothing_skips_cleanly(monkeypatch, tmp_path, env_editor):
    quitter = tmp_path / "quitter"
    quitter.write_text("#!/bin/sh\nexit 1\n")  # :q! and a non-zero exit
    quitter.chmod(0o755)
    monkeypatch.setenv(env_editor, str(quitter))

    assert not capture.capture_note(PROBLEM, 100).saved
