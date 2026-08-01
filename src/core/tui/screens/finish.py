"""Modals: the finish prompt and a generic confirm.

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

CONFIDENCE_OPTIONS = [
    "1  gone in a week",
    "2  shaky",
    "3  good",
    "4  solid",
]


class FinishModal(VimMotion, ModalScreen[dict[str, Any] | None]):
    """Verdict + self-confidence, plus the optional hand-entered LC percentiles."""

    # No VIM_TARGET: motions here move the focused radio set and nothing else.
    # With focus in one of the percentile inputs there is nothing sensible for
    # `j` to move, and guessing would move a radio set you can't see moving.
    BINDINGS = [
        *MOTIONS,
        Binding("escape", "cancel", "back to the problem"),
        Binding("ctrl+s", "save", "save"),
    ]

    def __init__(self, title: str, active_seconds: int, submissions: int, hint_tier: int):
        super().__init__()
        self.problem_title = title
        self.active_seconds = active_seconds
        self.submissions = submissions
        self.hint_tier = hint_tier

    @property
    def _default_verdict(self) -> int:
        """Which verdict the cursor starts on.

        `accepted` normally. Offline it starts on `ungraded`, because there was
        no judge to accept anything — and a default of ACCEPTED is precisely how
        a plane's worth of unverified solves quietly rots the distributions.
        """
        offline = getattr(getattr(self.app, "config", None), "cache", None)
        if offline is not None and offline.offline and "ungraded" in VERDICTS:
            return VERDICTS.index("ungraded")
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
            yield Static("how well will this stick?", classes="field-label")
            with RadioSet(id="confidence"):
                for i, label in enumerate(CONFIDENCE_OPTIONS):
                    yield RadioButton(label, value=(i == 2))
            yield Static("leetcode percentiles — optional", classes="field-label")
            with Horizontal(id="optional-row"):
                yield Input(placeholder="runtime %", id="runtime", type="number")
                yield Input(placeholder="memory %", id="memory", type="number")
            with Horizontal(id="confirm-buttons"):
                yield Button("save  (ctrl+s)", variant="primary", id="save")
                yield Button("back  (esc)", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#verdict", RadioSet).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
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

    def action_save(self) -> None:
        verdict_index = self.query_one("#verdict", RadioSet).pressed_index
        confidence_index = self.query_one("#confidence", RadioSet).pressed_index
        self.dismiss(
            {
                "verdict": VERDICTS[
                    verdict_index if verdict_index >= 0 else self._default_verdict
                ],
                "self_confidence": (confidence_index + 1) if confidence_index >= 0 else None,
                "lc_runtime_pct": self._pct(self.query_one("#runtime", Input).value),
                "lc_memory_pct": self._pct(self.query_one("#memory", Input).value),
            }
        )

    def action_cancel(self) -> None:
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
