"""
OpenAI OAuth (Codex) authentication via browser-based PKCE flow.
Adapted from gpt-oauth-example.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = f"{ISSUER}/oauth/token"
REDIRECT_PATH = "/auth/callback"
REDIRECT_PORT = 1455
SCOPE = "openid profile email offline_access"
ORIGINATOR = "opencode"

DEFAULT_AUTH_FILE = Path(__file__).resolve().parent / ".auth" / "openai-oauth.json"

ALLOWED_MODELS = {
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
}

MODEL_MAP = {
    "gpt-4o-mini": "gpt-4o-mini",
    "gpt-4o": "gpt-4o",
    "gpt-5.2": "gpt-5.2",
    "gpt-5.2-codex": "gpt-5.2-codex",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.1-codex": "gpt-5.1-codex",
    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
}


@dataclass
class AuthTokens:
    type: str
    access: str
    refresh: str
    expires: int
    accountId: str | None = None
    idToken: str | None = None


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def generate_pkce() -> tuple[str, str]:
    verifier = _base64url(secrets.token_bytes(32))
    challenge = _base64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def generate_state() -> str:
    return _base64url(secrets.token_bytes(16))


def build_authorize_url(redirect_uri: str, challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "state": state,
        "codex_cli_simplified_flow": "true",
        "originator": ORIGINATOR,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    payload = urlencode(form).encode("utf-8")
    req = Request(TOKEN_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Token request failed: {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def exchange_authorization_code(code: str, verifier: str, redirect_uri: str) -> AuthTokens:
    data = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        }
    )
    return AuthTokens(
        type="oauth",
        access=data["access_token"],
        refresh=data["refresh_token"],
        expires=int(time.time() * 1000) + int(data.get("expires_in", 3600)) * 1000,
        idToken=data.get("id_token"),
    )


def refresh_access_token(refresh_token: str) -> AuthTokens:
    data = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }
    )
    return AuthTokens(
        type="oauth",
        access=data["access_token"],
        refresh=data["refresh_token"],
        expires=int(time.time() * 1000) + int(data.get("expires_in", 3600)) * 1000,
        idToken=data.get("id_token"),
    )


def decode_jwt_claims(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    pad = "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        payload = base64.urlsafe_b64decode(parts[1] + pad)
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return None


def extract_account_id(tokens: AuthTokens) -> str | None:
    for claims in (decode_jwt_claims(tokens.idToken), decode_jwt_claims(tokens.access)):
        if not claims:
            continue
        if claims.get("chatgpt_account_id"):
            return str(claims["chatgpt_account_id"])
        auth_claim = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claim, dict) and auth_claim.get("chatgpt_account_id"):
            return str(auth_claim["chatgpt_account_id"])
        orgs = claims.get("organizations")
        if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict) and orgs[0].get("id"):
            return str(orgs[0]["id"])
    return None


def save_auth(auth: AuthTokens, auth_file: Path = DEFAULT_AUTH_FILE) -> None:
    auth.accountId = auth.accountId or extract_account_id(auth)
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"openai": asdict(auth)}
    auth_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(auth_file, 0o600)


def load_saved_auth(auth_file: Path = DEFAULT_AUTH_FILE) -> AuthTokens | None:
    if not auth_file.exists():
        return None
    data = json.loads(auth_file.read_text(encoding="utf-8"))
    raw = data.get("openai") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    if raw.get("type") != "oauth" or not raw.get("refresh"):
        return None
    return AuthTokens(
        type="oauth",
        access=str(raw.get("access", "")),
        refresh=str(raw["refresh"]),
        expires=int(raw.get("expires", 0)),
        accountId=raw.get("accountId"),
        idToken=raw.get("idToken"),
    )


class _CallbackHandler(BaseHTTPRequestHandler):
    expected_state: str = ""
    result_code: str | None = None
    result_error: str | None = None
    done_event: threading.Event = threading.Event()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        params = parse_qs(parsed.query)
        state = params.get("state", [""])[0]
        code = params.get("code", [""])[0]
        error = params.get("error", [""])[0]

        if error:
            self.__class__.result_error = f"OAuth error: {error}"
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Authorization failed")
            self.__class__.done_event.set()
            return

        if state != self.__class__.expected_state:
            self.__class__.result_error = "State mismatch"
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"State mismatch")
            self.__class__.done_event.set()
            return

        if not code:
            self.__class__.result_error = "Missing authorization code"
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing authorization code")
            self.__class__.done_event.set()
            return

        self.__class__.result_code = code
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Authorization successful!</h1>"
            b"<p>You can close this window and return to the translator.</p></body></html>"
        )
        self.__class__.done_event.set()

    def log_message(self, format: str, *args: object) -> None:
        return  # suppress HTTP server logs


def login_with_browser_oauth(
    open_browser: bool = True,
    auth_file: Path = DEFAULT_AUTH_FILE,
    url_callback: Any = None,
) -> AuthTokens:
    verifier, challenge = generate_pkce()
    state = generate_state()
    redirect_uri = f"http://localhost:{REDIRECT_PORT}{REDIRECT_PATH}"

    _CallbackHandler.expected_state = state
    _CallbackHandler.result_code = None
    _CallbackHandler.result_error = None
    _CallbackHandler.done_event = threading.Event()

    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("0.0.0.0", REDIRECT_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        authorize_url = build_authorize_url(redirect_uri, challenge, state)
        if url_callback:
            url_callback(authorize_url)
        elif open_browser:
            webbrowser.open(authorize_url)

        finished = _CallbackHandler.done_event.wait(timeout=300)
        if not finished:
            raise TimeoutError("OAuth callback timeout (5 minutes)")
        if _CallbackHandler.result_error:
            raise RuntimeError(_CallbackHandler.result_error)
        if not _CallbackHandler.result_code:
            raise RuntimeError("OAuth code missing")

        tokens = exchange_authorization_code(_CallbackHandler.result_code, verifier, redirect_uri)
        tokens.accountId = extract_account_id(tokens)
        save_auth(tokens, auth_file=auth_file)
        return tokens
    finally:
        server.shutdown()
        server.server_close()


def get_reusable_auth(
    force_login: bool = False,
    open_browser: bool = True,
    auth_file: Path = DEFAULT_AUTH_FILE,
    url_callback: Any = None,
) -> AuthTokens:
    if not force_login:
        saved = load_saved_auth(auth_file=auth_file)
        if saved:
            if saved.access and saved.expires > int(time.time() * 1000):
                return saved
            refreshed = refresh_access_token(saved.refresh)
            refreshed.accountId = saved.accountId or extract_account_id(refreshed)
            save_auth(refreshed, auth_file=auth_file)
            return refreshed

    if not open_browser and not url_callback:
        raise RuntimeError("No saved OAuth session. Login is required.")
    return login_with_browser_oauth(open_browser=open_browser, auth_file=auth_file, url_callback=url_callback)


def normalize_model(model: str) -> str:
    mapped = MODEL_MAP.get(model, model)
    if mapped not in ALLOWED_MODELS:
        raise ValueError(f"Unsupported OAuth Codex model: {model}")
    return mapped


def is_authenticated(auth_file: Path = DEFAULT_AUTH_FILE) -> bool:
    saved = load_saved_auth(auth_file=auth_file)
    return saved is not None and bool(saved.refresh)
