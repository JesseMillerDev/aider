"""Subscription authentication/transport contracts; no external inference calls."""

import copy
import json
import os
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, patch

import pytest
import requests

from aider.chatgpt_auth import (
    DYNAMIC_CLIENT,
    ISSUER,
    PLAN_SCOPE,
    RESOURCE,
    ChatGPTAuth,
    CredentialStore,
    _listener,
)
from aider.chatgpt_errors import ChatGPTError, response_error
from aider.chatgpt_transport import (
    SUPPORTED_MODEL,
    ChatGPTTransport,
    request_body,
    responses_input,
    sse_events,
)
from aider.exceptions import LiteLLMExceptions
from aider.models import Model

jwt = pytest.importorskip("jwt")
rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")


class Response:
    def __init__(self, body=None, status=200, events=None):
        self.body = body
        self.status_code = status
        self.headers = {"x-request-id": "request-test"}
        self.events = events or []
        self.closed = False

    def json(self):
        return self.body

    def iter_lines(self, **_):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield "data: " + json.dumps(event)
            yield ""

    def close(self):
        self.closed = True


def token_response(**overrides):
    return {
        "access_token": "access-new",
        "refresh_token": "refresh-new",
        "id_token": "identity-new",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": PLAN_SCOPE,
        **overrides,
    }


@pytest.fixture
def store(tmp_path):
    return CredentialStore(tmp_path / "auth")


@pytest.fixture
def auth(store):
    with store.transaction() as state:
        state.update(host_id="urn:uuid:host", active="oaiapp_test")
        state["profiles"]["oaiapp_test"] = {
            "client_id": "oaiapp_test",
            "issuer": ISSUER,
            "subject": "user-test",
            "email": "test@example.com",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "id_token": "id-secret",
            "scopes": [PLAN_SCOPE],
            "expires_at": time.time() + 3600,
        }
    return ChatGPTAuth(store, MagicMock())


def test_store_roundtrip_and_secret_protection(auth, store):
    assert auth.access()[0] == "access-secret"
    if os.name == "nt":
        assert b"access-secret" not in store.path.read_bytes()
        assert store.path.read_bytes().startswith(b"DPAPI1\n")
    else:
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.directory.stat().st_mode & 0o777 == 0o700
    assert list(store.directory.glob(".accounts-*")) == []
    assert "secret" not in json.dumps(auth.status())


def test_no_inference_without_signin_or_plan_permission(store, auth):
    with store.transaction() as state:
        state["profiles"][state["active"]]["scopes"] = []
    with pytest.raises(ChatGPTError, match="disabled"):
        auth.access()
    auth.http.request.assert_not_called()
    with store.transaction() as state:
        state["active"] = None
    with pytest.raises(ChatGPTError, match="Sign in first"):
        auth.access()


def test_refresh_rotates_tokens_and_reuses_client_and_grant(auth, store, monkeypatch):
    with store.transaction() as state:
        state["profiles"][state["active"]]["expires_at"] = 0
    auth.http.request.return_value = Response(token_response(id_token=None, scope=None))
    token, _ = auth.access()
    assert token == "access-new"
    sent = auth.http.request.call_args.kwargs["data"]
    assert sent == {
        "grant_type": "refresh_token",
        "client_id": "oaiapp_test",
        "refresh_token": "refresh-secret",
        "resource": RESOURCE,
    }
    assert "scope" not in sent
    saved = store.read()["profiles"]["oaiapp_test"]
    assert saved["refresh_token"] == "refresh-new"
    assert saved["scopes"] == [PLAN_SCOPE]
    auth.access()
    assert auth.http.request.call_count == 1


@pytest.mark.parametrize(
    "code,cleared", [("invalid_grant", True), ("invalid_client", False)]
)
def test_refresh_failure_only_clears_terminal_credentials(auth, store, code, cleared):
    with store.transaction() as state:
        state["profiles"][state["active"]]["expires_at"] = 0
    auth.http.request.return_value = Response({"error": code}, 400)
    with pytest.raises(ChatGPTError) as error:
        auth.access()
    assert error.value.code == code
    saved = store.read()["profiles"]["oaiapp_test"]
    assert ("access_token" not in saved) == cleared
    assert saved["client_id"] == "oaiapp_test"
    assert store.read()["host_id"] == "urn:uuid:host"


