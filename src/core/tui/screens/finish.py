"""Modals: the finish prompt, the end-of-run prompt, and a generic confirm.

The finish prompt is the manual verdict entry (spec §15.4). LeetCode is the
judge; you self-report. Keep it fast — this screen sits between you and the
next problem, and friction here is what makes people stop logging.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RadioButton, RadioSet, Static

from ...scoring import VERDICTS, VERDICT_LABELS, fmt_duration
from ..vim import MOTIONS, VimMotion

# Still 1..4, still worst-to-best, still stored in `attempts.self_confidence` --
# the numbers mean what they always meant, so old attempts read the same way.
#
# What changed is the question. "How well will this stick?" is a prediction made
# with the solution still in front of you, which is the condition under which
# self-assessment is least reliable; you are rating how clear it feels now, not
# how it will go cold. Naming the retrieval condition -- a month, no warning --
# is the standard correction, and it costs nothing to ask it this way instead.
CONFIDENCE_OPTIONS = [
    "1  no idea",
    "2  I'd struggle",
    "3  I'd get there",
    "4  I'd nail it",
]

# Stored value first, wording second, so the vocabulary that reaches the database
# and the vocabulary on screen can never drift apart.
#
# The cursor starts on "not sure" -- the last entry -- for the same reason the
# verdict cursor starts on the worst thing already on the record. "Optimal" is
# the flattering answer, and a default of flattering is how a month of solves
# you never actually checked quietly claims to have been optimal.
OPTIMALITY_OPTIONS = (
    ("optimal", "optimal"),
    ("suboptimal", "not optimal"),
    ("unsure", "not sure"),
)
OPTIMALITY_DEFAULT = len(OPTIMALITY_OPTIONS) - 1

# The same ladder asked once per axis, side by side. Two answers rather than one
# because the trade is the whole point: the hash map that turns O(n log n) into
# O(n) pays O(n) space for it, and an answer that cannot say "bought time with
# space" cannot record the decision you actually made. Each axis keeps its own
# "not sure" -- being certain about time and having never thought about space is
# the normal state, and a single answer would force you to lie about one of them.
OPTIMALITY_AXES = (
    ("time-optimality", "time", "time_optimality"),
    ("space-optimality", "space", "space_optimality"),
)

#: The two ways out of `EndRunModal` that end the run. Named rather than spelled
#: out at both ends, so the caller's branch and the modal's answer cannot drift.
END_RUN_RECORD = "record"
END_RUN_DISCARD = "discard"

#: Signal keys a post-solve modal can answer with instead of a set of answers.
#: `discard` throws the attempt away; `back` steps one screen towards the
#: problem. Both are named for the same reason as the two above: the branch that
#: reads them and the modal that writes them are in different files.
SIGNAL_DISCARD = "discard"
SIGNAL_BACK = "back"


def _pct_text(value: Any) -> str:
    """A percentile back in the box you typed it into.

    `%g` rather than `str`: the field is `type="number"` and `_pct` has already
    turned "91" into 91.0, which would come back as "91.0" and read as a number
    you did not enter.
    """
    return "" if value is None else f"{float(value):g}"


class FinishModal(VimMotion, ModalScreen[dict[str, Any] | None]):
    """Verdict, self-confidence, what you say the solution costs, the percentiles."""

    # No VIM_TARGET: motions here move the focused radio set and nothing else.
    # With focus in a complexity field or one of the percentile inputs there is
    # nothing sensible for `j` to move, and guessing would move a radio set you
    # can't see moving.
    BINDINGS = [
        *MOTIONS,
        Binding("escape", "cancel", "back to the problem"),
        Binding("ctrl+s", "save", "save"),
        # A chord, not a letter: focus lives in a radio set or in one of the
        # percentile inputs, where a bare `x` is something you typed.
        Binding("ctrl+x", "throw_away", "throw away"),
        # The one place on this screen with sideways content: the two optimality
        # ladders. `h` and `l` cross between them and mean nothing anywhere else,
        # which keeps the rule from `vim.py` — whatever `l` does, `h` undoes.
        Binding("h", "axis(-1)", "time / space", show=False),
        Binding("l", "axis(1)", "time / space", show=False),
    ]

    def __init__(
        self,
        title: str,
        active_seconds: int,
        submissions: int,
        hint_tier: int,
        answers: dict[str, Any] | None = None,
    ):
        super().__init__()
        self.problem_title = title
        self.active_seconds = active_seconds
        self.submissions = submissions
        self.hint_tier = hint_tier
        #: What this prompt answered last time it was open, when the screen
        #: after it sent you back here. Every widget below reads it before it
        #: reads its own default, so stepping back and forward is a round trip
        #: rather than a form you fill in twice.
        self.answers = answers or {}

    def _prior_index(self, key: str, options: tuple, fallback: int) -> int:
        """Where an optimality ladder starts: your last answer, or its default.

        One helper for both axes rather than the lookup written twice, because
        they are the same question asked twice and spelling it per axis is how
        one of them ends up quietly not restoring. The verdict and confidence
        ladders each restore in their own way — the first has a default worth
        computing, the second is a 1..4 offset.
        """
        value = self.answers.get(key)
        if value is None:
            return fallback
        try:
            return options.index(value)
        except ValueError:
            return fallback

    @property
    def _default_verdict(self) -> int:
        """Which verdict the cursor starts on.

        A prior answer wins over all of it. Coming back from the next screen is
        not a fresh prompt, and re-guessing at a verdict you already picked
        would be the app arguing with you.

        Offline it starts on `ungraded`, because there was no judge to accept
        anything — and a default of "solved" is precisely how a plane's worth of
        unverified solves quietly rots the distributions.

        Otherwise it starts on the worst thing already on the record: if you
        revealed a hint, the cursor sits on `solved_with_hints`. The hints are
        logged either way, so this costs nothing to be honest about — it just
        saves a keystroke on the common case.
        """
        if self.answers.get("verdict") in VERDICTS:
            return VERDICTS.index(self.answers["verdict"])
        offline = getattr(getattr(self.app, "config", None), "cache", None)
        if offline is not None and offline.offline and "ungraded" in VERDICTS:
            return VERDICTS.index("ungraded")
        if self.hint_tier > 0 and "solved_with_hints" in VERDICTS:
            return VERDICTS.index("solved_with_hints")
        return 0

    def compose(self) -> ComposeResult:
        summary = (
            f"{fmt_duration(self.active_seconds)}   ·   "
            f"{self.submissions} failed submit{'s' if self.submissions != 1 else ''}   ·   "
            f"{'no hints' if not self.hint_tier else f'hint tier {self.hint_tier}'}"
        )
        default = self._default_verdict
        with Vertical(id="finish-box"):
            yield Static(self.problem_title, classes="modal-title")
            yield Static(summary, classes="field-label")
            yield Static("verdict", classes="field-label")
            with RadioSet(id="verdict"):
                for i, v in enumerate(VERDICTS):
                    yield RadioButton(VERDICT_LABELS[v], value=(i == default))
            yield Static("if this came up cold in a month?", classes="field-label")
            confidence = self.answers.get("self_confidence")
            selected = int(confidence) - 1 if confidence else 2
            with RadioSet(id="confidence"):
                for i, label in enumerate(CONFIDENCE_OPTIONS):
                    yield RadioButton(label, value=(i == selected))
            yield Static("what your solution costs — optional", classes="field-label")
            with Horizontal(id="complexity-row"):
                yield Input(
                    self.answers.get("claimed_complexity") or "",
                    placeholder="time   O(n log n)",
                    id="complexity",
                )
                yield Input(
                    self.answers.get("claimed_space_complexity") or "",
                    placeholder="space   O(1)",
                    id="space-complexity",
                )
            yield Static("was it optimal?", classes="field-label")
            stored = tuple(value for value, _ in OPTIMALITY_OPTIONS)
            with Horizontal(id="optimality-row"):
                for radio_id, axis, key in OPTIMALITY_AXES:
                    chosen = self._prior_index(key, stored, OPTIMALITY_DEFAULT)
                    with Vertical(classes="optimality-axis"):
                        yield Static(axis, classes="axis-label")
                        with RadioSet(id=radio_id):
                            for i, (_, label) in enumerate(OPTIMALITY_OPTIONS):
                                yield RadioButton(label, value=(i == chosen))
            yield Static("leetcode percentiles — optional", classes="field-label")
            with Horizontal(id="optional-row"):
                yield Input(
                    _pct_text(self.answers.get("lc_runtime_pct")),
                    placeholder="runtime %",
                    id="runtime",
                    type="number",
                )
                yield Input(
                    _pct_text(self.answers.get("lc_memory_pct")),
                    placeholder="memory %",
                    id="memory",
                    type="number",
                )
            with Horizontal(id="confirm-buttons"):
                yield Button("save  (ctrl+s)", variant="primary", id="save")
                yield Button("throw away  (ctrl+x)", variant="warning", id="discard")
                yield Button("back  (esc)", id="cancel")

    def on_mount(self) -> None:
        # Textual parks a RadioSet's navigation cursor on the first button
        # whichever one is actually pressed, so `j`/`k` would start from the top
        # of a ladder whose default sits in the middle -- and every default on
        # this screen is deliberately not the first entry. Line the two up, or
        # correcting a default moves you somewhere you weren't looking.
        # `_selected` is the only handle on it; there is no public setter.
        for radio in self.query(RadioSet):
            if radio.pressed_index >= 0:
                radio._selected = radio.pressed_index
        self.query_one("#verdict", RadioSet).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "discard":
            self.action_throw_away()
        else:
            self.action_cancel()

    def on_input_submitted(self) -> None:
        self.action_save()

    @staticmethod
    def _pct(raw: str) -> float | None:
        raw = raw.strip()
        if not raw:
            return None
        try:
            return max(0.0, min(100.0, float(raw)))
        except ValueError:
            return None

    def action_axis(self, delta: int) -> None:
        """Move focus between the two ladders. A no-op from anywhere else.

        Deliberately not a wrap-around ride through every widget on the screen:
        `l` from the verdict ladder has nowhere sideways to go, and taking focus
        somewhere you were not looking is exactly what the motion rule forbids.
        """
        ids = [radio_id for radio_id, _, _ in OPTIMALITY_AXES]
        focused = getattr(self.focused, "id", None)
        if focused not in ids:
            return
        self.query_one(f"#{ids[(ids.index(focused) + delta) % len(ids)]}", RadioSet).focus()

    def _optimality(self, radio_id: str) -> str:
        index = self.query_one(f"#{radio_id}", RadioSet).pressed_index
        return OPTIMALITY_OPTIONS[index if index >= 0 else OPTIMALITY_DEFAULT][0]

    def action_save(self) -> None:
        verdict_index = self.query_one("#verdict", RadioSet).pressed_index
        confidence_index = self.query_one("#confidence", RadioSet).pressed_index
        self.dismiss(
            {
                "verdict": VERDICTS[
                    verdict_index if verdict_index >= 0 else self._default_verdict
                ],
                "self_confidence": (confidence_index + 1) if confidence_index >= 0 else None,
                "claimed_complexity": self.query_one("#complexity", Input).value.strip() or None,
                "claimed_space_complexity": (
                    self.query_one("#space-complexity", Input).value.strip() or None
                ),
                **{key: self._optimality(radio_id) for radio_id, _, key in OPTIMALITY_AXES},
                "lc_runtime_pct": self._pct(self.query_one("#runtime", Input).value),
                "lc_memory_pct": self._pct(self.query_one("#memory", Input).value),
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_throw_away(self) -> None:
        """Ask for the attempt to be dropped entirely.

        Only signals the intent — the caller confirms it and does the work, so
        the destructive step is never one keystroke deep inside a modal.
        """
        self.dismiss({SIGNAL_DISCARD: True})


class EndRunModal(ModalScreen[str | None]):
    """How to end a run that still has a problem open.

    Three answers rather than two, because "I'm done for today" and "this
    attempt should never have existed" are different things and the prompt used
    to only be able to say the first. Ending on a live problem records it as
    `gave_up` and then opens the editor for the partial code, which is the right
    default -- but it is the wrong thing entirely for the run you opened by
    mistake, and sitting through two editor handoffs to get out of one is how
    people learn to hard-quit instead.

    Dismisses with `END_RUN_RECORD`, `END_RUN_DISCARD`, or None for keep going.
    """

    BINDINGS = [
        Binding("escape", "keep", "keep going"),
        Binding("y", "record", "end run"),
        # `x` for throwing an attempt away, the same letter the finish modal
        # already chords for it. Bare here, because unlike that screen there is
        # nothing on this one you could be typing into.
        Binding("x", "discard", "throw it away"),
        Binding("n", "keep", "keep going"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="end-run-box"):
            yield Static("End the run here?", classes="modal-title")
            yield Static(
                "end run — the problem in progress is recorded as gave_up, then "
                "the editor opens for whatever code you have.",
                classes="field-label",
            )
            yield Static(
                "throw it away — nothing about this problem is recorded and no "
                "editor opens. Problems you already finished keep their scores "
                "either way.",
                classes="field-label",
            )
            yield Static(
                "If you mean to come back to it, keep going and z suspends the run.",
                classes="field-label",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("end run  (y)", variant="warning", id="record")
                yield Button("throw away  (x)", variant="warning", id="discard")
                yield Button("keep going  (n)", id="keep")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(
            {"record": END_RUN_RECORD, "discard": END_RUN_DISCARD}.get(event.button.id or "")
        )

    def action_record(self) -> None:
        self.dismiss(END_RUN_RECORD)

    def action_discard(self) -> None:
        self.dismiss(END_RUN_DISCARD)

    def action_keep(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "no", "no"),
        Binding("y", "yes", "yes"),
        Binding("n", "no", "no"),
    ]

    def __init__(self, question: str, detail: str = "", yes_label: str = "yes", no_label: str = "no"):
        super().__init__()
        self.question = question
        self.detail = detail
        self.yes_label = yes_label
        self.no_label = no_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.question, classes="modal-title")
            if self.detail:
                yield Static(self.detail, classes="field-label")
            with Horizontal(id="confirm-buttons"):
                yield Button(f"{self.yes_label}  (y)", variant="warning", id="yes")
                yield Button(f"{self.no_label}  (n)", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
