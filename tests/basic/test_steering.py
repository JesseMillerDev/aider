"""Real prompt-toolkit input handoffs and provider-free steering exchanges."""

import threading
import time
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.shortcuts import PromptSession
from rich.console import Console

from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model
from aider.steering import ParagraphStream, SteeringInput


def until(condition):
    deadline = time.monotonic() + 4
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError("Prompt did not reach the expected state")
        time.sleep(.01)


@pytest.fixture
def terminal():
    with create_pipe_input() as pipe:
        io = InputOutput(pretty=False, fancy_input=False)
        io.prompt_session = PromptSession(input=pipe, output=DummyOutput())
        with SteeringInput(io) as steering:
            io.steering = steering
            try:
                yield io, pipe, steering
            finally:
                io.steering = None


def test_submit_while_busy_keeps_fifo_and_does_not_log_or_consume_on_reader_thread(terminal):
    io, pipe, steering = terminal
    with patch.object(io, "user_input") as logged:
        pipe.send_text("Use Godot\rKeep it 2D\r")
        until(lambda: io.queued_inputs.qsize() == 2)
        steering.pause()
        logged.assert_not_called()
        assert io.take_queued_input() == "Use Godot"
        assert io.take_queued_input() == "Keep it 2D"
        assert io.take_queued_input() is None
        assert logged.call_count == 2


def test_partial_draft_survives_pause_and_resumes_without_becoming_a_message(terminal):
    io, pipe, steering = terminal
    pipe.send_text("Actually use ")
    until(lambda: steering.session.default_buffer.text == "Actually use ")
    steering.pause()
    assert steering.draft == "Actually use " and not steering.pending
    steering.resume()
    until(lambda: steering.session.default_buffer.text == "Actually use ")
    pipe.send_text("Godot\r")
    until(lambda: steering.pending)
    steering.pause()
    assert io.take_queued_input() == "Actually use Godot"


def test_confirmation_has_exclusive_input_and_preserves_the_steering_draft(terminal):
    io, pipe, steering = terminal
    pipe.send_text("Unfinished draft")
    until(lambda: steering.session.default_buffer.text == "Unfinished draft")
    responder = threading.Thread(
        target=lambda: (until(lambda: io.prompt_session.app.is_running), pipe.send_text("n\r")),
        daemon=True,
    )
    responder.start()
    assert io.confirm_ask("Run shell command?", explicit_yes_required=True) is False
    responder.join(timeout=4)
    assert not responder.is_alive()
    until(lambda: steering.session.default_buffer.text == "Unfinished draft")
    assert steering.active and io.take_queued_input() is None


def test_ctrl_c_requests_main_thread_interrupt_and_stops_reader(terminal):
    io, pipe, steering = terminal
    signalled = threading.Event()
    with patch("aider.steering._thread.interrupt_main", side_effect=signalled.set):
        pipe.send_text("\x03")
        assert signalled.wait(4)
        steering.pause()
    assert not steering.active and io.take_queued_input() is None


def test_repeated_handoffs_stop_each_reader_and_ctrl_d_closes_only_busy_input(terminal):
    io, pipe, steering = terminal
    for _ in range(5):
        steering.pause()
        assert not steering.active and steering.thread is None
        steering.resume()
        assert steering.active
    pipe.send_text("\x04")
    until(lambda: steering.disabled)
    steering.pause()
    assert not steering.active and io.take_queued_input() is None


def test_redirected_or_dumb_input_never_starts_a_competing_reader():
    io = InputOutput(pretty=False, fancy_input=False)
    with patch("aider.io.SteeringInput") as controller:
        with io.working_input(None):
            assert io.steering is None
    controller.assert_not_called()


def test_queued_steering_is_sent_after_a_complete_response_with_navigation_feedback(tmp_path):
    io = InputOutput(pretty=False, fancy_input=False)
    coder = Coder.create(Model("gpt-3.5-turbo"), "diff", io=io, use_git=False,
                         map_tokens=0, auto_commits=False)
    sent = []

    def respond(message):
        sent.append(message)
        if len(sent) == 1:
            io.queued_inputs.put("Use Godot instead")
            coder.reflected_message = "Loaded the requested reference."
        if False:
            yield

    with patch.object(coder, "send_message", side_effect=respond):
        coder.run_one("Plan the game", preproc=False)
    assert len(sent) == 2
    assert "Loaded the requested reference." in sent[1]
    assert sent[1].endswith("Use Godot instead")


def test_pending_steering_prevents_a_completed_response_from_applying_stale_edits(tmp_path):
    io = InputOutput(pretty=False, fancy_input=False)
    coder = Coder.create(Model("gpt-3.5-turbo"), "diff", io=io, use_git=False,
                         map_tokens=0, auto_commits=False, stream=False)

    class Controller:
        pending = False

        def pause(self):
            pass

        def resume(self):
            pass

    def respond(*args, **kwargs):
        coder.partial_response_content = "Proposed stale edits"
        coder.partial_response_function_call = {}
        coder.multi_response_content = ""
        io.steering.pending = True
        if False:
            yield

    io.steering = Controller()
    with (patch.object(coder, "send", side_effect=respond),
          patch.object(coder, "apply_updates") as edits,
          patch.object(coder, "run_shell_commands") as shell):
        list(coder.send_message("Original request"))
    edits.assert_not_called()
    shell.assert_not_called()
    assert "not applied" in coder.reflected_message
    io.steering = None


def test_steering_arriving_during_prompt_preparation_skips_the_old_network_request():
    io = InputOutput(pretty=False, fancy_input=False)
    coder = Coder.create(Model("gpt-3.5-turbo"), "diff", io=io, use_git=False,
                         map_tokens=0, auto_commits=False)

    class Controller:
        pending = True

        def pause(self):
            pass

    io.steering = Controller()
    with patch.object(coder, "send") as send:
        list(coder.send_message("Original request"))
    send.assert_not_called()
    assert "before the model request" in coder.reflected_message
    io.steering = None


def test_stream_outputs_complete_markdown_without_cursor_repainting():
    output = StringIO()
    stream = ParagraphStream(Console(file=output, color_system=None), {})
    stream.update("First paragraph.\n\n```python\n\nx = 1\n")
    assert "First paragraph." in output.getvalue() and "x = 1" not in output.getvalue()
    stream.update("First paragraph.\n\n```python\n\nx = 1\n```\n\nLast line", final=True)
    text = output.getvalue()
    assert text.count("First paragraph.") == 1 and "x = 1" in text and "Last line" in text
    assert "\x1b[" not in text