def test_refresh_network_failure_preserves_credentials(auth, store):
    with store.transaction() as state:
        state["profiles"][state["active"]]["expires_at"] = 0
    auth.http.request.side_effect = requests.ConnectionError("unavailable")
    with pytest.raises(ChatGPTError, match="preserved"):
        auth.access()
    assert store.read()["profiles"]["oaiapp_test"]["refresh_token"] == "refresh-secret"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://other.invalid"},
        {"aud": "another-client"},
        {"nonce": "wrong"},
        {"exp": 1},
        {"sub": ""},
        {"sub": None},
    ],
)
def test_identity_claims_are_verified(auth, rsa_key, change):
    claims = {
        "iss": ISSUER,
        "aud": "oaiapp_test",
        "sub": "user-test",
        "nonce": "nonce-test",
        "iat": time.time(),
        "exp": time.time() + 300,
        **change,
    }
    encoded = jwt.encode(claims, rsa_key, algorithm="RS256")
    auth._jwks = MagicMock()
    auth._jwks.get_signing_key_from_jwt.return_value = SimpleNamespace(
        key=rsa_key.public_key()
    )
    with pytest.raises(ChatGPTError, match="identity validation failed"):
        auth._verify_identity(encoded, "oaiapp_test", "nonce-test")


def test_identity_signature_and_valid_identity(auth, rsa_key):
    claims = {
        "iss": ISSUER,
        "aud": "oaiapp_test",
        "sub": "user-test",
        "nonce": "nonce-test",
        "iat": time.time(),
        "exp": time.time() + 300,
    }
    token = jwt.encode(claims, rsa_key, algorithm="RS256")
    auth._jwks = MagicMock()
    auth._jwks.get_signing_key_from_jwt.return_value = SimpleNamespace(
        key=rsa_key.public_key()
    )
    assert (
        auth._verify_identity(token, "oaiapp_test", "nonce-test")["sub"] == "user-test"
    )
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = jwt.encode(claims, other, algorithm="RS256")
    with pytest.raises(ChatGPTError):
        auth._verify_identity(bad, "oaiapp_test", "nonce-test")


def fake_callback(monkeypatch, auth, result=None, identity=None, captured=None):
    def callback(server, state, url, timeout, open_browser):
        params = {key: value[0] for key, value in parse_qs(urlsplit(url).query).items()}
        if captured is not None:
            captured.update(params)
        return {
            "state": state,
            "client_id": "oaiapp_test",
            "code": "one-time-code",
            **(result or {}),
        }

    monkeypatch.setattr("aider.chatgpt_auth._callback", callback)
    monkeypatch.setattr(
        auth,
        "_verify_identity",
        lambda *a: identity
        or {"iss": ISSUER, "sub": "user-test", "email": "test@example.com"},
    )
    auth.http.request.return_value = Response(token_response())


def test_login_uses_pkce_exact_redirect_and_returning_registration(
    auth, store, monkeypatch
):
    captured = {}
    fake_callback(monkeypatch, auth, captured=captured)
    auth.login()
    assert captured["client_id"] == "oaiapp_test"
    assert captured["ext_agent_host_id"] == "urn:uuid:host"
    assert "agent_name_hint" not in captured
    assert captured["id_token_hint"] == "id-secret"
    sent = auth.http.request.call_args.kwargs["data"]
    assert sent["client_id"] == "oaiapp_test"
    assert sent["redirect_uri"] == captured["redirect_uri"]
    assert sent["redirect_uri"].startswith("http://127.0.0.1:")
    assert sent["redirect_uri"].endswith("/auth/callback")
    import base64
    import hashlib

    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(sent["code_verifier"].encode()).digest()
        )
        .decode()
        .rstrip("=")
    )
    assert captured["code_challenge"] == challenge
    assert PLAN_SCOPE in captured["scope"]


