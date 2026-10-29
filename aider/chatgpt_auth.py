"""Sign in with ChatGPT for a local, public OAuth client. Never uses Codex's credentials."""

import argparse
import base64
import ctypes
import hashlib
import hmac
import json
import math
import os
import secrets
import tempfile
import time
import uuid
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from filelock import FileLock

from aider.chatgpt_errors import USAGE_URL, ChatGPTError, response_error

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
DYNAMIC_CLIENT = "dynamic_agent_client"
APP_NAME = "Better Aider"
TOKEN_KEYS = ("access_token", "refresh_token", "id_token", "expires_at", "scopes")
TERMINAL_REFRESH_ERRORS = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}


def _dpapi(data, decrypt=False):
    """Use Windows' current-user credential protection, including on shared machines."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    # CRYPTPROTECT_UI_FORBIDDEN; protection is bound to this Windows user's logon.
    if not function(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
    ):
        raise ChatGPTError(
            "Windows could not protect/unlock the ChatGPT credential store"
        )
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


class CredentialStore:
    def __init__(self, directory=None):
        self.directory = (
            Path(directory) if directory else Path.home() / ".aider" / "chatgpt"
        )
        self.path = self.directory / "accounts.bin"

    def read(self):
        if not self.path.exists():
            return {"version": 1, "host_id": None, "active": None, "profiles": {}}
        try:
            data = self.path.read_bytes()
            if os.name == "nt":
                if not data.startswith(b"DPAPI1\n"):
                    raise ValueError("Unprotected Windows credential file")
                data = _dpapi(data[7:], decrypt=True)
            state = json.loads(data)
            if state["version"] != 1 or not isinstance(state["profiles"], dict):
                raise ValueError("Unsupported credential format")
            return state
        except (ValueError, KeyError, OSError) as exc:
            raise ChatGPTError(
                "Cannot read the protected ChatGPT credential store"
            ) from exc

    def _write(self, state):
        data = json.dumps(state).encode("utf-8")
        if os.name == "nt":
            data = b"DPAPI1\n" + _dpapi(data)
        descriptor, name = tempfile.mkstemp(dir=self.directory, prefix=".accounts-")
        try:
            if os.name != "nt":
                os.chmod(name, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    @contextmanager
    def transaction(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.directory, 0o700)
        with FileLock(str(self.directory / "accounts.lock"), timeout=30, mode=0o600):
            state = self.read()
            try:
                yield state
            finally:
                # Terminal refresh errors must persist token removal even while propagating.
                self._write(state)


def _callback(server, state, authorize_url, timeout, open_browser):
    if open_browser:
        print("Continue with ChatGPT in your browser. Waiting for sign-in...")
        if not webbrowser.open(authorize_url):
            raise ChatGPTError(
                "Could not open your browser; retry login with --no-browser"
            )
    else:
        print(
            "Continue with ChatGPT by opening this temporary sign-in URL:\n"
            + authorize_url
        )
    deadline = time.monotonic() + timeout
    while not server.result and time.monotonic() < deadline:
        server.timeout = min(1.0, max(0.01, deadline - time.monotonic()))
        server.handle_request()
    if not server.result:
        raise ChatGPTError("ChatGPT sign-in timed out; no account was changed")
    return server.result


def _listener(expected_state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Callback query strings contain single-use credentials.

        def do_GET(self):
            url = urlsplit(self.path)
            params = parse_qs(url.query, keep_blank_values=True)
            valid = (
                url.path == "/auth/callback"
                and all(len(value) == 1 for value in params.values())
                and hmac.compare_digest(params.get("state", [""])[0], expected_state)
            )
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(
                b"Return to Better Aider." if valid else b"Invalid sign-in callback."
            )
            if valid:
                self.server.result = {key: value[0] for key, value in params.items()}

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.result = None
    return server


class ChatGPTAuth:
    def __init__(self, store=None, http=None):
        self.store = store or CredentialStore()
        self.http = http or requests.Session()
        self._jwks = None

    def _json(self, method, url, *, secret_values=(), **kwargs):
        try:
            response = self.http.request(
                method, url, timeout=20, allow_redirects=False, **kwargs
            )
        except requests.RequestException as exc:
            raise ChatGPTError(
                "ChatGPT connection unavailable; saved credentials were preserved"
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {"detail": "The server returned a non-JSON response"}
        if not 200 <= response.status_code < 300:
            raise response_error(
                body,
                response.status_code,
                response.headers.get("x-request-id"),
                secret_values,
            )
        return body

    def _verify_identity(self, token, client_id, nonce=None):
        try:
            import jwt
        except ImportError as exc:
            raise ChatGPTError(
                "Install ChatGPT support: uv pip install -r requirements/requirements-chatgpt.txt"
            ) from exc
        try:
            if self._jwks is None:
                self._jwks = jwt.PyJWKClient(
                    ISSUER + "/.well-known/jwks.json", timeout=20
                )
            key = self._jwks.get_signing_key_from_jwt(token).key
            identity = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=ISSUER,
                audience=client_id,
                leeway=5,
                options={"require": ["sub", "exp", "iat"]},
            )
            if not isinstance(identity["sub"], str) or not identity["sub"]:
                raise ValueError("Missing subject")
            if nonce is not None and not hmac.compare_digest(
                identity.get("nonce", ""), nonce
            ):
                raise ValueError("Invalid nonce")
            return identity
        except Exception:
            # JWT exceptions can contain parts of the token; keep them out of user-facing logs.
            raise ChatGPTError(
                "ChatGPT identity validation failed; the active account was not changed"
            ) from None

    def _token_set(self, body, profile, nonce=None, refreshing=False):
        if not isinstance(body.get("access_token"), str) or not body["access_token"]:
            raise ChatGPTError("ChatGPT returned no access token")
        expires = body.get("expires_in")
        if (
            type(expires) not in (int, float)
            or not math.isfinite(expires)
            or expires <= 0
        ):
            raise ChatGPTError("ChatGPT returned an invalid token lifetime")
        if body.get("token_type", "").lower() != "bearer":
            raise ChatGPTError("ChatGPT returned an unsupported token type")
        updated = dict(profile)
        if body.get("id_token"):
            identity = self._verify_identity(
                body["id_token"], profile["client_id"], nonce
            )
            if profile.get("subject") and identity["sub"] != profile["subject"]:
                raise ChatGPTError(
                    "The signed-in ChatGPT account differs from the selected registration"
                )
            updated.update(
                issuer=identity["iss"],
                subject=identity["sub"],
                email=identity.get("email"),
                id_token=body["id_token"],
            )
        elif not refreshing:
            raise ChatGPTError("ChatGPT returned no identity token")
        scopes = body.get("scope")
        updated.update(
            access_token=body["access_token"],
            expires_at=time.time() + expires,
            scopes=(
                scopes.split()
                if isinstance(scopes, str)
                else (profile.get("scopes", []) if refreshing else [])
            ),
        )
        if body.get("refresh_token"):
            updated["refresh_token"] = body["refresh_token"]
        return updated

    def login(self, profile_id=None, new_account=False, open_browser=True, timeout=180):
        with self.store.transaction() as state:
            if not state["host_id"]:
                state["host_id"] = "urn:uuid:" + str(uuid.uuid4())
            selected = None if new_account else (profile_id or state["active"])
            if selected and selected not in state["profiles"]:
                raise ChatGPTError("Unknown ChatGPT registration")
            profile = dict(state["profiles"].get(selected, {}))
            host_id = state["host_id"]
        client_id = profile.get("client_id", DYNAMIC_CLIENT)
        pending_state, nonce, verifier = [secrets.token_urlsafe(32) for _ in range(3)]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        with _listener(pending_state) as server:
            redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
            params = dict(
                client_id=client_id,
                ext_agent_host_id=host_id,
                response_type="code",
                redirect_uri=redirect_uri,
                scope=SCOPES,
                resource=RESOURCE,
                state=pending_state,
                nonce=nonce,
                code_challenge_method="S256",
                code_challenge=challenge,
            )
            if client_id == DYNAMIC_CLIENT:
                params["agent_name_hint"] = APP_NAME
            elif open_browser and profile.get("id_token"):
                # Never print or log a URL containing the retained identity token.
                params["id_token_hint"] = profile["id_token"]
            callback = _callback(
                server,
                pending_state,
                AUTHORIZE_URL + "?" + urlencode(params),
                timeout,
                open_browser,
            )
        if not hmac.compare_digest(callback.get("state", ""), pending_state):
            raise ChatGPTError("Invalid ChatGPT callback state")
        if callback.get("error"):
            raise ChatGPTError(
                "ChatGPT sign-in was declined or could not complete",
                code=callback["error"],
            )
        issued = callback.get("client_id", client_id)
        if (
            not issued
            or issued == DYNAMIC_CLIENT
            or (client_id != DYNAMIC_CLIENT and issued != client_id)
        ):
            raise ChatGPTError("ChatGPT registration returned an invalid client ID")
        if not callback.get("code"):
            raise ChatGPTError("ChatGPT returned no authorization code")
        # Retain the issued registration even if code exchange fails. Do not make it active yet.
        with self.store.transaction() as state:
            profile = dict(state["profiles"].get(issued, profile))
            profile["client_id"] = issued
            state["profiles"][issued] = profile
        body = self._json(
            "POST",
            TOKEN_URL,
            data=dict(
                grant_type="authorization_code",
                client_id=issued,
                code=callback["code"],
                code_verifier=verifier,
                redirect_uri=redirect_uri,
                resource=RESOURCE,
            ),
            secret_values=(callback["code"], verifier),
        )
        updated = self._token_set(body, profile, nonce)
        with self.store.transaction() as state:
            state["profiles"][issued] = updated
            state["active"] = issued
        return self.status()

    def status(self):
        state = self.store.read()
        return {
            "active": state["active"],
            "profiles": [
                {
                    "client_id": key,
                    "email": value.get("email"),
                    "active": key == state["active"],
                    "connected": bool(value.get("access_token")),
                    "plan_enabled": bool(value.get("access_token"))
                    and PLAN_SCOPE in value.get("scopes", []),
                }
                for key, value in state["profiles"].items()
            ],
        }

    def select(self, profile_id):
        with self.store.transaction() as state:
            if profile_id not in state["profiles"]:
                raise ChatGPTError("Unknown ChatGPT registration")
            state["active"] = profile_id

    def access(self):
        # Hold the cross-process lock through refresh and its atomic save: refresh tokens rotate.
        with self.store.transaction() as state:
            profile = state["profiles"].get(state["active"], {})
            if not profile.get("access_token"):
                raise ChatGPTError("Sign in first: python -m aider.chatgpt_auth login")
            if PLAN_SCOPE not in profile.get("scopes", []):
                raise ChatGPTError(
                    "ChatGPT plan usage is disabled; enable it with python -m aider.chatgpt_auth login"
                )
            if profile.get("expires_at", 0) <= time.time() + 60:
                if not profile.get("refresh_token"):
                    raise ChatGPTError(
                        "ChatGPT sign-in expired; run python -m aider.chatgpt_auth login"
                    )
                try:
                    body = self._json(
                        "POST",
                        TOKEN_URL,
                        data=dict(
                            grant_type="refresh_token",
                            client_id=profile["client_id"],
                            refresh_token=profile["refresh_token"],
                            resource=RESOURCE,
                        ),
                        secret_values=tuple(
                            profile.get(key)
                            for key in TOKEN_KEYS
                            if key.endswith("token")
                        ),
                    )
                    updated = self._token_set(body, profile, refreshing=True)
                except ChatGPTError as exc:
                    if exc.code in TERMINAL_REFRESH_ERRORS:
                        for key in TOKEN_KEYS:
                            profile.pop(key, None)
                    raise
                state["profiles"][state["active"]] = profile = updated
                if PLAN_SCOPE not in profile.get("scopes", []):
                    raise ChatGPTError(
                        "ChatGPT plan permission is no longer enabled; sign in again"
                    )
            identity = (profile["client_id"], profile["subject"], profile["expires_at"])
            return profile["access_token"], identity

    def models(self):
        token, _ = self.access()
        body = self._json(
            "GET",
            RESOURCE + "/models",
            headers={"Authorization": f"Bearer {token}"},
            secret_values=(token,),
        )
        catalog = body.get("models")
        if not isinstance(catalog, list):
            raise ChatGPTError("ChatGPT returned an invalid model catalog")
        return [
            dict(slug=row["slug"], display_name=row.get("display_name", row["slug"]))
            for row in catalog
            if row.get("visibility") == "list" and isinstance(row.get("slug"), str)
        ]

    def logout(self):
        confirmed = False
        with self.store.transaction() as state:
            profile = state["profiles"].get(state["active"], {})
            try:
                if profile.get("refresh_token"):
                    discovery = self._json(
                        "GET", ISSUER + "/.well-known/openid-configuration"
                    )
                    endpoint = discovery.get("revocation_endpoint", "")
                    if not endpoint.startswith(ISSUER + "/"):
                        raise ChatGPTError("Invalid ChatGPT revocation endpoint")
                    self._json(
                        "POST",
                        endpoint,
                        data=dict(
                            token=profile["refresh_token"],
                            token_type_hint="refresh_token",
                            client_id=profile["client_id"],
                        ),
                        secret_values=(profile["refresh_token"],),
                    )
                    confirmed = True
                else:
                    confirmed = True
            except ChatGPTError:
                pass
            for key in TOKEN_KEYS:
                profile.pop(key, None)
            state["active"] = None
        return confirmed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Continue with ChatGPT in your browser")
    login.add_argument("--new-account", action="store_true")
    login.add_argument("--profile")
    login.add_argument("--no-browser", action="store_true")
    login.add_argument("--timeout", type=int, default=180)
    commands.add_parser("status")
    commands.add_parser("models")
    commands.add_parser("logout")
    use = commands.add_parser("use")
    use.add_argument("profile")
    args = parser.parse_args(argv)
    auth = ChatGPTAuth()
    try:
        if args.command == "login":
            result = auth.login(
                args.profile, args.new_account, not args.no_browser, args.timeout
            )
            print(json.dumps(result, indent=2))
            print(f"Manage ChatGPT plan usage: {USAGE_URL}")
        elif args.command == "models":
            print(json.dumps(auth.models(), indent=2))
        elif args.command == "status":
            print(json.dumps(auth.status(), indent=2))
        elif args.command == "use":
            auth.select(args.profile)
            print("Selected ChatGPT registration.")
        elif args.command == "logout":
            confirmed = auth.logout()
            print(
                "Signed out locally."
                if confirmed
                else f"Signed out locally; remote revocation was not confirmed. Disconnect the app at {USAGE_URL}"
            )
    except ChatGPTError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
