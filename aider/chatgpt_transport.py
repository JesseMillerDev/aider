"""Translate Aider's messages to the public subscription Responses route."""

import copy
import hashlib
import json
from types import SimpleNamespace

import requests

from aider.chatgpt_auth import RESOURCE, ChatGPTAuth
from aider.chatgpt_errors import ChatGPTError, response_error

SUPPORTED_MODEL = "gpt-6.1-sol"
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def responses_input(messages):
    """Retain role/order/text while removing provider-specific cache annotations."""
    result = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            role = "developer"
        if role not in ("developer", "user", "assistant"):
            raise ChatGPTError(
                "ChatGPT transport supports text editing formats, not tool-result messages"
            )
        source = message.get("content")
        if isinstance(source, str):
            source = [{"type": "text", "text": source}]
        if not isinstance(source, list):
            raise ChatGPTError("Unsupported Aider message content for ChatGPT")
        content = []
        for part in source:
            if part.get("type") in ("text", "input_text", "output_text"):
                text = part.get("text")
                if not isinstance(text, str):
                    raise ChatGPTError("ChatGPT text input must be a string")
                content.append(
                    {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": text,
                    }
                )
            elif part.get("type") == "image_url" and role == "user":
                image = part["image_url"]
                if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                    raise ChatGPTError("Unsupported Aider image input for ChatGPT")
                content.append(
                    {
                        "type": "input_image",
                        "image_url": image["url"],
                        "detail": image.get("detail", "auto"),
                    }
                )
            else:
                raise ChatGPTError("Unsupported Aider content type for ChatGPT")
        result.append({"role": role, "content": content})
    return result


def request_body(model, messages, functions=None, extra_params=None):
    if model != SUPPORTED_MODEL:
        raise ChatGPTError(f"This adapter currently supports chatgpt/{SUPPORTED_MODEL}")
    if functions is not None:
        raise ChatGPTError(
            "Use --edit-format diff or whole with ChatGPT; function edit formats are not supported"
        )
    extra = copy.deepcopy(extra_params or {})
    extra_body = extra.pop("extra_body", {})
    if not isinstance(extra_body, dict):
        raise ChatGPTError("Invalid ChatGPT model extra_body")
    if set(extra) - {"reasoning_effort"} or set(extra_body) - {"reasoning_effort"}:
        raise ChatGPTError(
            "ChatGPT subscription transport accepts only reasoning_effort model overrides"
        )
    effort = extra.get("reasoning_effort", extra_body.get("reasoning_effort", "medium"))
    if effort not in EFFORTS:
        raise ChatGPTError("Unsupported GPT-6.1 Sol reasoning effort")
    return {
        "model": model,
        "input": responses_input(messages),
        "store": False,
        "stream": True,
        "reasoning": {"effort": effort},
    }


def sse_events(response):
    """SSE frames may contain multiple data lines and comment/keepalive lines."""
    data = []
    for line in response.iter_lines(decode_unicode=True):
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if line == "":
            if data:
                yield _event(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield _event(data)


def _event(data):
    try:
        value = json.loads("\n".join(data))
        if not isinstance(value, dict):
            raise ValueError("Non-object event")
        return value
    except ValueError:
        raise ChatGPTError("ChatGPT returned a malformed response stream") from None


def usage_record(response):
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise ChatGPTError("The completed ChatGPT response has no token usage")
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
    for value in (input_tokens, output_tokens, cached, reasoning):
        if type(value) is not int or value < 0:
            raise ChatGPTError("ChatGPT returned invalid token usage")
    if cached > input_tokens or reasoning > output_tokens:
        raise ChatGPTError("ChatGPT returned inconsistent token usage")
    # cached input is included in prompt_tokens; do not use Anthropic's additive counters.
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        prompt_cache_hit_tokens=cached,
        reasoning_tokens=reasoning,
        total_tokens=input_tokens + output_tokens,
    )


