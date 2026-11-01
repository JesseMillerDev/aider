"""Own terminal input while the synchronous agent works; only the agent consumes messages."""

import _thread
import functools
import queue
import re
import threading
import time
from contextlib import ExitStack, contextmanager
from contextvars import copy_context

from prompt_toolkit.application import create_app_session
from prompt_toolkit.completion import ThreadedCompleter
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession
from aider.mdstream import NoInsetMarkdown

_PAUSED = object()
_INTERRUPTED = object()


def suspend_steering(func):
    """Give confirmation/question prompts sole ownership of terminal input."""
    @functools.wraps(func)
    def wrapper(io, *args, **kwargs):
        steering = getattr(io, "steering", None)
        if steering is None or io.yolo or io.yes in (True, False):
            return func(io, *args, **kwargs)
        with steering.suspended():
            return func(io, *args, **kwargs)
    return wrapper


class SteeringInput:
    """A single input reader, a FIFO, and a lossless handoff to ordinary prompts."""

    def __init__(self, io, completer=None):
        self.io = io
        self.messages = io.queued_inputs
        self.draft = ""
        self.thread = None
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.disabled = False
        self.error = None
        self.stack = ExitStack()
        self.session = None
        self.completer = ThreadedCompleter(completer) if completer is not None else None
        self.keys = KeyBindings()

        @self.keys.add("enter")
        def submit(event):
            if io.multiline_mode:
                event.current_buffer.insert_text("\n")
            else:
                event.current_buffer.validate_and_handle()

        @self.keys.add("escape", "enter")
        def alternate_enter(event):
            if io.multiline_mode:
                event.current_buffer.validate_and_handle()
            else:
                event.current_buffer.insert_text("\n")

        @self.keys.add("c-c")
        def interrupt(event):
            event.app.exit(result=_INTERRUPTED)

    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive() and not self.stop.is_set()

    @property
    def pending(self):
        return not self.messages.empty()

    def __enter__(self):
        original = self.io.prompt_session
        try:
            self.stack.enter_context(
                create_app_session(input=original.input, output=original.output)
            )
            self.session = PromptSession(
                input=original.input, output=original.output, history=original.history,
                editing_mode=self.io.editingmode, erase_when_done=True,
            )
            self.stack.enter_context(patch_stdout(raw=True))
            self.resume()
        except BaseException:
            self.stack.close()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self.pause()
        finally:
            self.stack.close()

    def _toolbar(self):
        mode = "YOLO · " if self.io.yolo else ""
        submit = "Alt+Enter" if self.io.multiline_mode else "Enter"
        return (f" {mode}Working · {submit} queues steering · Ctrl+C interrupts"
                f" · {self.messages.qsize()} queued")

    def _pause_prompt(self):
        app = self.session.app
        if app.is_running and app.future is not None and not app.future.done():
            self.draft = app.current_buffer.text
            app.exit(result=_PAUSED)

    def _prompt_ready(self):
        self.ready.set()
        # Covers a stop between prompts, before Application.future existed.
        if self.stop.is_set():
            self._pause_prompt()

    def _read(self):
        interrupted = False
        try:
            while not self.stop.is_set():
                text = self.session.prompt(
                    "› ", default=self.draft, key_bindings=self.keys,
                    completer=self.completer, complete_while_typing=True,
                    style=self.io._get_style(), bottom_toolbar=self._toolbar,
                    refresh_interval=.2, pre_run=self._prompt_ready, handle_sigint=False,
                )
                if text is _PAUSED:
                    break
                if text is _INTERRUPTED:
                    interrupted = True
                    break
                self.draft = ""
                if text.strip():
                    self.messages.put(text)
                    print(f"Queued steering ({self.messages.qsize()}): {text}", flush=True)
        except EOFError:
            self.disabled = True
        except Exception as err:
            self.error = err
            self.disabled = True
        finally:
            self.stop.set()
            self.ready.set()
        if interrupted:
            _thread.interrupt_main()

    def resume(self):
        if self.disabled or self.active:
            return
        self.stop.clear()
        self.ready.clear()
        self.thread = threading.Thread(
            target=copy_context().run, args=(self._read,), name="aider-steering-input", daemon=True,
        )
        self.thread.start()
        if not self.ready.wait(3):
            self.pause()
            raise RuntimeError("Steering prompt did not start; terminal input was not handed off.")
        if self.error:
            self.io.tool_warning(f"Steering input unavailable: {self.error}")

    def pause(self):
        if self.thread is None:
            return
        self.stop.set()
        loop = self.session.app.loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._pause_prompt)
        # Completion may be finishing the bounded workspace inventory (up to 10s).
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RuntimeError("Steering reader did not stop; refusing competing terminal readers.")
        self.thread = None

    @contextmanager
    def suspended(self):
        resume = self.active
        self.pause()
        try:
            yield
        finally:
            if resume:
                self.resume()


def take_queued_input(io):
    try:
        text = io.queued_inputs.get_nowait()
    except queue.Empty:
        return None
    io.user_input(text)
    return text


class ParagraphStream:
    """Append completed Markdown blocks; avoid competing cursor movement from Rich Live."""

    def __init__(self, console, mdargs):
        self.console = console
        self.mdargs = mdargs
        self.emitted = 0
        self.when = 0.0

    def update(self, text, final=False):
        now = time.monotonic()
        if not final and now - self.when < .05:
            return
        self.when = now
        end = self.emitted
        fence = None
        offset = self.emitted
        for line in text[self.emitted:].splitlines(keepends=True):
            offset += len(line)
            marker = re.match(r" {0,3}(`{3,}|~{3,})", line)
            if marker:
                if fence is None:
                    fence = marker[1]
                elif re.fullmatch(r" {0,3}" + re.escape(fence[0])
                                  + "{" + str(len(fence)) + r",}\s*", line):
                    fence = None
            if fence is None and not line.strip():
                end = offset
        if final:
            end = len(text)
        if end > self.emitted:
            self.console.print(NoInsetMarkdown(text[self.emitted:end], **self.mdargs))
            self.emitted = end