def test_new_registration_uses_issued_client_not_dynamic_id(store, monkeypatch):
    auth = ChatGPTAuth(store, MagicMock())
    captured = {}
    fake_callback(monkeypatch, auth, captured=captured)
    auth.login(new_account=True)
    assert captured["client_id"] == DYNAMIC_CLIENT
    assert captured["agent_name_hint"] == "Better Aider"
    assert auth.http.request.call_args.kwargs["data"]["client_id"] == "oaiapp_test"
    assert store.read()["active"] == "oaiapp_test"
    host = store.read()["host_id"]
    auth.login()
    assert store.read()["host_id"] == host


@pytest.mark.parametrize(
    "callback",
    [
        {"state": "wrong"},
        {"client_id": "another-client"},
        {"error": "access_denied"},
    ],
)
def test_callback_failure_never_exchanges_or_changes_active_account(
    auth, store, monkeypatch, callback
):
    original = store.read()
    fake_callback(monkeypatch, auth, result=callback)
    with pytest.raises(ChatGPTError):
        auth.login()
    auth.http.request.assert_not_called()
    assert store.read() == original


def test_wrong_account_does_not_replace_credentials(auth, store, monkeypatch):
    original = store.read()
    fake_callback(monkeypatch, auth, identity={"iss": ISSUER, "sub": "another-user"})
    with pytest.raises(ChatGPTError, match="differs"):
        auth.login()
    assert store.read() == original


def test_no_browser_url_never_contains_identity_token(auth, monkeypatch):
    captured = {}
    fake_callback(monkeypatch, auth, captured=captured)
    auth.login(open_browser=False)
    assert "id_token_hint" not in captured


def test_callback_listener_rejects_wrong_state_without_consuming_attempt():
    with _listener("expected") as server:
        worker = threading.Thread(target=server.handle_request)
        worker.start()
        response = requests.get(
            f"http://127.0.0.1:{server.server_port}/auth/callback?state=wrong&code=secret",
            timeout=5,
        )
        worker.join(timeout=5)
        assert response.status_code == 400
        assert server.result is None
        assert "secret" not in response.text


def test_logout_revokes_then_clears_only_active_tokens(auth, store):
    auth.http.request.side_effect = [
        Response({"revocation_endpoint": ISSUER + "/api/accounts/oauth/revoke"}),
        Response(None),
    ]
    assert auth.logout()
    state = store.read()
    assert state["active"] is None
    assert state["host_id"] == "urn:uuid:host"
    assert state["profiles"]["oaiapp_test"]["subject"] == "user-test"
    assert "access_token" not in state["profiles"]["oaiapp_test"]
    assert auth.http.request.call_args.kwargs["data"]["token"] == "refresh-secret"


def test_logout_network_failure_still_clears_local_tokens(auth, store):
    auth.http.request.side_effect = requests.ConnectionError()
    assert not auth.logout()
    assert "refresh_token" not in store.read()["profiles"]["oaiapp_test"]


def final_event(text="fixed", **changes):
    response = {
        "id": "resp_test",
        "status": "completed",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 60},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ],
        **changes,
    }
    return {"type": "response.completed", "response": response}


@pytest.fixture
def transport(auth):
    auth.http.request.return_value = Response(
        {
            "models": [
                {
                    "slug": SUPPORTED_MODEL,
                    "display_name": "GPT-6.1 Sol",
                    "visibility": "list",
                },
            ]
        }
    )
    http = MagicMock()
    http.post.return_value = Response(
        events=[
            {"type": "response.output_text.delta", "delta": "fixed"},
            final_event(),
        ]
    )
    return ChatGPTTransport(auth=auth, http=http)


MESSAGES = [
    {"role": "system", "content": "Aider's editing instructions"},
    {"role": "user", "content": "Fix this issue"},
]


def test_translation_preserves_prompt_order_and_drops_cache_annotations():
    messages = [
        *MESSAGES,
        {"role": "assistant", "content": "Earlier reply"},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Next",
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,eA=="},
                },
            ],
        },
    ]
    original = copy.deepcopy(messages)
    converted = responses_input(messages)
    assert [item["role"] for item in converted] == [
        "developer",
        "user",
        "assistant",
        "user",
    ]
    assert converted[0]["content"][0]["text"] == messages[0]["content"]
    assert "cache_control" not in json.dumps(converted)
    assert converted[-1]["content"][1]["type"] == "input_image"
    assert messages == original


