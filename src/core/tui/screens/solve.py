"""The active-problem screen — the run loop (spec §15.3).

Sequence per problem: solve in the browser → finish → verdict prompt → both
`$EDITOR` capture steps → next problem. The screen owns no state of its own:
everything it shows is read back off the engine, and every keystroke that
matters writes an event before it changes anything on screen.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static
from textual.worker import Worker

from ... import branding, capture
from ...engine import MAX_HINT_TIER, RunEngine
from ...render import DIFFICULTY_STYLE, bar
from ...scoring import HINT_TIER_NAMES, fmt_duration
from ..vim import MOTIONS, VimMotion
from .finish import ConfirmModal, FinishModal


class SolveScreen(VimMotion, Screen[None]):
    # Hint is `?` and give-up is `x` because `h` and `g` are motions everywhere
    # else. Both of those keys used to live here, and both are irreversible: a
    # reflex `h` while reading the hint panel would have revealed the next tier
    # and cost real points. Motion keys don't get to do that.
    BINDINGS = [
        *MOTIONS,
        Binding("o", "open_url", "open"),
        Binding("p", "pause", "pause"),
        Binding("question_mark", "hint", "hint"),
        Binding("s", "submit", "failed submit"),
        Binding("f", "finish", "finish"),
        Binding("x", "give_up", "give up"),
        Binding("q", "end_run", "end run"),
    ]

    VIM_TARGET = "#solve-body"

    def __init__(self, engine: RunEngine):
        super().__init__()
        self.engine = engine
        self._busy = False

    def compose(self) -> ComposeResult:
        # Scrollable because the hint panel grows: on a short terminal a tier-3
        # reveal would otherwise push the timer off the bottom with no way back.
        with VerticalScroll(id="solve-body"):
            yield Static(id="progress")
            yield Static(id="problem-title")
            yield Static(id="problem-meta")
            yield Static(id="problem-url")
            yield Static(id="timer")
            yield Static(id="attempt-state")
            yield Static(id="toast")
            yield Static(id="hint-panel")
        yield Footer()

    def on_mount(self) -> None:
        self._next_problem()
        self.set_interval(0.5, self._tick)

    # --- problem lifecycle ------------------------------------------------

    def _next_problem(self) -> None:
        session = self.engine.session
        if session is None:
            return
        remaining = session.remaining
        if not remaining:
            self._show_summary()
            return
        self.engine.start_problem(remaining[0])
        self._render_problem()
        self._toast("solve it in the browser — o opens it")

    def _show_summary(self) -> None:
        self.app.show_summary()  # type: ignore[attr-defined]

    def _render_problem(self) -> None:
        attempt = self.engine.attempt
        session = self.engine.session
        if attempt is None or session is None:
            return
        p = attempt.problem

        self.query_one("#progress", Static).update(
            f"  problem {session.index + 1} of {len(session.slugs)}"
        )
        title = Text(p.title, style="bold")
        title.append("   ")
        title.append(f"[{p.difficulty_label}]", style=DIFFICULTY_STYLE.get(p.difficulty, "white"))
        self.query_one("#problem-title", Static).update(title)
        self.query_one("#problem-meta", Static).update(
            f"{p.pattern or '—'}  ·  {', '.join(p.tags)}"
        )
        self.query_one("#problem-url", Static).update(p.url)

        panel = self.query_one("#hint-panel", Static)
        panel.remove_class("visible")
        panel.update("")
        self._tick()

    def _tick(self) -> None:
        attempt = self.engine.attempt
        if attempt is None:
            return
        w = self.app.weights  # type: ignore[attr-defined]
        par = w.par_for(attempt.problem.difficulty)
        active = attempt.active_seconds
        ratio = active / par if par else 0

        style = "green" if ratio <= 1 else ("yellow" if ratio <= 1.5 else "red")
        clock = Text("  ")
        clock.append(fmt_duration(active), style=f"bold {style}")
        clock.append(f"   (par {fmt_duration(par)})   ", style="bright_black")
        clock.append(bar(min(ratio, 1.0), width=24), style=style)
        if attempt.finished:
            clock.append("   STOPPED", style="bold bright_black")
        elif attempt.paused:
            clock.append("   PAUSED", style="bold yellow")
        self.query_one("#timer", Static).update(clock)

        state = Text("  ")
        state.append("hints ", style="bright_black")
        state.append(
            f"tier {attempt.max_hint_tier}" if attempt.max_hint_tier else "none",
            style="yellow" if attempt.max_hint_tier else "",
        )
        state.append("   ·   ")
        state.append("failed submits ", style="bright_black")
        state.append(str(attempt.submissions), style="red" if attempt.submissions else "")
        if attempt.total_paused_seconds:
            state.append("   ·   ")
            state.append("paused ", style="bright_black")
            state.append(fmt_duration(attempt.total_paused_seconds))
        self.query_one("#attempt-state", Static).update(state)

    def _toast(self, message: str, style: str = "") -> None:
        self.query_one("#toast", Static).update(Text(f"  {message}", style=style))

    # --- actions ----------------------------------------------------------

    def action_open_url(self) -> None:
        attempt = self.engine.attempt
        if attempt is None:
            return
        if capture.open_url(attempt.problem.url):
            self._toast(f"opened {attempt.problem.url}")
        else:
            self._toast(f"couldn't open a browser — {attempt.problem.url}", "yellow")

    def action_pause(self) -> None:
        if self.engine.attempt is None:
            return
        paused = self.engine.toggle_pause()
        self._toast("paused — timer stopped, the pause is logged" if paused else "resumed")
        self._tick()

    def action_hint(self) -> None:
        attempt = self.engine.attempt
        if attempt is None or self._busy or attempt.finished:
            return
        if attempt.max_hint_tier >= MAX_HINT_TIER:
            return
        self._reveal_hint()

    def _reveal_hint(self) -> None:
        tier, text = self.engine.reveal_hint()
        panel = self.query_one("#hint-panel", Static)
        header = Text()
        header.append(f"tier {tier} — {HINT_TIER_NAMES[tier]}\n", style="bold yellow")
        header.append(text)
        panel.update(header)
        panel.add_class("visible")
        if tier == MAX_HINT_TIER:
            # The engine has already recorded this as gave_up (spec §13). Read
            # the solution properly, then f moves on to the capture step.
            self._toast(
                "recorded as gave_up — read it properly, then f to write it up",
                "yellow",
            )
        self._tick()

    def action_submit(self) -> None:
        attempt = self.engine.attempt
        if attempt is None or self._busy or attempt.finished:
            return
        self._submit_flow()

    def action_finish(self) -> None:
        attempt = self.engine.attempt
        if attempt is None or self._busy:
            return
        if attempt.finished:
            # A tier-4 hint already ended it; all that's left is the write-up.
            self.run_worker(self._capture_flow(), exclusive=True)
            return
        self._finish_flow()

    def action_give_up(self) -> None:
        attempt = self.engine.attempt
        if attempt is None or self._busy or attempt.finished:
            return
        self._give_up_flow()

    def action_end_run(self) -> None:
        if self._busy:
            return
        self._end_run_flow()

    # --- flows ------------------------------------------------------------

    def _submit_flow(self) -> Worker:
        return self.run_worker(self._do_submit(), exclusive=True)

    async def _do_submit(self) -> None:
        """Log a failed submit, then offer to archive the code behind it."""
        attempt = self.engine.attempt
        if attempt is None:
            return
        self._busy = True
        try:
            # The clock reading goes in the buffer header, so take it before
            # anything else: it is how far in you were when this looked right.
            active = attempt.active_seconds
            n = self.engine.record_submission("wrong_answer")
            self._toast(f"logged failed submit #{attempt.submissions}", "red")
            self._tick()

            cfg = self.app.config  # type: ignore[attr-defined]
            if not (cfg.capture.enabled and cfg.capture.on_failed_submit):
                return
            if not capture.editor_available():
                self._toast(
                    f"failed submit #{attempt.submissions} logged — no $EDITOR, so no code kept",
                    "yellow",
                )
                return

            row = {**(self.engine.attempt_row() or {}), "active_seconds": active}
            result = capture.CaptureResult(False)
            # Pasting a wrong answer is not solve time. The attempt is still
            # running, so the only honest way to not bill it is the pause the
            # user could have taken by hand — logged in `paused_seconds` like
            # any other, and visible on the screen the whole time.
            was_paused = attempt.paused
            if not was_paused:
                self.engine.pause()
            try:
                with self.app.editor_context():  # type: ignore[attr-defined]
                    result = capture.capture_submission(
                        attempt.problem, row, attempt.id, n, cfg.capture.language
                    )
                if result.saved and result.path:
                    self.engine.archive_submission(str(result.path), n, cfg.capture.language)
            except SuspendNotSupported:
                self._toast("this terminal can't hand off to $EDITOR", "yellow")
                return
            except Exception:
                # Same rule as the post-solve capture: a broken editor handoff
                # never costs you the thing that was already recorded.
                self._toast("the editor handoff failed — the submit is still logged", "yellow")
                return
            finally:
                if not was_paused:
                    self.engine.resume()
                self._tick()

            self._toast(
                f"failed submit #{attempt.submissions} — "
                f"{'wrong answer archived' if result.saved else 'code skipped'}",
                "red",
            )
        finally:
            self._busy = False

    def _finish_flow(self) -> Worker:
        return self.run_worker(self._do_finish(), exclusive=True)

    async def _do_finish(self) -> None:
        attempt = self.engine.attempt
        if attempt is None:
            return
        self._busy = True
        try:
            # Freeze the clock now, not when the modal closes: filling in the
            # verdict is not solve time, and it is not a pause either.
            timing = attempt.timing()
            result = await self.app.push_screen_wait(
                FinishModal(
                    attempt.problem.title,
                    timing["active_seconds"],
                    attempt.submissions,
                    attempt.max_hint_tier,
                )
            )
            if result is None:
                self._toast("back to the problem")
                return

            cfg = self.app.config  # type: ignore[attr-defined]
            self.engine.finish(
                result["verdict"],
                timing=timing,
                self_confidence=result.get("self_confidence"),
                lc_runtime_pct=result.get("lc_runtime_pct"),
                lc_memory_pct=result.get("lc_memory_pct"),
                language=cfg.capture.language,
            )
            await self._capture_flow()
        finally:
            self._busy = False

    def _give_up_flow(self) -> Worker:
        return self.run_worker(self._do_give_up(), exclusive=True)

    async def _do_give_up(self) -> None:
        attempt = self.engine.attempt
        if attempt is None:
            return
        self._busy = True
        try:
            timing = attempt.timing()
            ok = await self.app.push_screen_wait(
                ConfirmModal(
                    "Give up on this one?",
                    "Scores 0 — but the attempt is still recorded, and partial "
                    "code is still worth archiving.",
                    yes_label="give up",
                    no_label="keep going",
                )
            )
            if not ok:
                self._toast("keep going")
                return
            self.engine.abandon(timing=timing)
            await self._capture_flow()
        finally:
            self._busy = False

    async def _capture_flow(self, advance: bool = True) -> None:
        """Both `$EDITOR` handoffs, then move to the next problem (spec §7)."""
        attempt = self.engine.attempt
        if attempt is None:
            return
        cfg = self.app.config  # type: ignore[attr-defined]
        row = self.engine.attempt_row() or {}
        code = note = capture.CaptureResult(False)
        blocked = ""

        if not cfg.capture.enabled:
            blocked = "capture is disabled in config.toml"
        elif not capture.editor_available():
            blocked = f"no $EDITOR found — set it, then check `{branding.COMMAND} doctor`"
        else:
            try:
                with self.app.editor_context():
                    code = capture.capture_solution(
                        attempt.problem, row, attempt.id, cfg.capture.language
                    )
                    if code.saved and code.path:
                        self.engine.archive_code(str(code.path), cfg.capture.language)

                    note = capture.capture_note(attempt.problem, attempt.id)
                    if note.saved and note.path:
                        self.engine.record_note(str(note.path))
            except SuspendNotSupported:
                blocked = "this terminal can't hand off to $EDITOR"
            except Exception:
                # A broken editor handoff must never cost you the attempt.
                blocked = "the editor handoff failed"

        if blocked:
            # Say so loudly. Notes are the highest-signal input the system
            # collects and they cannot be written retroactively (spec §7) —
            # silently dropping them for weeks is the worst outcome here.
            self._toast(f"no code or note captured: {blocked}", "yellow")
        else:
            self._toast(
                f"{'code archived' if code.saved else 'code skipped'} · "
                f"{'note written' if note.saved else 'note skipped'}"
            )

        if advance:
            self.engine.advance()
            self._next_problem()

    def _end_run_flow(self) -> Worker:
        return self.run_worker(self._do_end_run(), exclusive=True)

    async def _do_end_run(self) -> None:
        attempt = self.engine.attempt
        self._busy = True
        try:
            if attempt is not None and not attempt.finished:
                timing = attempt.timing()
                ok = await self.app.push_screen_wait(
                    ConfirmModal(
                        "End the run here?",
                        "The problem in progress is recorded as gave_up.",
                        yes_label="end run",
                        no_label="keep going",
                    )
                )
                if not ok:
                    return
                self.engine.abandon(timing=timing)
                # Keep partial code on gave_up (spec §16.2): diffing it against
                # the eventual solution is one of the most instructive artifacts
                # this system can produce, and it's nearly free.
                await self._capture_flow(advance=False)
            self._show_summary()
        finally:
            self._busy = False
