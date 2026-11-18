"""Signing in to OpenRouter without ever handling the key by hand.

`archivist-transcribe auth login` runs OpenRouter's OAuth PKCE flow:

1. make a random code verifier and its SHA-256 challenge, and a random state;
2. start a one-shot web server on a free localhost port and open
   https://openrouter.ai/auth?callback_url=http://localhost:<port>/callback&code_challenge=...
3. the researcher signs in (or signs up) and approves in the browser; OpenRouter
   redirects back with a one-time code (valid 10 minutes);
4. exchange code + verifier at POST /api/v1/auth/keys for a personal API key;
5. store the key in the operating system's keychain (macOS Keychain, Windows Credential
   Manager, Secret Service on Linux), or, where no keychain exists, in a file only the
   user can read.

The key is never printed. Where the key comes from, in order: the OPENROUTER_API_KEY
environment variable, the keychain, the fallback file.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import stat
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .openrouter import API, BASE_URL, money

SERVICE = "archivist-transcribe"
ACCOUNT = "openrouter"
CONFIG_DIR = Path(os.environ.get("ARCHIVIST_CONFIG_DIR") or Path.home() / ".config" / "archivist-transcribe")
FALLBACK_FILE = CONFIG_DIR / "credentials.json"
LOGIN_TIMEOUT = 300  # seconds to wait for the browser


# ---------------------------------------------------------------- storage

def _keyring():
    if os.environ.get("ARCHIVIST_NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends import fail

        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:  # noqa: BLE001 - no usable keychain on this machine
        return None


def stored_key() -> tuple[str | None, str]:
    """(key, where it came from)."""
    env = os.environ.get("OPENROUTER_API_KEY")
    if env:
        return env, "the OPENROUTER_API_KEY environment variable"
    kr = _keyring()
    if kr:
        try:
            k = kr.get_password(SERVICE, ACCOUNT)
            if k:
                return k, "the system keychain"
        except Exception:  # noqa: BLE001
            pass
    if FALLBACK_FILE.exists():
        try:
            k = json.loads(FALLBACK_FILE.read_text())["openrouter_api_key"]
            if k:
                return k, str(FALLBACK_FILE)
        except Exception:  # noqa: BLE001
            pass
    return None, ""


def save_key(key: str) -> str:
    kr = _keyring()
    if kr:
        try:
            kr.set_password(SERVICE, ACCOUNT, key)
            return "the system keychain"
        except Exception:  # noqa: BLE001 - fall through to the file
            pass
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(FALLBACK_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        json.dump({"openrouter_api_key": key}, f)
    return f"{FALLBACK_FILE} (readable only by you; no system keychain was available)"


def delete_key() -> list[str]:
    removed = []
    kr = _keyring()
    if kr:
        try:
            if kr.get_password(SERVICE, ACCOUNT):
                kr.delete_password(SERVICE, ACCOUNT)
                removed.append("the system keychain")
        except Exception:  # noqa: BLE001
            pass
    if FALLBACK_FILE.exists():
        FALLBACK_FILE.unlink()
        removed.append(str(FALLBACK_FILE))
    return removed


def require_key() -> str:
    key, _ = stored_key()
    if not key:
        sys.exit("Not signed in to OpenRouter. Run `archivist-transcribe auth login` first "
                 "(or set OPENROUTER_API_KEY).")
    return key


# ---------------------------------------------------------------- PKCE

def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge), both base64url without padding."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(challenge: str, callback: str | None, state: str | None, limit: str | None,
             expiration: str | None) -> str:
    q = {"code_challenge": challenge, "code_challenge_method": "S256", "key_label": "archivist-transcribe"}
    if callback:
        q["callback_url"] = callback
    if state:
        q["state"] = state
    if limit:
        q["limit"] = limit
    if expiration:
        q["expiration"] = expiration
    return f"{BASE_URL}/auth?{urlencode(q)}"


def exchange(code: str, verifier: str) -> str:
    r = httpx.post(f"{API}/auth/keys", json={"code": code, "code_verifier": verifier,
                                               "code_challenge_method": "S256"}, timeout=30)
    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:  # noqa: BLE001
            msg = r.text[:200]
        sys.exit(f"OpenRouter did not issue a key (HTTP {r.status_code}): {msg}\n"
                 "The code is single-use and expires after 10 minutes; run `archivist-transcribe auth login` again.")
    key = r.json().get("key")
    if not key:
        sys.exit("OpenRouter's reply contained no key; run `archivist-transcribe auth login` again.")
    return key


PAGE = """<!doctype html><meta charset="utf-8"><title>archivist-transcribe</title>
<body style="font:16px/1.5 system-ui,sans-serif;max-width:32em;margin:15vh auto;padding:0 16px">
<h1 style="font-size:1.3em">{title}</h1><p>{body}</p></body>"""


def _wait_for_code(state: str) -> tuple[HTTPServer, dict]:
    result: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the terminal quiet
            pass

        def do_GET(self):
            u = urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            ok = "code" in q and q.get("state") == state
            if ok:
                result["code"] = q["code"]
                title, body = "Signed in", "archivist-transcribe has your OpenRouter key. You can close this tab."
            else:
                result["error"] = q.get("error") or ("state mismatch" if "code" in q else "no code in the callback")
                title, body = "Sign-in did not finish", html.escape(result["error"]) + ". Run <code>archivist-transcribe auth login</code> again."
            page = PAGE.format(title=title, body=body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            done.set()

    server = HTTPServer(("127.0.0.1", 0), Handler)  # port 0: the OS picks a free port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    result["_done"] = done
    return server, result


def login(headless: bool = False, limit: str | None = None, expiration: str | None = None,
          no_browser: bool = False) -> None:
    verifier, challenge = pkce_pair()
    if headless:
        url = auth_url(challenge, None, None, limit, expiration)
        print("Open this address in a browser, sign in to OpenRouter and approve:\n\n  " + url + "\n")
        code = input("Then paste the code OpenRouter shows you here: ").strip()
    else:
        state = secrets.token_urlsafe(16)
        server, result = _wait_for_code(state)
        port = server.server_address[1]
        url = auth_url(challenge, f"http://localhost:{port}/callback", state, limit, expiration)
        print("Opening OpenRouter in your browser. Sign in (or create an account) and approve access.")
        print("If no browser opens, go to this address yourself:\n\n  " + url + "\n", flush=True)
        if not no_browser:
            webbrowser.open(url)
        print(f"Waiting up to {LOGIN_TIMEOUT // 60} minutes for approval...", flush=True)
        finished = result["_done"].wait(LOGIN_TIMEOUT)
        server.shutdown()
        if not finished:
            sys.exit("Timed out waiting for the browser. Run `archivist-transcribe auth login` again "
                     "(or `archivist-transcribe auth login --headless` to paste a code instead).")
        if "error" in result:
            sys.exit(f"Sign-in failed: {result['error']}")
        code = result["code"]
    key = exchange(code, verifier)
    where = save_key(key)
    info = key_info(key)
    label = info.get("label", "your new key") if info else "your new key"
    print(f"Signed in. Saved {label} to {where}.")
    if info:
        print(describe_limits(info))


# ---------------------------------------------------------------- status

def key_info(key: str) -> dict | None:
    try:
        r = httpx.get(f"{API}/key", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return {"_status": r.status_code}
    return r.json().get("data") or {}


def describe_limits(info: dict) -> str:
    parts = [f"used {money(float(info.get('usage') or 0))} so far"]
    if info.get("limit") is not None:
        parts.append(f"limit {money(float(info['limit']))}, {money(float(info.get('limit_remaining') or 0))} left")
    else:
        parts.append("no spending limit on this key (set one at https://openrouter.ai/settings/keys)")
    if info.get("expires_at"):
        parts.append(f"expires {info['expires_at']}")
    return "Key: " + "; ".join(parts) + "."


def status() -> int:
    key, where = stored_key()
    if not key:
        print("Not signed in. Run `archivist-transcribe auth login`.")
        return 1
    info = key_info(key)
    if info is None:
        print(f"A key is stored in {where}, but OpenRouter could not be reached to check it.")
        return 1
    if "_status" in info:
        print(f"The key in {where} was rejected (HTTP {info['_status']}). Run `archivist-transcribe auth login` again.")
        return 1
    print(f"Signed in with {info.get('label', 'a key')} from {where}.")
    print(describe_limits(info))
    return 0


def logout() -> None:
    removed = delete_key()
    if removed:
        print("Removed the stored key from " + " and ".join(removed) + ".")
    else:
        print("No stored key found.")
    if os.environ.get("OPENROUTER_API_KEY"):
        print("Note: OPENROUTER_API_KEY is still set in your environment.")
    print("The key itself still exists on OpenRouter; delete it at https://openrouter.ai/settings/keys to revoke it.")