@pytest.mark.parametrize(
    "extra",
    [
        {"temperature": 0},
        {"max_output_tokens": 100},
        {"extra_body": {"tools": []}},
        {"reasoning_effort": "invalid"},
    ],
)
def test_unsupported_parameters_fail_before_inference(extra):
    with pytest.raises(ChatGPTError):
        request_body(SUPPORTED_MODEL, MESSAGES, extra_params=extra)


def test_function_formats_are_rejected():
    with pytest.raises(ChatGPTError, match="edit-format"):
        request_body(SUPPORTED_MODEL, MESSAGES, functions=[{"name": "edit"}])


def test_nonstream_aider_request_still_streams_http_and_uses_exact_catalog_model(
    transport,
):
    _, completion = transport.send_completion(
        SUPPORTED_MODEL,
        MESSAGES,
        stream=False,
        extra_params={"extra_body": {"reasoning_effort": "high"}},
    )
    assert completion.choices[0].message.content == "fixed"
    assert completion.usage.prompt_tokens == 100
    assert completion.usage.prompt_cache_hit_tokens == 60
    assert completion.usage.completion_tokens == 20
    assert completion.total_cost_usd is None
    args, kwargs = transport.http.post.call_args
    assert args == (RESOURCE + "/responses",)
    assert kwargs["stream"] is True
    assert kwargs["json"] == {
        "model": SUPPORTED_MODEL,
        "input": responses_input(MESSAGES),
        "store": False,
        "stream": True,
        "reasoning": {"effort": "high"},
    }
    assert kwargs["headers"]["Authorization"] == "Bearer access-secret"
    assert not kwargs["allow_redirects"]
    transport.auth.http.request.assert_called_once()


def test_stream_object_retains_final_usage_for_coder(transport):
    _, completion = transport.send_completion(SUPPORTED_MODEL, MESSAGES)
    assert completion.usage is None
    assert "".join(chunk.choices[0].delta.content for chunk in completion) == "fixed"
    assert completion.usage.prompt_cache_hit_tokens == 60
    assert completion.response.closed


def compact_events(
    text="fixed", *, sealed_text=None, item_text=None, item_status="completed"
):
    key = {"output_index": 1, "item_id": "msg_test", "content_index": 0}
    return [
        {"type": "response.output_text.delta", **key, "delta": text},
        {
            "type": "response.output_text.done",
            **key,
            "text": text if sealed_text is None else sealed_text,
        },
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": item_status,
                "content": [
                    {
                        "type": "output_text",
                        "text": text if item_text is None else item_text,
                    }
                ],
            },
        },
        final_event(""),
    ]


def test_compact_completion_verifies_finished_text_and_message_items(transport):
    events = compact_events("fix \U0001f9ea")
    events[-1]["response"]["output"] = []
    transport.http.post.return_value = Response(events=events)
    _, response = transport.send_completion(SUPPORTED_MODEL, MESSAGES, stream=False)
    assert response.choices[0].message.content == "fix \U0001f9ea"
    assert response.usage.prompt_tokens == 100


@pytest.mark.parametrize(
    "options",
    [
        {"sealed_text": "different"},
        {"item_text": "different"},
        {"item_status": "in_progress"},
    ],
)
def test_compact_completion_rejects_conflicting_or_unfinished_items(transport, options):
    events = compact_events(**options)
    events[-1]["response"]["output"] = []
    transport.http.post.return_value = Response(events=events)
    with pytest.raises(ChatGPTError):
        transport.send_completion(SUPPORTED_MODEL, MESSAGES, stream=False)


@pytest.mark.parametrize("missing", [1, 2])
def test_compact_completion_requires_both_text_and_item_seals(transport, missing):
    events = compact_events()
    events[-1]["response"]["output"] = []
    del events[missing]
    transport.http.post.return_value = Response(events=events)
    with pytest.raises(ChatGPTError):
        transport.send_completion(SUPPORTED_MODEL, MESSAGES, stream=False)


