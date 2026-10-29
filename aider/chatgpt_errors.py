"""Non-retryable subscription errors, separate from API-key billing errors."""


class ChatGPTError(Exception):
    def __init__(
        self, message, *, code=None, status=None, request_id=None, details=None
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id
        self.details = details


USAGE_URL = "https://chatgpt.com/settings/usage"


def response_error(body, status=None, request_id=None, secrets=()):
    """Preserve diagnostic shape without putting credentials in an exception/log."""

    def redact(value):
        if isinstance(value, dict):
            return {
                key: (
                    "[redacted]"
                    if key.lower()
                    in (
                        "access_token",
                        "refresh_token",
                        "id_token",
                        "authorization",
                        "code_verifier",
                    )
                    else redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[redacted]")
        return value

    body = redact(body)
    if not isinstance(body, dict):
        body = {"detail": "Unexpected server error format", "response": body}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if isinstance(
        error, str
    ):  # OAuth errors use a string rather than the API error object.
        code = error
        message = body.get("error_description", error)
    elif isinstance(error, dict):
        code = error.get("code")
        message = (
            error.get("message")
            or body.get("detail")
            or "Request could not be completed"
        )
    else:
        code, message = None, "Unexpected server error format"
    text = f"ChatGPT plan: {str(message)[:500]}"
    if code:
        text += f" ({code})"
    if status:
        text += f" [HTTP {status}]"
    if request_id:
        text += f" [request {request_id}]"
    if code == "subscription_sharing_usage_limit_exceeded":
        text += f". Manage your allowance at {USAGE_URL}"
    return ChatGPTError(
        text, code=code, status=status, request_id=request_id, details=body
    )