def completed_text(response):
    return "".join(
        part.get("text", "")
        for item in response.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


def _text_key(event):
    index, item, content = (
        event.get(name) for name in ("output_index", "item_id", "content_index")
    )
    if (
        type(index) is int
        and index >= 0
        and isinstance(item, str)
        and item
        and type(content) is int
        and content >= 0
    ):
        return index, item, content
    return None


def _compact_completed_text(deltas, text_events, item_events):
    """A compact terminal event needs matching text and completed message seals."""
    error = "ChatGPT compact completion has unverified or conflicting text"
    sealed = {}
    for event in text_events:
        key = _text_key(event)
        if key is None or key in sealed or not isinstance(event.get("text"), str):
            raise ChatGPTError(error)
        sealed[key] = event["text"]
    expected, outputs, indices = set(), [], set()
    for event in item_events:
        item = event.get("item", {})
        if item.get("type") != "message":
            continue
        index, item_id = event.get("output_index"), item.get("id")
        if (
            type(index) is not int
            or index < 0
            or index in indices
            or not isinstance(item_id, str)
            or not item_id
            or item.get("status") != "completed"
            or item.get("role") != "assistant"
            or not isinstance(item.get("content"), list)
        ):
            raise ChatGPTError(error)
        indices.add(index)
        for content_index, part in enumerate(item["content"]):
            if part.get("type") != "output_text":
                continue
            key = index, item_id, content_index
            text = part.get("text")
            if (
                not isinstance(text, str)
                or sealed.get(key) != text
                or deltas.get(key, "") != text
            ):
                raise ChatGPTError(error)
            expected.add(key)
            outputs.append((index, content_index, text))
    if not expected or set(sealed) != expected or not set(deltas).issubset(expected):
        raise ChatGPTError(error)
    return "".join(text for _, _, text in sorted(outputs))


class ChatGPTStream:
    """Aider-compatible streaming object; final usage remains on the iterable."""

    def __init__(self, response, token):
        self.response = response
        self._token = token
        self.usage = None
        self.response_id = None
        self.request_id = response.headers.get("x-request-id")
        self.billing_mode = "chatgpt_subscription"
        self.total_cost_usd = None
        self._iterator = self._chunks()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def close(self):
        self._iterator.close()
        self.response.close()

    def _chunks(self):
        text = ""
        deltas, text_events, item_events = {}, [], []
        completed = False
        try:
            for event in sse_events(self.response):
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    delta = event.get("delta")
                    if not isinstance(delta, str):
                        raise ChatGPTError("Invalid ChatGPT text delta")
                    text += delta
                    key = _text_key(event)
                    deltas[key] = deltas.get(key, "") + delta
                    yield SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content=delta), finish_reason=None
                            )
                        ]
                    )
                elif kind == "response.output_text.done":
                    text_events.append(event)
                elif kind == "response.output_item.done":
                    item_events.append(event)
                elif kind == "response.completed":
                    final = event.get("response", {})
                    if final.get("status") != "completed":
                        raise ChatGPTError("ChatGPT did not complete the response")
                    self.usage = usage_record(final)
                    self.response_id = final.get("id")
                    full_text = completed_text(final)
                    if text and not full_text and final.get("output") == []:
                        full_text = _compact_completed_text(
                            deltas, text_events, item_events
                        )
                    if text and text != full_text:
                        raise ChatGPTError(
                            "ChatGPT streamed text differs from its completed response"
                        )
                    completed = True
                    if not text and full_text:
                        yield SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    delta=SimpleNamespace(content=full_text),
                                    finish_reason=None,
                                )
                            ]
                        )
                    return
                elif kind in ("response.failed", "response.incomplete", "error"):
                    detail = event.get("response", event)
                    raise response_error(
                        detail, request_id=self.request_id, secrets=(self._token,)
                    )
            if not completed:
                raise ChatGPTError(
                    "ChatGPT stream ended before response.completed; no edit was accepted"
                )
        except requests.RequestException:
            raise ChatGPTError(
                "ChatGPT response stream disconnected before completion"
            ) from None
        finally:
            self.response.close()
            self._token = None


class ChatGPTTransport:
    def __init__(self, auth=None, http=None, timeout=600):
        self.auth = auth or ChatGPTAuth()
        self.http = http or requests.Session()
        self.timeout = timeout
        self._catalog_identity = None
        self._slugs = set()

    def send_completion(
        self, model, messages, functions=None, stream=True, extra_params=None
    ):
        body = request_body(model, messages, functions, extra_params)
        token, identity = self.auth.access()
        if identity != self._catalog_identity:
            # Use exactly the same signed-in profile for discovery and inference.
            catalog = self.auth._json(
                "GET",
                RESOURCE + "/models",
                secret_values=(token,),
                headers={"Authorization": f"Bearer {token}"},
            )
            self._slugs = {
                row["slug"]
                for row in catalog.get("models", [])
                if row.get("visibility") == "list" and isinstance(row.get("slug"), str)
            }
            self._catalog_identity = identity
        if model not in self._slugs:
            raise ChatGPTError(
                f"{model} is unavailable to the selected ChatGPT account; run python -m aider.chatgpt_auth models"
            )
        try:
            response = self.http.post(
                RESOURCE + "/responses",
                json=body,
                stream=True,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
                timeout=(20, self.timeout),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise ChatGPTError(
                "ChatGPT connection failed; no API-key billing fallback was attempted"
            ) from None
        if response.status_code != 200:
            try:
                details = response.json()
            except ValueError:
                details = {"detail": "Non-JSON admission error"}
            finally:
                response.close()
            raise response_error(
                details,
                response.status_code,
                response.headers.get("x-request-id"),
                (token,),
            )
        response.encoding = "utf-8"
        completion = ChatGPTStream(response, token)
        key = hashlib.sha1(json.dumps(body, sort_keys=True).encode("utf-8"))
        if stream:
            return key, completion
        text = ""
        try:
            for chunk in completion:
                text += chunk.choices[0].delta.content
        except requests.RequestException:
            raise ChatGPTError(
                "ChatGPT response stream disconnected before completion"
            ) from None
        finally:
            completion.close()
        return key, SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=text, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=completion.usage,
            response_id=completion.response_id,
            billing_mode="chatgpt_subscription",
            total_cost_usd=None,
        )