def test_compact_completion_preserves_multiple_message_and_content_order(transport):
    first, second = compact_events("first"), compact_events("second")
    for event in second[:-1]:
        event["output_index"] = 3
        if "item_id" in event:
            event["item_id"] = "msg_second"
        if "item" in event:
            event["item"]["id"] = "msg_second"
    events = first[:-1] + second
    events[-1]["response"]["output"] = []
    transport.http.post.return_value = Response(events=events)
    _, response = transport.send_completion(SUPPORTED_MODEL, MESSAGES, stream=False)
    assert response.choices[0].message.content == "firstsecond"


def test_compact_completion_rejects_unsealed_extra_streamed_text(transport):
    events = compact_events()
    events.insert(0, {"type": "response.output_text.delta", "delta": "unsealed"})
    events[-1]["response"]["output"] = []
    transport.http.post.return_value = Response(events=events)
    with pytest.raises(ChatGPTError):
        transport.send_completion(SUPPORTED_MODEL, MESSAGES, stream=False)


@pytest.mark.parametrize(
    "events",
    [
        [{"type": "response.output_text.delta", "delta": "partial"}],
        [
            {"type": "response.output_text.delta", "delta": "partial"},
            requests.ConnectionError(),
        ],
        [
            {"type": "response.output_text.delta", "delta": "partial"},
            final_event("different"),
        ],
        [final_event(usage=None)],
        [{"type": "response.incomplete", "response": {"status": "incomplete"}}],
    ],
)
def test_incomplete_or_inconsistent_streams_are_never_accepted(transport, events):
    transport.http.post.return_value = Response(events=events)
    with pytest.raises(ChatGPTError):
        transport.send_completion(SUPPORTED_MODEL, MESSAGES, stream=False)
    assert transport.http.post.return_value.closed


def test_quota_failure_after_stream_start_stops_without_retry_or_billing_fallback(
    transport,
):
    transport.http.post.return_value = Response(
        events=[
            {"type": "response.output_text.delta", "delta": "partial"},
            {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "subscription_sharing_usage_limit_exceeded",
                        "message": "Quota exceeded",
                    }
                },
            },
        ]
    )
    with pytest.raises(ChatGPTError) as error:
        transport.send_completion(SUPPORTED_MODEL, MESSAGES, stream=False)
    assert error.value.code == "subscription_sharing_usage_limit_exceeded"
    assert "settings/usage" in str(error.value)
    assert not LiteLLMExceptions().get_ex_info(error.value).retry
    assert transport.http.post.call_count == 1


def test_unavailable_model_does_not_substitute_or_make_inference_call(transport):
    transport.auth.http.request.return_value = Response({"models": []})
    with pytest.raises(ChatGPTError, match="unavailable"):
        transport.send_completion(SUPPORTED_MODEL, MESSAGES)
    transport.http.post.assert_not_called()


def test_admission_error_keeps_status_and_request_id_without_leaking_token(transport):
    transport.http.post.return_value = Response({"detail": "Denied access-secret"}, 403)
    with pytest.raises(ChatGPTError) as error:
        transport.send_completion(SUPPORTED_MODEL, MESSAGES)
    assert error.value.status == 403
    assert error.value.request_id == "request-test"
    assert "access-secret" not in str(error.value)
    assert "access-secret" not in json.dumps(error.value.details)


def test_sse_multiline_comments_and_malformed_frames():
    response = Response()
    response.iter_lines = lambda **_: iter(
        [":ping", 'data: {"type":', 'data: "event"}', ""]
    )
    assert list(sse_events(response)) == [{"type": "event"}]
    response.iter_lines = lambda **_: iter(["data: not-json", ""])
    with pytest.raises(ChatGPTError, match="malformed"):
        list(sse_events(response))


def test_error_redaction_handles_oauth_and_nonstandard_shapes():
    error = response_error(
        {"error": "invalid_grant", "refresh_token": "refresh-secret"}, 400
    )
    assert error.code == "invalid_grant"
    assert "refresh-secret" not in json.dumps(error.details)
    assert response_error(["unexpected"], 503).status == 503


