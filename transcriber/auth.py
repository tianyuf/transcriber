"""Signing in to OpenRouter without ever handling the key by hand.

`transcriber auth login` runs OpenRouter's OAuth PKCE flow:

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

SERVICE = "transcriber"
ACCOUNT = "openrouter"
CONFIG_DIR = Path(os.environ.get("TRANSCRIBER_CONFIG_DIR") or Path.home() / ".config" / "transcriber")
FALLBACK_FILE = CONFIG_DIR / "credentials.json"
LOGIN_TIMEOUT = 300  # seconds to wait for the browser
# Set by the command line for runs that need a key: when none is stored and someone is at the
# terminal, sign in there and then instead of stopping. Library callers and the app leave it off.
AUTO_LOGIN = False


def interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def can_auto_login() -> bool:
    return AUTO_LOGIN and interactive() and not os.environ.get("TRANSCRIBER_NO_AUTO_LOGIN")


# ---------------------------------------------------------------- storage

def _keyring():
    if os.environ.get("TRANSCRIBER_NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends import fail

        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:  # noqa: BLE001 - no usable keychain on this machine
        return None


NATIVE_LABELS = {"openai": "OpenAI", "grok": "xAI Grok", "gemini": "Google Gemini", "anthropic": "Anthropic"}


def _account(provider: str) -> str:
    return ACCOUNT if provider == "openrouter" else provider


def _field(provider: str) -> str:
    return f"{provider}_api_key"


def _read_fallback() -> dict:
    try:
        data = json.loads(FALLBACK_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_fallback(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(FALLBACK_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    if hasattr(os, "fchmod"):
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def stored_key(provider: str = "openrouter") -> tuple[str | None, str]:
    """(key, where it came from), with environment variables taking precedence."""
    if provider == "openrouter":
        env_names = ("OPENROUTER_API_KEY",)
    else:
        from .providers import PROVIDER_ENV
        env_names = PROVIDER_ENV[provider]
    for env in env_names:
        if os.environ.get(env):
            return os.environ[env], f"the {env} environment variable"
    kr = _keyring()
    if kr:
        try:
            key = kr.get_password(SERVICE, _account(provider))
            if key:
                return key, "the system keychain"
        except Exception:  # noqa: BLE001
            pass
    key = _read_fallback().get(_field(provider))
    if key:
        return key, str(FALLBACK_FILE)
    return None, ""


def save_key(key: str, provider: str = "openrouter") -> str:
    kr = _keyring()
    if kr:
        try:
            kr.set_password(SERVICE, _account(provider), key)
            return "the system keychain"
        except Exception:  # noqa: BLE001 - fall through to the file
            pass
    data = _read_fallback()
    data[_field(provider)] = key
    _write_fallback(data)
    return f"{FALLBACK_FILE} (readable only by you; no system keychain was available)"


def delete_key(provider: str = "openrouter") -> list[str]:
    removed = []
    kr = _keyring()
    if kr:
        try:
            if kr.get_password(SERVICE, _account(provider)):
                kr.delete_password(SERVICE, _account(provider))
                removed.append("the system keychain")
        except Exception:  # noqa: BLE001
            pass
    data = _read_fallback()
    if _field(provider) in data:
        data.pop(_field(provider))
        if data:
            _write_fallback(data)
        else:
            FALLBACK_FILE.unlink(missing_ok=True)
        removed.append(str(FALLBACK_FILE))
    return removed


def login_native(provider: str) -> None:
    """Store a native API key obtained from a hidden terminal prompt."""
    import getpass
    label = NATIVE_LABELS[provider]
    key = getpass.getpass(f"{label} API key (input hidden): ").strip()
    if not key:
        raise SystemExit("No API key entered; nothing was saved")
    where = save_key(key, provider)
    print(f"Saved the {label} API key to {where}. It will be checked when first used.")
    from .providers import PROVIDER_ENV
    if any(os.environ.get(env) for env in PROVIDER_ENV[provider]):
        print("Note: an environment variable for this provider is set and takes precedence.")


def native_status(provider: str) -> int:
    key, where = stored_key(provider)
    label = NATIVE_LABELS[provider]
    if not key:
        from .providers import PROVIDER_ENV
        names = " or ".join(PROVIDER_ENV[provider])
        print(f"No {label} key found. Set {names} or run `transcriber auth login --provider {provider}`.")
        return 1
    print(f"A {label} API key is available from {where}. It has not been validated by a provider request.")
    return 0


def logout_native(provider: str) -> None:
    removed = delete_key(provider)
    label = NATIVE_LABELS[provider]
    if removed:
        print(f"Removed the {label} key from " + " and ".join(removed) + ".")
    else:
        print(f"No stored {label} key found.")
    from .providers import PROVIDER_ENV
    if any(os.environ.get(env) for env in PROVIDER_ENV[provider]):
        print("Note: an environment variable for this provider is still set and takes precedence.")


def require_key() -> str:
    key, _ = stored_key()
    if not key and can_auto_login():
        print("You are not signed in to OpenRouter yet, so let's do that first.", flush=True)
        login()
        key, _ = stored_key()
    if not key:
        sys.exit("Not signed in to OpenRouter. Run `transcriber auth login` first "
                 "(or set OPENROUTER_API_KEY), or use the point-and-click app: `transcriber app`.")
    return key


def require_native_key(provider: str, model: str) -> str:
    key, _ = stored_key(provider)
    if not key and can_auto_login():
        print(f"{model} needs a {NATIVE_LABELS[provider]} API key, and none is saved yet.", flush=True)
        login_native(provider)
        key, _ = stored_key(provider)
    if not key:
        from .providers import PROVIDER_ENV
        names = " or ".join(PROVIDER_ENV[provider])
        raise SystemExit(f"{model} requires {names} in the environment or `transcriber auth login --provider "
                         f"{provider}`. Transcriber never prints the key")
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
    q = {"code_challenge": challenge, "code_challenge_method": "S256", "key_label": "transcriber"}
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
                 "The code is single-use and expires after 10 minutes; run `transcriber auth login` again.")
    key = r.json().get("key")
    if not key:
        sys.exit("OpenRouter's reply contained no key; run `transcriber auth login` again.")
    return key


PAGE = """<!doctype html><meta charset="utf-8"><title>transcriber</title>
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
                title, body = "Signed in", "transcriber has your OpenRouter key. You can close this tab."
            else:
                result["error"] = q.get("error") or ("state mismatch" if "code" in q else "no code in the callback")
                title, body = "Sign-in did not finish", html.escape(result["error"]) + ". Run <code>transcriber auth login</code> again."
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
            sys.exit("Timed out waiting for the browser. Run `transcriber auth login` again "
                     "(or `transcriber auth login --headless` to paste a code instead).")
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
        print("Not signed in. Run `transcriber auth login`.")
        return 1
    info = key_info(key)
    if info is None:
        print(f"A key is stored in {where}, but OpenRouter could not be reached to check it.")
        return 1
    if "_status" in info:
        print(f"The key in {where} was rejected (HTTP {info['_status']}). Run `transcriber auth login` again.")
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
