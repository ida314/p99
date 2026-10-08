"""The active-problem screen — the run loop (spec §15.3).

Sequence per problem: solve in the browser → finish → verdict prompt → both
`$EDITOR` capture steps → next problem. The screen owns no state of its own:
everything it shows is read back off the engine, and every keystroke that
matters writes an event before it changes anything on screen.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static
from textual.worker import Worker

from ... import (
    audio,
    branding,
    cache,
    capture,
    events,
    methods,
    problemtypes,
    scoring,
    stats,
    strategies,
)
from ...catalog import Problem
from ...engine import MAX_HINT_TIER, RunEngine
from ...render import (
    DIFFICULTY_STYLE,
    bar,
    last_attempt_line,
    past_attempts_panel,
    past_methods_panel,
)
from ...scoring import HINT_TIER_NAMES, fmt_duration
from ..vim import MOTIONS, VimMotion
from .finish import (
    END_RUN_DISCARD,
    SIGNAL_BACK,
    SIGNAL_DISCARD,
    ConfirmModal,
    EndRunModal,
    FinishModal,
)
from .methods import MethodsModal
from .strategy import StrategyModal

#: The prompts a finish walks through, in order. The first is always asked; the
#: other two are asked when the problem's type, the settings and the verdict all
#: say so -- see `SolveScreen._steps`.
STEP_VERDICT = "verdict"
STEP_PATTERNS = "patterns"
STEP_METHODS = "methods"

#: How often the run writes down where it is, so a process that is killed rather
#: than quit can be picked up. Costs one UPSERT against a one-row table — less
#: than the transaction `events.append` already runs for every hint and submit —
#: and caps what a crash can take off the clock at ten seconds.
CHECKPOINT_SECONDS = 10.0


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
        # Your methods and their variants: the answer, in your own words, so
        # behind a key of its own rather than `r`'s, which is only times.
        Binding("m", "toggle_methods", "methods"),
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
        # Shown only while there is a problem behind you to go back to: see
        # `check_action`. `b` and not `ctrl+b`, which is a motion.
        Binding("b", "go_back", "back"),
    ]

    VIM_TARGET = "#solve-body"

    def __init__(self, engine: RunEngine):
        super().__init__()
        self.engine = engine
        self._busy = False
        # Read once per problem rather than on every keypress: it is a database
        # query, and nothing can add to it while the problem is on screen.
        self._past_attempts: list[stats.PastAttempt] = []
        #: The `m` panel, or None when this problem has no methods to show.
        self._methods_text = None
        # Speech mode's recorder for the problem on screen. None whenever the
        # run isn't recording, which is the only check any caller has to make.
        self._recorder: audio.Recorder | None = None

    @property
    def _ptype(self) -> problemtypes.ProblemType:
        """What kind of problem is on screen, and so what this screen offers.

        Read off the problem rather than off the run: a run is a list of slugs,
        and nothing stops one from holding a LeetCode problem and a design side
        by side. Every key below that a type can switch off asks this first.
        """
        attempt = self.engine.attempt
        return problemtypes.load(attempt.problem.type if attempt is not None else None)

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
            yield Static(id="past-methods")
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
        self.set_interval(CHECKPOINT_SECONDS, self.engine.checkpoint)

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
        previous = self.engine.previous
        # The type's own words for where this gets solved: a browser tab for one
        # kind of problem, a whiteboard for another.
        prompt = self._ptype.solve_prompt
        if previous is not None:
            # Said here, the one moment it is worth saying: a `next problem`
            # that meant `again` is noticed on this screen or not at all.
            self._toast(f"{prompt}, b goes back to {previous.problem.title}")
        else:
            self._toast(prompt)
        self._start_recording()

    def _show_summary(self) -> None:
        self.app.show_summary()  # type: ignore[attr-defined]

    def _render_problem(self) -> None:
        attempt = self.engine.attempt
        session = self.engine.session
        if attempt is None or session is None:
            return
        p = attempt.problem

        progress = f"  problem {session.index + 1} of {len(session.slugs)}"
        if attempt.solves > 1:
            # Which pass you are on, because two runs at one problem in one
            # sitting is exactly the situation where the screen has to say.
            progress += f"  ·  solve {attempt.solves}"
        self.query_one("#progress", Static).update(progress)
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
        self.refresh_bindings()
        self._tick()

    # The strategies you named on this problem are deliberately *not* shown
    # here, and this is the note that stops someone adding them. (Your methods
    # are, behind `m` and only behind it: hidden is the default, and reading
    # them is a choice you make with a keypress, like the categories.) `PastAttempt`
    # already withholds your code and your notes on the reasoning that reopening
    # a problem you have seen before is supposed to still be the problem. The
    # name of the technique is the same spoiler in fewer words -- being told
    # "you solved this with a monotonic stack" on a review is most of the
    # answer, and it would quietly turn every review into a tier-2 hint that
    # nothing logged. They belong on `history`, after the fact, where they are.

    def _load_past_attempts(self, slug: str) -> None:
        """Your own record on this problem: when, how long, how it ended.

        All of it is folded away -- the summary line too -- and `r` unfolds it,
        refolded for every problem, like the categories. A review is a card you
        sit cold until you ask otherwise. None of it shows the code or the note
        -- see `stats.PastAttempt`.
        """
        self._past_attempts = stats.problem_history(
            self.app.conn,  # type: ignore[attr-defined]
            slug,
            weights=self.app.weights,  # type: ignore[attr-defined]
        )
        line = self.query_one("#last-attempt", Static)
        line.update(last_attempt_line(self._past_attempts))
        line.add_class("hidden")
        panel = self.query_one("#past-attempts", Static)
        panel.remove_class("visible")
        panel.update(past_attempts_panel(self._past_attempts))
        conn = self.app.conn  # type: ignore[attr-defined]
        self._methods_text = past_methods_panel(
            methods.for_problem(conn, slug),
            methods.variants_for(conn, slug),
            methods.last_written(conn, slug),
        )
        shelf = self.query_one("#past-methods", Static)
        shelf.remove_class("visible")
        shelf.update(self._methods_text or "")

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
        # The type's own par where it sets one: a design is not measured against
        # the clock a LeetCode problem is.
        w = scoring.for_type(
            self.app.weights,  # type: ignore[attr-defined]
            attempt.problem.type,
        )
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
        if self._ptype.judge:
            state.append("   ·   ")
            state.append("failed submits ", style="bright_black")
            state.append(
                str(attempt.submissions), style="red" if attempt.submissions else ""
            )
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
        if not problem.url and not (self._offline and cache.cacheable(problem)):
            # A problem with nowhere to point. Said rather than left blank: an
            # empty line where the address usually is reads as a screen that
            # failed to draw.
            return Text("nothing to open — this one has no link", style="bright_black")
        if not self._offline or not cache.cacheable(problem):
            # Offline changes nothing for a type with no cache to open instead.
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
        if not target:
            self._toast("nothing to open — this one has no link", "yellow")
            return
        if self._offline and not is_local and cache.cacheable(attempt.problem):
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
        self.query_one("#last-attempt", Static).set_class(
            not panel.has_class("visible"), "hidden"
        )
        if panel.has_class("visible"):
            self._toast("past attempts — time and result, no code or notes")
            panel.scroll_visible(animate=False)
        else:
            self._toast("past attempts hidden")

    def action_toggle_methods(self) -> None:
        """Show or hide the methods you have recorded here, and their variants.

        Unlike `r` this is the answer, or most of it, which is why it is a key of
        its own: nothing is logged, but you have to mean it.
        """
        panel = self.query_one("#past-methods", Static)
        if not self._methods_text:
            self._toast("no methods recorded for this one yet")
            return
        panel.toggle_class("visible")
        if panel.has_class("visible"):
            self._toast("your methods — best variant first")
            panel.scroll_visible(animate=False)
        else:
            self._toast("methods hidden")

    def action_submit(self) -> None:
        attempt = self.engine.attempt
        if attempt is None or self._busy or attempt.finished:
            return
        if not self._ptype.judge:
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
        attempt = self.engine.attempt
        if attempt is not None and attempt.solves > 1:
            # A suspend records the attempt's clock, and on a later pass that
            # clock is this pass's -- writing it back would overwrite the first
            # solve's timing with the rerun's. Finish it or drop it first.
            self._toast("finish or drop this re-solve first", "yellow")
            return
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            recorder.pause()
        self.app.suspend_run()  # type: ignore[attr-defined]

    def action_end_run(self) -> None:
        if self._busy:
            return
        self._end_run_flow()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "go_back":
            return self.engine.previous is not None
        if action == "submit":
            # Off the footer as well as off the key, for a type with nothing to
            # submit to. A failed submit costs points, and offering to log one
            # against a whiteboard is offering to charge for nothing.
            return self._ptype.judge
        return True

    def action_go_back(self) -> None:
        """Back to the problem just finished, for the pass `next problem` skipped."""
        if self._busy or self.engine.previous is None:
            return
        if not self.engine.can_go_back():
            self._toast(
                "a hint or submit is logged on this one — finish it or give it up first",
                "yellow",
            )
            return
        self.run_worker(self._do_go_back(), exclusive=True)

    async def _do_go_back(self) -> None:
        previous = self.engine.previous
        current = self.engine.attempt
        if previous is None:
            return
        self._busy = True
        try:
            self._pause_recording(True)
            ok = await self.app.push_screen_wait(
                ConfirmModal(
                    f"Back to {previous.problem.title}?",
                    "Same problem, fresh clock, recorded under the attempt you "
                    "just finished. "
                    + (
                        f"{current.problem.title} isn't started — it comes up "
                        "again after this."
                        if current is not None
                        else ""
                    ),
                    yes_label="go back",
                    no_label="stay",
                )
            )
            if not ok:
                self._pause_recording(False)
                self._toast("staying on this one")
                self._tick()
                return
            # The microphone was on the problem being thrown away, and goes with
            # it, as in `_do_throw_away`. The rerun gets no recorder: see
            # `_offer_again`.
            self._stop_recording(keep=False)
            self.engine.back_to_previous()
            self._render_problem()
            self._toast("fresh clock — solve it again")
        finally:
            self._busy = False

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
                language = capture.language_for(attempt.problem, cfg.capture.language)
                with self.app.editor_context():  # type: ignore[attr-defined]
                    result = capture.capture_submission(
                        attempt.problem, row, attempt.id, n, language
                    )
                if result.saved and result.path:
                    self.engine.archive_submission(str(result.path), n, language)
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

            # The post-solve prompts, walked as a cursor rather than a chain of
            # awaits. `esc` on any of them steps back exactly one screen --
            # methods to patterns, patterns to verdict, verdict to the problem
            # -- and stepping back has to be repeatable: one back that works and
            # a second that dumps you somewhere else is worse than none. Every
            # screen reopens carrying what it last held, so a round trip changes
            # nothing.
            #
            # Which of them run is the problem type's to say, so "one screen
            # back" is one screen back among the ones that ran: a type with no
            # patterns prompt steps from methods straight to the verdict,
            # rather than into a screen that would answer itself and bounce you
            # forward again.
            #
            # Safe because nothing is written until `engine.finish` below.
            # Everything above that line is a prompt, and the attempt is still
            # live behind it.
            answers: dict[str, Any] = {}
            # `None` until the patterns prompt has been answered once, because
            # the prompt opens with the problem's saved tags ticked and only a
            # real previous answer may overrule that -- an empty list here means
            # "you unticked them all", not "nothing yet". See `StrategyModal`.
            used: list[tuple[str, str]] | None = None
            ways: list[dict[str, Any]] = []
            chosen: dict | None = None
            methods_block: list[dict[str, Any]] | None = None

            step: str | None = STEP_VERDICT
            while step is not None:
                if step == STEP_VERDICT:
                    result = await self.app.push_screen_wait(
                        FinishModal(
                            attempt.problem.title,
                            timing["active_seconds"],
                            attempt.submissions,
                            attempt.max_hint_tier,
                            answers=answers,
                            ptype=self._ptype,
                        )
                    )
                    if result is None:
                        self._pause_recording(False)
                        self._toast("back to the problem")
                        self._tick()
                        return
                    if result.get(SIGNAL_DISCARD):
                        await self._do_throw_away()
                        return
                    answers = result
                    steps = self._steps(answers)
                    # A prompt the new verdict no longer asks for takes its
                    # answer with it. Coming back to change `solved` into
                    # `gave up` must not carry the patterns you ticked for the
                    # solve you have just said you did not reach.
                    if STEP_PATTERNS not in steps:
                        chosen = None
                    if STEP_METHODS not in steps:
                        methods_block = None
                    step = self._after(steps, step)
                    continue

                if step == STEP_PATTERNS:
                    chosen = await self._ask_strategies(answers, used)
                    if isinstance(chosen, dict) and chosen.get(SIGNAL_BACK):
                        used = chosen.get("picked") or []
                        self._toast("back to the verdict")
                        step = STEP_VERDICT
                        continue
                    used = self._used_pairs(chosen)
                    step = self._after(self._steps(answers), step)
                    continue

                methods_block = await self._ask_methods(answers, ways)
                if isinstance(methods_block, dict) and methods_block.get(SIGNAL_BACK):
                    ways = methods_block.get("picked") or []
                    methods_block = None
                    step = self._before(self._steps(answers), step)
                    self._toast(f"back to the {step}")
                    continue
                step = None

            cfg = self.app.config  # type: ignore[attr-defined]
            # The form's answers, sorted into the ones that have a column and
            # the ones that ride the `answers` block. One flat form on screen,
            # two places in the log, and `problemtypes.split` is the only thing
            # that knows which is which.
            columns, rest = problemtypes.split(answers)
            self.engine.finish(
                answers["verdict"],
                timing=timing,
                self_confidence=answers.get("self_confidence"),
                language=capture.language_for(attempt.problem, cfg.capture.language),
                answers=rest,
                strategies=chosen if isinstance(chosen, dict) else None,
                methods=methods_block if isinstance(methods_block, list) else None,
                **columns,
            )
            self._record_tags(attempt.problem.slug, chosen)
            self._stop_recording()
            await self._capture_flow(ways=methods_block)
        finally:
            self._busy = False

    # --- which prompts a finish asks ----------------------------------------

    def _asks_about_the_solve(self, answers: dict) -> bool:
        """Is there a solve to ask about at all.

        Only for one. There is no approach to record on an attempt that never
        reached one, which is the same reason `engine.finish` drops the
        complexity claims on the way to `abandon`. And `[strategy] enabled` in
        config.toml still switches both prompts off for every type at once.
        """
        cfg = self.app.config  # type: ignore[attr-defined]
        return (
            self.engine.attempt is not None
            and cfg.strategy.enabled
            and answers.get("verdict") in scoring.CLEAN_VERDICTS
        )

    def _steps(self, answers: dict) -> list[str]:
        """The prompts this finish walks through, in order.

        Computed from the verdict as it now stands, so it is asked again after
        every trip back to the verdict prompt rather than once at the top.
        """
        steps = [STEP_VERDICT]
        if self._asks_about_the_solve(answers):
            if self._ptype.screens.patterns:
                steps.append(STEP_PATTERNS)
            if self._ptype.screens.methods:
                steps.append(STEP_METHODS)
        return steps

    @staticmethod
    def _after(steps: list[str], step: str) -> str | None:
        """The prompt that follows `step`, or None when it was the last."""
        index = steps.index(step) + 1 if step in steps else len(steps)
        return steps[index] if index < len(steps) else None

    @staticmethod
    def _before(steps: list[str], step: str) -> str:
        """The prompt `esc` steps back to. Never further than the verdict."""
        index = steps.index(step) - 1 if step in steps else 0
        return steps[max(0, index)]

    def _record_tags(self, slug: str, chosen: Any) -> None:
        """Set this problem's tags to what the patterns prompt came back with.

        After `engine.finish` and not folded into it, because this is not an
        answer about the attempt. `problem_finished` carries the same names --
        that is what `attempt_strategies` is built from, the tags this problem
        wore on the night of this solve -- while the problem's live list is set
        here, by an event that carries the whole list and so can express a tag
        being taken off. See `strategies.set_payload`.

        Sent whenever the prompt was answered, including with nothing ticked:
        clearing the list is exactly the edit that needs an event of its own.
        Skipped entirely when the prompt never ran, since a question nobody was
        asked cannot have emptied anything.
        """
        if not isinstance(chosen, dict) or chosen.get(SIGNAL_BACK):
            return
        events.append(
            self.app.conn,  # type: ignore[attr-defined]
            events.PROBLEM_STRATEGIES_SET,
            strategies.set_payload(slug, chosen.get(strategies.USED) or []),
        )

    @staticmethod
    def _used_pairs(chosen: Any) -> list[tuple[str, str]]:
        """The `(key, name)` of every pattern the patterns prompt came back with.

        Both halves, because stepping back has to be a round trip: the key is
        what the list is rebuilt against and the name is the only record of a
        strategy you typed one screen ago and have not committed yet.
        """
        if not isinstance(chosen, dict):
            return []
        return [
            (entry.key, entry.name)
            for entry in strategies.clean(chosen.get(strategies.USED) or [])
        ]

    async def _ask_strategies(self, answers: dict, used: list | None) -> dict | None:
        """Which patterns can solve this problem, from the shared vocabulary.

        Before `engine.finish`, not after: the strategy rows have to exist by the
        time the card is graded. Asking afterwards would need a second event and
        a regrade.

        Only for a solve. There is no approach to record on an attempt that
        never reached one, which is the same reason `engine.finish` drops the
        complexity claims on the way to `abandon`.

        Returns a payload block, None for nothing picked, or the `back` signal
        the caller steps on.
        """
        attempt = self.engine.attempt
        if attempt is None or STEP_PATTERNS not in self._steps(answers):
            return None
        return await self.app.push_screen_wait(
            StrategyModal(attempt.problem.title, attempt.problem.slug, used)
        )

    async def _ask_methods(self, answers: dict, ways: list) -> Any:
        """The ways this problem can be solved, after the patterns you used.

        Also before `engine.finish`, and for a sharper reason than the strategy
        prompt: `srs.rate` reads whether an optimal method is recorded here that
        is not the one you wrote, so these rows have to be in place before the
        card is folded. That ordering is the whole reason both answers ride the
        `problem_finished` payload instead of arriving as later events.

        Takes no strategy answer, and that is the point of the change: a method is
        the problem's own route and is never assembled out of the patterns you
        just named. See `methods`.
        """
        attempt = self.engine.attempt
        if attempt is None or STEP_METHODS not in self._steps(answers):
            return None
        return await self.app.push_screen_wait(
            MethodsModal(
                attempt.problem.title,
                attempt.problem.slug,
                # The type rides along so the screen knows whether there is a
                # time answer to read a quality off at all.
                {**answers, "type": attempt.problem.type},
                ways,
            )
        )

    async def _do_throw_away(self) -> None:
        """Drop the attempt entirely, then move on. Confirmed, because it is final.

        No capture step: there is nothing to archive against an attempt that
        will not exist. Declining leaves the problem exactly as it was, still
        running — `f` reopens the verdict prompt.

        On a later pass it throws away the pass and not the attempt. The attempt
        was sealed when its own finish was logged, and a rerun you did not like
        is not grounds for deleting the solve that earned the score.
        """
        attempt = self.engine.attempt
        again = attempt is not None and attempt.solves > 1
        ok = await self.app.push_screen_wait(
            ConfirmModal(
                "Throw this re-solve away?" if again else "Throw this attempt away?",
                "The solve you already recorded is untouched — only this "
                "second pass is dropped."
                if again
                else "It is not recorded at all — no score, no review scheduled, "
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

    @staticmethod
    def _method_tag(ways: Any) -> methods.Named | None:
        """The method the archived file is for, or None.

        One tag, because one solve writes one file. When you marked two methods
        tonight -- which is what solving it twice looks like -- the file is tagged
        with neither rather than with a guess: a wrong tag says something false
        about which route the code is, and no tag only leaves the row unwritten.
        """
        if not isinstance(ways, list):
            return None
        named = methods.clean(
            [str(w.get("name") or "") for w in ways if isinstance(w, dict) and w.get("used")]
        )
        return named[0] if len(named) == 1 else None

    async def _capture_flow(self, advance: bool = True, ways: Any = None) -> None:
        """Both `$EDITOR` handoffs, then the offer of another pass (spec §7).

        One handoff for the solution and one for the reflection note: a night has
        one of each in it. The method you marked rides along as a tag -- it is
        stamped into the file's header and attached to that method's row, which
        is what lets the methods screen say which routes you have written.

        Every file written here is named for the pass that wrote it, so solving
        the problem again does not overwrite what the first pass produced.
        """
        attempt = self.engine.attempt
        if attempt is None:
            return
        cfg = self.app.config  # type: ignore[attr-defined]
        row = self.engine.attempt_row() or {}
        code = capture.CaptureResult(False)
        note = capture.CaptureResult(False)
        method = self._method_tag(ways)
        # Which of the two handoffs this kind of problem takes. Both, for
        # LeetCode; a type may turn either off, and one that turns off both has
        # nothing to capture and is told nothing about it.
        screens = self._ptype.screens
        wanted = screens.solution or screens.note
        language = capture.language_for(attempt.problem, cfg.capture.language)
        blocked = ""

        if not wanted:
            pass
        elif not cfg.capture.enabled:
            blocked = "capture is disabled in config.toml"
        elif not capture.editor_available():
            blocked = f"no $EDITOR found — set it, then check `{branding.COMMAND} doctor`"
        else:
            try:
                with self.app.editor_context():
                    if screens.solution:
                        code = capture.capture_solution(
                            attempt.problem,
                            row,
                            attempt.id,
                            language,
                            method,
                            again=attempt.solves,
                        )
                        if code.saved and code.path:
                            self.engine.archive_code(
                                str(code.path),
                                language,
                                method.name if method else None,
                            )

                    if screens.note:
                        note = capture.capture_note(
                            attempt.problem, attempt.id, again=attempt.solves
                        )
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
        elif wanted:
            said = []
            if screens.solution:
                said.append("code archived" if code.saved else "code skipped")
            if screens.note:
                said.append("note written" if note.saved else "note skipped")
            self._toast(" · ".join(said))

        if not advance:
            return
        if await self._offer_again():
            return
        self.engine.advance()
        self._next_problem()

    async def _offer_again(self) -> bool:
        """Ask whether to solve the same problem again, right now.

        Here rather than at the verdict prompt because the answer is only worth
        anything once the pass is fully written down: the code is archived, the
        note is written, and the thing you are being offered is a clean second
        run at a problem still fresh in your head.

        Offered after a give-up too, which is the pass where a second go is
        worth the most.
        """
        if self.engine.attempt is None:
            return False
        if not self._ptype.screens.again:
            # Not offered for a kind of problem nobody sits twice in a night.
            return False
        ok = await self.app.push_screen_wait(
            ConfirmModal(
                "Solve it again?",
                "Same problem, fresh clock. It is recorded under this attempt — "
                "it won't re-score it or move the review.",
                yes_label="again",
                no_label="next problem",
            )
        )
        if not ok:
            return False
        self.engine.solve_again()
        # No recorder: speech mode covers the first pass, whose audio the attempt
        # already holds. A second recording would need a file and a column of its
        # own, and going without is better than quietly overwriting the first.
        self._render_problem()
        self._toast("fresh clock — solve it again")
        return True

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