def test_chatgpt_model_uses_adapter_without_litellm_or_api_credentials(transport):
    with (
        patch("aider.chatgpt_auth.ChatGPTAuth", return_value=transport.auth),
        patch("aider.models.litellm.completion") as api,
    ):
        model = Model("chatgpt/gpt-6.1-sol")
        model._chatgpt_transport = transport
        _, completion = model.send_completion(MESSAGES, None, False, temperature=0)
        assert completion.choices[0].message.content == "fixed"
        assert model.weak_model is model
        assert model.editor_model is model
        assert model.edit_format == "diff"
        assert not model.missing_keys
        assert model.info["input_cost_per_token"] is None
        assert model.token_count("hello world") > 0
        api.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
def test_real_coder_applies_mocked_subscription_patch_and_records_usage(
    tmp_path, monkeypatch, transport, stream
):
    from aider.coders import Coder
    from aider.io import InputOutput

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "hello.py"
    target.write_text("value = 1\n", encoding="utf-8")
    patch_text = "hello.py\n```python\n<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n>>>>>>> REPLACE\n```\n"
    transport.http.post.return_value = Response(
        events=[
            {"type": "response.output_text.delta", "delta": patch_text},
            final_event(patch_text),
        ]
    )
    with (
        patch("aider.chatgpt_auth.ChatGPTAuth", return_value=transport.auth),
        patch("aider.models.litellm.completion") as api,
    ):
        model = Model("chatgpt/gpt-6.1-sol")
        model._chatgpt_transport = transport
        coder = Coder.create(
            model,
            "diff",
            io=InputOutput(yes=True, pretty=False),
            use_git=False,
            fnames=[str(target)],
            stream=stream,
            auto_lint=False,
            auto_test=False,
            map_tokens=0,
            suggest_shell_commands=False,
        )
        coder.event = MagicMock()
        coder.run(with_message="Change value to 2")
        assert target.read_text() == "value = 2\n"
        assert coder.total_tokens_sent == 100
        assert coder.total_tokens_received == 20
        assert coder.total_cost_usd is None
        assert "Using ChatGPT plan" in coder.usage_report
        assert "$" not in coder.usage_report
        send_events = [
            call.kwargs
            for call in coder.event.call_args_list
            if call.args == ("message_send",)
        ]
        assert send_events[-1]["cost"] is None
        assert send_events[-1]["total_cost"] is None
        with patch.object(coder.io, "tool_output") as output:
            coder.commands.cmd_tokens("")
            token_display = "\n".join(
                str(call.args[0]) for call in output.call_args_list if call.args
            )
            assert "Using ChatGPT plan" in token_display
            assert "$" not in token_display
        coder.add_cache_headers = True
        coder.num_cache_warming_pings = 10
        coder.warm_cache(MagicMock())
        api.assert_not_called()


def test_failed_subscription_stream_cannot_apply_partial_edit(
    tmp_path, monkeypatch, transport
):
    from aider.coders import Coder
    from aider.io import InputOutput

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "hello.py"
    target.write_text("value = 1\n", encoding="utf-8")
    patch_text = "hello.py\n```python\n<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n>>>>>>> REPLACE\n```\n"
    transport.http.post.return_value = Response(
        events=[
            {"type": "response.output_text.delta", "delta": patch_text},
            {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "subscription_sharing_usage_limit_exceeded",
                        "message": "Quota",
                    }
                },
            },
        ]
    )
    with patch("aider.chatgpt_auth.ChatGPTAuth", return_value=transport.auth):
        model = Model("chatgpt/gpt-6.1-sol")
        model._chatgpt_transport = transport
        coder = Coder.create(
            model,
            "diff",
            io=InputOutput(yes=True, pretty=False),
            use_git=False,
            fnames=[str(target)],
            stream=True,
            auto_lint=False,
            auto_test=False,
            map_tokens=0,
        )
        with patch.object(coder, "apply_updates") as apply:
            coder.run(with_message="Change value to 2")
            apply.assert_not_called()
        assert target.read_text() == "value = 1\n"
        assert transport.http.post.call_count == 1
