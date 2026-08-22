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

from ... import audio, branding, cache, capture, scoring, stats
from ...catalog import Problem
from ...engine import MAX_HINT_TIER, RunEngine
from ...render import DIFFICULTY_STYLE, bar, last_attempt_line, past_attempts_panel
from ...scoring import HINT_TIER_NAMES, fmt_duration
from ..vim import MOTIONS, VimMotion
from .finish import END_RUN_DISCARD, ConfirmModal, EndRunModal, FinishModal
from .strategy import StrategyModal


class SolveScreen(VimMotion, Screen[None]):
    # Hint is `?` and give-up is `x` because `h` and `g` are motions everywhere
    # else. Both of those keys used to live here, and both are irreversible: a
    # reflex `h` while reading the hint panel would have revealed the next tier
    # and cost real points. Motion keys don't get to do that.
    BINDINGS = [
        *MOTIONS,
        Binding("o", "open_url", "open"),
        Binding("p", "pause", "pause"),
        Binding("c", "toggle_categories", "categories"),
        # `r` for runs, the same mnemonic history has on the home menu — and for
        # the same reason it isn't `h` there either: `h` is a motion.
        Binding("r", "toggle_past", "attempts"),
        Binding("question_mark", "hint", "hint"),
        Binding("s", "submit", "failed submit"),
        Binding("f", "finish", "finish"),
        Binding("x", "give_up", "give up"),
        # `z` for the break you come back from, next to `x` for the one you
        # don't. No confirmation, because unlike everything else on this row it
        # costs nothing: `c` on the home screen hands the problem back with the
        # clock reading exactly what it reads now.
        Binding("z", "suspend", "suspend"),
        Binding("q", "end_run", "end run"),
    ]

    VIM_TARGET = "#solve-body"

    def __init__(self, engine: RunEngine):
        super().__init__()
        self.engine = engine
        self._busy = False
        # Read once per problem rather than on every keypress: it is a database
        # query, and nothing can add to it while the problem is on screen.
        self._past_attempts: list[stats.PastAttempt] = []
        # Speech mode's recorder for the problem on screen. None whenever the
        # run isn't recording, which is the only check any caller has to make.
        self._recorder: audio.Recorder | None = None

    def compose(self) -> ComposeResult:
        # Scrollable because the hint panel grows: on a short terminal a tier-3
        # reveal would otherwise push the timer off the bottom with no way back.
        with VerticalScroll(id="solve-body"):
            yield Static(id="progress")
            yield Static(id="problem-title")
            yield Static(id="problem-meta")
            yield Static(id="problem-url")
            yield Static(id="last-attempt")
            yield Static(id="timer")
            yield Static(id="attempt-state")
            yield Static(id="toast")
            yield Static(id="hint-panel")
            # Last, under the hint panel: `r` must never move the clock or the
            # hint text you are reading, and this one can be a dozen lines long.
            yield Static(id="past-attempts")
        yield Footer()

    def on_mount(self) -> None:
        attempt = self.engine.attempt
        if attempt is not None and not attempt.finished:
            # Resumed: the engine already holds the attempt, rebuilt from its
            # row. Starting a problem here would open a second one on top of it.
            self._render_problem()
            self._toast("resumed — the clock is paused, p starts it")
            self._resume_recording()
        else:
            self._next_problem()
        self.set_interval(0.5, self._tick)

    def on_unmount(self) -> None:
        """A hard quit must never leave ffmpeg holding the microphone.

        Closes the open segment and leaves every piece on disk rather than
        joining them. This screen only ever unmounts holding a live recorder
        when the run is being suspended — by `z`, or by the hard quit that
        `CoreApp.on_unmount` now suspends rather than seals — and a resume picks
        the pieces back up through `Recorder.adopt`. Joining here would end the
        recording halfway through a problem that isn't over.

        Best-effort throughout: the screen is already going away.
        """
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            return
        try:
            recorder.pause()
        except Exception:
            pass

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
        self._start_recording()

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
        # Rendered but hidden: `c` reveals it. Reset on every problem, the same
        # way the hint panel is below — the toggle is a decision you make once
        # per problem, not a mode you leave on for the rest of the run.
        meta = self.query_one("#problem-meta", Static)
        meta.update(f"{p.pattern or '—'}  ·  {', '.join(p.tags)}")
        meta.remove_class("visible")
        self.query_one("#problem-url", Static).update(self._where_line(p))
        self._load_past_attempts(p.slug)

        panel = self.query_one("#hint-panel", Static)
        panel.remove_class("visible")
        panel.update("")
        self._tick()

    # The strategies you named on this problem are deliberately *not* shown
    # here, and this is the note that stops someone adding them. `PastAttempt`
    # already withholds your code and your notes on the reasoning that reopening
    # a problem you have seen before is supposed to still be the problem. The
    # name of the technique is the same spoiler in fewer words -- being told
    # "you solved this with a monotonic stack" on a review is most of the
    # answer, and it would quietly turn every review into a tier-2 hint that
    # nothing logged. They belong on `history`, after the fact, where they are.

    def _load_past_attempts(self, slug: str) -> None:
        """Your own record on this problem: when, how long, how it ended.

        The summary line stays up; the full table is folded away behind `r` and
        refolded for every problem, like the categories. Neither shows the code
        or the note — see `stats.PastAttempt`.
        """
        self._past_attempts = stats.problem_history(
            self.app.conn,  # type: ignore[attr-defined]
            slug,
            weights=self.app.weights,  # type: ignore[attr-defined]
        )
        self.query_one("#last-attempt", Static).update(
            last_attempt_line(self._past_attempts)
        )
        panel = self.query_one("#past-attempts", Static)
        panel.remove_class("visible")
        panel.update(past_attempts_panel(self._past_attempts))

    # --- speech mode ------------------------------------------------------
    #
    # The recorder tracks the clock exactly: every place the attempt pauses, it
    # pauses, and it stops the moment the attempt does. What it must never do is
    # cost you an attempt — a missing ffmpeg, a dead microphone or a failed join
    # is a yellow toast and nothing more.

    def _new_recorder(self) -> audio.Recorder | None:
        """A recorder for the attempt on screen, or None if this run isn't recording."""
        attempt = self.engine.attempt
        if attempt is None or not self.app.speech_mode:  # type: ignore[attr-defined]
            return None
        if not audio.available():
            self._toast("speech mode is on but there's no ffmpeg — not recording", "yellow")
            return None
        cfg = self.app.config.audio  # type: ignore[attr-defined]
        return audio.Recorder(
            attempt.problem.slug,
            attempt.id,
            bitrate_kbps=cfg.bitrate_kbps,
            input_format=cfg.input_format,
            device=cfg.device,
        )

    def _start_recording(self) -> None:
        self._recorder = None
        recorder = self._new_recorder()
        if recorder is None:
            return
        if not recorder.start():
            self._toast("couldn't open the microphone — not recording", "yellow")
            return
        self._recorder = recorder
        self._tick()

    def _resume_recording(self) -> None:
        """Pick the suspended attempt's recording back up.

        Adopted rather than started, and left closed: the attempt comes back
        paused, so the microphone does too. The first `p` opens the next segment
        through the same path a hand-taken pause uses, and the join at the end
        of the attempt covers both halves as one recording.
        """
        self._recorder = None
        recorder = self._new_recorder()
        if recorder is None:
            return
        if not recorder.adopt():
            # Nothing was recorded before the break — either the run started
            # without a microphone or ffmpeg never opened one. Begin now rather
            # than silently recording nothing for the rest of the attempt.
            self._start_recording()
            return
        self._recorder = recorder
        self._tick()

    def _pause_recording(self, paused: bool) -> None:
        if self._recorder is None:
            return
        if paused:
            self._recorder.pause()
        else:
            self._recorder.resume()

    def _stop_recording(self, keep: bool = True) -> None:
        """Finalize, and hang the file on the attempt while it is still current.

        Must run before `engine.advance()`: `record_audio` addresses the attempt
        the engine is holding, and after an advance there isn't one.
        """
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            return
        if not keep:
            recorder.discard()
            return
        path = recorder.stop()
        if path is None:
            self._toast("the recording couldn't be saved — everything else is logged", "yellow")
            return
        try:
            self.engine.record_audio(str(path))
        except Exception:
            # Same rule as the editor handoff: a bookkeeping failure never costs
            # the attempt, and the file is on disk either way.
            self._toast("recorded, but couldn't log where — check the audio directory", "yellow")

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
        # A microphone that can be on without the screen saying so is not a
        # thing to ship, so this sits on the clock line rather than anywhere it
        # could scroll away.
        if self._recorder is not None:
            if self._recorder.recording:
                clock.append("   ● REC", style="bold red")
            else:
                clock.append("   ● REC PAUSED", style="bold yellow")
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

    @property
    def _offline(self) -> bool:
        return bool(self.app.config.cache.offline)  # type: ignore[attr-defined]

    def _where_line(self, problem: Problem) -> Text:
        """The URL line, which offline becomes the cache indicator.

        Where the problem is coming from belongs where the address already was;
        offline mode is not worth a widget of its own, but silently opening a
        different thing than the line says would be.
        """
        if not self._offline:
            return Text(problem.url)
        if cache.local_path(problem.slug) is not None:
            return Text(f"offline — cached copy of {problem.slug}", style="cyan")
        return Text(
            f"offline — not cached, run `{branding.COMMAND} fetch`", style="yellow"
        )

    def action_open_url(self) -> None:
        attempt = self.engine.attempt
        if attempt is None:
            return
        target, is_local = cache.target_for(attempt.problem, offline=self._offline)
        if self._offline and not is_local:
            self._toast(
                f"not cached — `{branding.COMMAND} fetch` while you have a network",
                "yellow",
            )
            return
        if not capture.open_url(target):
            self._toast(f"couldn't open a browser — {target}", "yellow")
        elif is_local:
            self._toast("opened the cached copy")
        else:
            self._toast(f"opened {target}")

    def action_pause(self) -> None:
        if self.engine.attempt is None:
            return
        paused = self.engine.toggle_pause()
        self._pause_recording(paused)
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
            # The engine has already recorded this as gave_up (spec §13), so the
            # attempt's clock is stopped and the recorder's has to be too —
            # reading a solution out loud is not part of the solve.
            self._stop_recording()
            # Read the solution properly, then f moves on to the capture step.
            self._toast(
                "recorded as gave_up — read it properly, then f to write it up",
                "yellow",
            )
        self._tick()

    def action_toggle_categories(self) -> None:
        """Show or hide the pattern and tags.

        Free to look at, on purpose: the point is that you have to decide to,
        not that you can't. Nothing is logged either way — unlike a hint, this
        tells you what kind of problem it is, not how to solve it.
        """
        meta = self.query_one("#problem-meta", Static)
        meta.toggle_class("visible")
        self._toast("categories shown" if meta.has_class("visible") else "categories hidden")

    def action_toggle_past(self) -> None:
        """Show or hide every past attempt at this problem.

        Free to look at, like the categories and for a stronger reason: this is
        your own record, and none of it says anything about the answer.
        """
        panel = self.query_one("#past-attempts", Static)
        if not self._past_attempts:
            self._toast("first time on this one — no past attempts yet")
            return
        panel.toggle_class("visible")
        if panel.has_class("visible"):
            self._toast("past attempts — time and result, no code or notes")
            panel.scroll_visible(animate=False)
        else:
            self._toast("past attempts hidden")

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

    def action_suspend(self) -> None:
        """Put the run down and come back to it later — today, or next week.

        The opposite of `q`: nothing is graded, nothing is abandoned, and the
        problem keeps its place in the run. The recorder is detached first, while
        the attempt is still the engine's current one, so its segments are left
        where a resume will look for them.
        """
        if self._busy or self.engine.session is None:
            return
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            recorder.pause()
        self.app.suspend_run()  # type: ignore[attr-defined]

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
                self._pause_recording(True)
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
                    self._pause_recording(False)
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
            # Paused, not stopped. `esc` hands you back a problem that is still
            # running, and a stopped recorder could not have picked the rest of
            # it up. Stopping happens once the modal has committed to something.
            self._pause_recording(True)
            result = await self.app.push_screen_wait(
                FinishModal(
                    attempt.problem.title,
                    timing["active_seconds"],
                    attempt.submissions,
                    attempt.max_hint_tier,
                )
            )
            if result is None:
                self._pause_recording(False)
                self._toast("back to the problem")
                self._tick()
                return

            if result.get("discard"):
                await self._do_throw_away()
                return

            cfg = self.app.config  # type: ignore[attr-defined]
            picked = await self._ask_strategies(result)
            self.engine.finish(
                result["verdict"],
                timing=timing,
                self_confidence=result.get("self_confidence"),
                lc_runtime_pct=result.get("lc_runtime_pct"),
                lc_memory_pct=result.get("lc_memory_pct"),
                language=cfg.capture.language,
                claimed_complexity=result.get("claimed_complexity"),
                claimed_space_complexity=result.get("claimed_space_complexity"),
                time_optimality=result.get("time_optimality"),
                space_optimality=result.get("space_optimality"),
                strategies=picked,
            )
            self._stop_recording()
            await self._capture_flow()
        finally:
            self._busy = False

    async def _ask_strategies(self, result: dict) -> dict[str, list[str]] | None:
        """Name the approach, between the verdict prompt and the editor steps.

        Before `engine.finish`, not after: the rating reads whether you named a
        better approach, so the answer has to be on the payload that grades the
        card. Asking afterwards would need a second event and a regrade.

        Only for a solve. There is no approach to record on an attempt that
        never reached one, which is the same reason `engine.finish` drops the
        complexity claims on the way to `abandon`.
        """
        attempt = self.engine.attempt
        cfg = self.app.config  # type: ignore[attr-defined]
        if attempt is None or not cfg.strategy.enabled:
            return None
        if result.get("verdict") not in scoring.CLEAN_VERDICTS:
            return None
        return await self.app.push_screen_wait(
            StrategyModal(attempt.problem.title, attempt.problem.slug, result)
        )

    async def _do_throw_away(self) -> None:
        """Drop the attempt entirely, then move on. Confirmed, because it is final.

        No capture step: there is nothing to archive against an attempt that
        will not exist. Declining leaves the problem exactly as it was, still
        running — `f` reopens the verdict prompt.
        """
        ok = await self.app.push_screen_wait(
            ConfirmModal(
                "Throw this attempt away?",
                "It is not recorded at all — no score, no review scheduled, "
                "nothing in your history.",
                yes_label="throw it away",
                no_label="keep it",
            )
        )
        if not ok:
            self._pause_recording(False)
            self._toast("back to the problem")
            self._tick()
            return
        # Thrown away, so the recording goes with it. The one place this differs
        # from the archived code and notes a discard deliberately leaves alone:
        # those are your writing, and this is a microphone left running over the
        # wrong problem that nothing would ever point at again.
        self._stop_recording(keep=False)
        self.engine.discard()
        self.engine.advance()
        # No toast: `_next_problem` writes its own, and the problem counter
        # moving on is the feedback that the attempt is gone.
        self._next_problem()

    def _give_up_flow(self) -> Worker:
        return self.run_worker(self._do_give_up(), exclusive=True)

    async def _do_give_up(self) -> None:
        attempt = self.engine.attempt
        if attempt is None:
            return
        self._busy = True
        try:
            timing = attempt.timing()
            self._pause_recording(True)
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
                self._pause_recording(False)
                self._toast("keep going")
                self._tick()
                return
            self.engine.abandon(timing=timing)
            self._stop_recording()
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
                self._pause_recording(True)
                choice = await self.app.push_screen_wait(EndRunModal())
                if choice is None:
                    self._pause_recording(False)
                    self._tick()
                    return
                if choice == END_RUN_DISCARD:
                    # A way out that costs nothing and asks for nothing: the
                    # attempt is thrown away exactly as `ctrl+x` throws one away
                    # at the finish prompt, and the capture flow is skipped
                    # because there is no attempt left to archive anything
                    # against. The recording goes too, for the same reason it
                    # does there — see `_do_throw_away`.
                    self._stop_recording(keep=False)
                    self.engine.discard()
                else:
                    self.engine.abandon(timing=timing)
                    self._stop_recording()
                    # Keep partial code on gave_up (spec §16.2): diffing it
                    # against the eventual solution is one of the most
                    # instructive artifacts this system can produce, and it's
                    # nearly free.
                    await self._capture_flow(advance=False)
            self._show_summary()
        finally:
            self._busy = False
