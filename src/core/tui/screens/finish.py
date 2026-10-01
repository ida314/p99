"""Modals: the finish prompt, the end-of-run prompt, and a generic confirm.

The finish prompt is the manual verdict entry (spec §15.4). LeetCode is the
judge; you self-report. Keep it fast — this screen sits between you and the
next problem, and friction here is what makes people stop logging.

The verdict and the recall question are asked of every problem, because the
score and the review are built on them. Everything under those two is the
problem *type's*: a form drawn from the fields the type lists, in the order it
lists them. What LeetCode asks — the two complexities, the three ladders, the
percentiles — used to be written out here and is now `data/types/leetcode.toml`,
where the reasons for each default went with it.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RadioButton, RadioSet, Static

from ... import clipboard, problemtypes
from ...config import DEFAULT_POST_SOLVE_PROMPT
from ...scoring import VERDICT_LABELS, fmt_duration
from ..vim import MOTIONS, VimMotion

# Still 1..4, still worst-to-best, still stored in `attempts.self_confidence` --
# the numbers mean what they always meant, so old attempts read the same way.
#
# The question names the retrieval condition -- a month, cold, no notes or hints
# -- because "how well will this stick?" is a prediction made with the solution
# still in front of you, when self-assessment is least reliable. And the answers
# are amounts of help you would need, a thing you can picture, rather than a
# rating of yourself. (Kept short because a radio label is one line, and the
# box is 74 wide.)
#
# Not a field of any type, and asked of all of them: whether it would come back
# cold is the same question about a design as about a solution, and `srs.rate`
# reads the answer without asking what kind of problem it was.
CONFIDENCE_QUESTION = (
    "If I saw this problem cold in a month, with no notes or hints, how much "
    "help would I need to re-derive the key insight and implement it correctly?"
)
CONFIDENCE_OPTIONS = [
    "1  Major help — I probably wouldn’t find the key idea",
    "2  A hint — I’d need a nudge toward the key idea",
    "3  No hint, but effort — reconstruct with some struggle",
    "4  Independent — derive and implement from scratch",
]

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


def _number_text(value: Any) -> str:
    """A number back in the box you typed it into.

    `%g` rather than `str`: the field is `type="number"` and the form has
    already turned "91" into 91.0, which would come back as "91.0" and read as
    a number you did not enter.
    """
    if value is None:
        return ""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


class FinishModal(VimMotion, ModalScreen[dict[str, Any] | None]):
    """Verdict, self-confidence, and whatever the problem's type asks after them."""

    # No VIM_TARGET: motions here move the focused radio set and nothing else.
    # With focus in a text field there is nothing sensible for `j` to move, and
    # guessing would move a radio set you can't see moving.
    BINDINGS = [
        *MOTIONS,
        Binding("escape", "cancel", "back to the problem"),
        Binding("ctrl+s", "save", "save"),
        # A chord, not a letter: focus lives in a radio set or in one of the
        # inputs, where a bare `x` is something you typed.
        Binding("ctrl+x", "throw_away", "throw away"),
        # `y` for yank. Copies and stays put: the point is to paste it
        # somewhere and come back here with the answers.
        Binding("ctrl+y", "copy_prompt", "copy AI prompt"),
        # The one place on this screen with sideways content: ladders that share
        # a row. `h` and `l` cross between them and mean nothing anywhere else,
        # which keeps the rule from `vim.py` — whatever `l` does, `h` undoes.
        Binding("h", "axis(-1)", "across the ladders", show=False),
        Binding("l", "axis(1)", "across the ladders", show=False),
    ]

    def __init__(
        self,
        title: str,
        active_seconds: int,
        submissions: int,
        hint_tier: int,
        answers: dict[str, Any] | None = None,
        ptype: problemtypes.ProblemType | None = None,
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
        #: What kind of problem this is, which is what decides every row under
        #: the recall question. LeetCode when nobody said, because that is what
        #: a problem was before there were types.
        self.ptype = ptype or problemtypes.load()

    @property
    def verdicts(self) -> tuple[str, ...]:
        """The rungs on offer, in radio order. Index 0 is the default."""
        return self.ptype.verdicts

    def _prior_index(self, entry: problemtypes.Field) -> int:
        """Where a ladder starts: your last answer, or the field's own default.

        One helper for every ladder rather than the lookup written out per
        field, which is how one of them ends up quietly not restoring. The
        verdict and confidence ladders each restore in their own way — the
        first has a default worth computing, the second is a 1..4 offset.
        """
        value = self.answers.get(entry.key)
        if value in entry.values:
            return entry.values.index(value)
        return entry.default_index

    def _prior_text(self, entry: problemtypes.Field) -> str:
        value = self.answers.get(entry.key)
        if entry.kind == problemtypes.NUMBER:
            return _number_text(value)
        return "" if value is None else str(value)

    @property
    def _default_verdict(self) -> int:
        """Which verdict the cursor starts on.

        A prior answer wins over all of it. Coming back from the next screen is
        not a fresh prompt, and re-guessing at a verdict you already picked
        would be the app arguing with you.

        Offline it starts on `ungraded`, because there was no judge to accept
        anything — and a default of "solved" is precisely how a plane's worth of
        unverified solves quietly rots the distributions. Only for a type that
        has a judge to be without: a design is self-assessed on the ground too,
        and being on a plane changes nothing about that.

        Otherwise it starts on the worst thing already on the record: if you
        revealed a hint, the cursor sits on `solved_with_hints`. The hints are
        logged either way, so this costs nothing to be honest about — it just
        saves a keystroke on the common case.
        """
        verdicts = self.verdicts
        if self.answers.get("verdict") in verdicts:
            return verdicts.index(self.answers["verdict"])
        offline = getattr(getattr(self.app, "config", None), "cache", None)
        if (
            self.ptype.judge
            and offline is not None
            and offline.offline
            and "ungraded" in verdicts
        ):
            return verdicts.index("ungraded")
        if self.hint_tier > 0 and "solved_with_hints" in verdicts:
            return verdicts.index("solved_with_hints")
        return 0

    def _summary(self) -> str:
        bits = [fmt_duration(self.active_seconds)]
        if self.ptype.judge:
            # Only where there was something to submit to. "0 failed submits"
            # about a whiteboard is a count of a thing that cannot happen.
            bits.append(
                f"{self.submissions} failed submit{'s' if self.submissions != 1 else ''}"
            )
        bits.append("no hints" if not self.hint_tier else f"hint tier {self.hint_tier}")
        return "   ·   ".join(bits)

    def _copy_button(self) -> Button:
        copy = Button("📋", id="copy-prompt")
        copy.tooltip = "AI prompt  (ctrl+y)"
        # ctrl+y reaches it from anywhere, so tab doesn't stop on it.
        copy.can_focus = False
        return copy

    def _compose_field(self, entry: problemtypes.Field, row_label: str) -> ComposeResult:
        """One field, as the widget its kind calls for.

        A ladder is a radio set under its own name. Everything else is a box
        you type in, because a number, a share and a sentence are all things
        you already know how to write — the kind decides what is *stored*, in
        `Field.clean`, not what you are made to click through.
        """
        if entry.kind == problemtypes.CHOICE:
            chosen = self._prior_index(entry)
            with Vertical(classes="field-axis"):
                # Named on screen, or three ladders side by side mean nothing.
                # Not when the row's own label already said it.
                if entry.label != row_label:
                    yield Static(entry.label, classes="axis-label")
                with RadioSet(id=entry.widget_id):
                    for i, option in enumerate(entry.options):
                        yield RadioButton(option.label, value=(i == chosen))
            return
        yield Input(
            self._prior_text(entry),
            placeholder=entry.placeholder or entry.label,
            id=entry.widget_id,
            type="number" if entry.kind == problemtypes.NUMBER else "text",
        )

    def compose(self) -> ComposeResult:
        default = self._default_verdict
        groups = self.ptype.groups()
        # The prompt sits outside the box, level with its top: it is an errand
        # to the LeetCode tab rather than one of the answers the box collects.
        with Horizontal(id="finish-frame"):
            with Vertical(id="finish-box"):
                yield Static(self.problem_title, classes="modal-title")
                yield Static(self._summary(), classes="field-label")
                yield Static("verdict", classes="field-label")
                with RadioSet(id="verdict"):
                    for i, v in enumerate(self.verdicts):
                        yield RadioButton(VERDICT_LABELS[v], value=(i == default))
                yield Static(CONFIDENCE_QUESTION, classes="field-label")
                confidence = self.answers.get("self_confidence")
                selected = int(confidence) - 1 if confidence else 2
                with RadioSet(id="confidence"):
                    for i, label in enumerate(CONFIDENCE_OPTIONS):
                        yield RadioButton(label, value=(i == selected))
                for group in groups:
                    if group.labelled:
                        yield Static(group.label, classes="field-label")
                    with Horizontal(classes="field-row"):
                        for entry in group.fields:
                            yield from self._compose_field(entry, group.label)
                with Horizontal(id="confirm-buttons"):
                    yield Button("save  (ctrl+s)", variant="primary", id="save")
                    yield Button("throw away  (ctrl+x)", variant="warning", id="discard")
                    yield Button("back  (esc)", id="cancel")
            if self.ptype.ai_copy:
                yield self._copy_button()

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

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Hidden, not just inert, for a type with nothing to copy: a footer
        # that offers a key which then apologises is worse than no key.
        if action == "copy_prompt":
            return self.ptype.ai_copy
        return True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "discard":
            self.action_throw_away()
        elif event.button.id == "copy-prompt":
            self.action_copy_prompt()
        else:
            self.action_cancel()

    def on_input_submitted(self) -> None:
        self.action_save()

    def action_axis(self, delta: int) -> None:
        """Move focus between the ladders that share a row. A no-op anywhere else.

        Deliberately not a wrap-around ride through every widget on the screen:
        `l` from the verdict ladder has nowhere sideways to go, and taking focus
        somewhere you were not looking is exactly what the motion rule forbids.
        For the same reason it stays inside the row it started in.
        """
        focused = getattr(self.focused, "id", None)
        for group in self.ptype.groups():
            ids = [entry.widget_id for entry in group.ladders]
            if focused in ids:
                target = ids[(ids.index(focused) + delta) % len(ids)]
                self.query_one(f"#{target}", RadioSet).focus()
                return

    def _answer(self, entry: problemtypes.Field) -> Any:
        """What one field holds right now, as it will be stored."""
        if entry.kind == problemtypes.CHOICE:
            index = self.query_one(f"#{entry.widget_id}", RadioSet).pressed_index
            return entry.values[index if index >= 0 else entry.default_index]
        return entry.clean(self.query_one(f"#{entry.widget_id}", Input).value)

    def action_save(self) -> None:
        verdict_index = self.query_one("#verdict", RadioSet).pressed_index
        confidence_index = self.query_one("#confidence", RadioSet).pressed_index
        self.dismiss(
            {
                "verdict": self.verdicts[
                    verdict_index if verdict_index >= 0 else self._default_verdict
                ],
                "self_confidence": (confidence_index + 1) if confidence_index >= 0 else None,
                # One key per field, answered or not. `problemtypes.split` is
                # what sorts them into the columns and the `answers` block on
                # the way to the log; here they are one flat form.
                **{entry.key: self._answer(entry) for entry in self.ptype.fields},
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_copy_prompt(self) -> None:
        """Put the post-solve prompt on the clipboard.

        The type's own prompt where it wrote one, and `[ai] post_solve_prompt`
        where it did not — which is LeetCode's arrangement, and the reason the
        settings screen still edits the one you actually paste.

        A clipboard tool first, since that is the copy you can count on; the
        terminal's OSC 52 escape otherwise, which reaches the clipboard over ssh
        but which some terminals quietly drop -- so that path says it tried
        rather than that it worked.
        """
        if not self.ptype.ai_copy:
            return
        ai = getattr(getattr(self.app, "config", None), "ai", None)
        prompt = self.ptype.ai_prompt or (
            ai.post_solve_prompt if ai is not None else DEFAULT_POST_SOLVE_PROMPT
        )
        where = self.ptype.ai_paste_into
        if clipboard.copy(prompt):
            self.notify(f"prompt copied — paste it into {where}" if where else "prompt copied")
            return
        self.app.copy_to_clipboard(prompt)
        self.notify("prompt sent to the terminal's clipboard — if paste comes up empty, install wl-copy or xclip")

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
