"""`transcriber app`: the whole workflow as a point-and-click page in the browser.

A small web server on this computer serves one page that walks through signing in, choosing
a document, describing it, a pilot on a few pages, the full run and the check with a second
model. It calls the same code as the command line and writes the same files, in an `output`
folder next to the document.

Only this computer can reach the server (it listens on 127.0.0.1), requests must name it as
localhost, and every API call must carry the random token from the address the app opens.
Other web pages the user happens to visit therefore cannot drive it. Keys go from the browser
straight to the keychain and are never sent back to the page.
"""
from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, auth, transcribe, validate
from .openrouter import DEFAULT_MODEL, MODEL_ALIASES, MODEL_NOTES, SPEND
from .profiles import notation_for
from .providers import DIRECT_EXAMPLES, default_fallback, resolve_model, split_model
from .sources import Source
from .triage import summarize

PILOT_PAGES = 3
MAX_BODY = 64 * 1024
LOGIN_TTL = 15 * 60  # seconds a started sign-in stays valid
KINDS = {  # what the page offers -> (profile, notation)
    "printed": ("printed", None),
    "handwritten": ("handwritten", None),
    "cjk": ("cjk", None),
    "handwritten-cjk": ("handwritten", "cjk"),
}


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- the operating system

def pick_path(kind: str) -> str | None:
    """Ask for a file or folder with the system's own dialog; None if cancelled."""
    folder = kind == "folder"
    system = platform.system()
    if system == "Darwin":
        what = ('choose folder with prompt "Choose a folder of photos"' if folder else
                'choose file with prompt "Choose a PDF or a photo" of type {"com.adobe.pdf", "public.image"}')
        cmd = ["osascript", "-e", "activate", "-e", f"POSIX path of ({what})"]
    elif system == "Windows":
        dialog = ("$d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = 'Choose a folder of photos'; "
                  "$ok = $d.ShowDialog($w); $p = $d.SelectedPath" if folder else
                  "$d = New-Object System.Windows.Forms.OpenFileDialog; "
                  "$d.Filter = 'PDF or image|*.pdf;*.jpg;*.jpeg;*.png;*.tif;*.tiff;*.webp;*.bmp;*.gif;*.jp2|All files|*.*'; "
                  "$ok = $d.ShowDialog($w); $p = $d.FileName")
        script = ("Add-Type -AssemblyName System.Windows.Forms; [Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                  "$w = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}; "
                  f"{dialog}; if ($ok -eq 'OK') {{ $p }}")
        cmd = ["powershell", "-NoProfile", "-STA", "-Command", script]
    elif shutil.which("zenity"):
        cmd = ["zenity", "--file-selection"] + (["--directory"] if folder else [])
    elif shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory" if folder else "--getopenfilename", str(Path.home())]
    else:
        raise AppError("This computer has no file dialog Transcriber can use; type the location instead.")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AppError(f"The file dialog could not be opened ({e}); type the location instead.") from e
    path = r.stdout.strip()
    return path if r.returncode == 0 and path else None


def open_with_system(path: Path, how: str) -> None:
    """how: 'text' (a text editor), 'reveal' (the file manager), or 'browser'."""
    system = platform.system()
    if how == "browser":
        webbrowser.open(path.resolve().as_uri())
    elif system == "Darwin":
        subprocess.Popen(["open", "-t", str(path)] if how == "text" else ["open", "-R", str(path)])
    elif system == "Windows":
        subprocess.Popen(["notepad", str(path)] if how == "text" else ["explorer", f"/select,{path}"])
    else:
        subprocess.Popen(["xdg-open", str(path if how == "text" else path.parent)])


def clean_path(text: str) -> Path:
    """A location as people paste it: maybe quoted, maybe with Terminal-style escaped spaces."""
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    path = Path(text).expanduser()
    if not path.exists() and "\\ " in text:
        path = Path(text.replace("\\ ", " ")).expanduser()
    if not text or not path.exists():
        raise AppError(f"Nothing found at {text!r}. Check the location, or use the Choose button.")
    return path.resolve()


# ---------------------------------------------------------------- documents and settings

def output_dirs(src: Source) -> tuple[Path, Path]:
    return src.path.parent / "output", src.path.parent / "output-b"


def open_source(path_text: str) -> Source:
    path = clean_path(path_text)
    try:
        return Source(path)
    except SystemExit as e:
        raise AppError(str(e)) from e


def done_pages(out: Path, src: Source, n: int) -> list[int]:
    pdir = transcribe.pages_dir(out, src)
    return [i for i in range(n) if transcribe.page_file(pdir, i).exists()]


def kind_of(run: dict) -> str:
    profile, notation = run.get("profile", "printed"), run.get("notation")
    if profile == "handwritten" and notation == "cjk":
        return "handwritten-cjk"
    return profile if profile in KINDS else "printed"


def make_settings(form: dict) -> transcribe.Settings:
    kind = form.get("kind") or "printed"
    if kind not in KINDS:
        raise AppError(f"unknown kind of document: {kind}")
    profile, notation = KINDS[kind]
    model = resolve_model((form.get("model") or DEFAULT_MODEL).strip())
    try:
        fallback = default_fallback([model])
    except SystemExit as e:
        raise AppError(str(e)) from e
    return transcribe.Settings(
        model=model, fallback_model=fallback, profile=profile, notation=notation,
        language=(form.get("language") or "").strip() or None,
        layout="lines" if form.get("lines") else "paragraphs", diplomatic=bool(form.get("diplomatic")),
        dpi=200 if form.get("faint") else 150, workers=4, retries=5, timeout=180.0,
        deny_data_collection=bool(form.get("private")),
    )


def check_credentials(models: list[str], private: bool) -> None:
    """Fail early, in words that fit the page, rather than inside the run."""
    for model in models:
        provider, _ = split_model(model)
        if provider == "openrouter":
            if not auth.stored_key()[0]:
                raise AppError("Sign in to OpenRouter first (step 1).")
            continue
        if private:
            raise AppError("The privacy option works only through OpenRouter. Choose an OpenRouter model, "
                           "or turn the privacy option off.")
        if not auth.stored_key(provider)[0]:
            raise AppError(f"{model} needs a {auth.NATIVE_LABELS[provider]} API key. "
                           "Add one under “Other providers” in step 1.")


def mismatch(run: dict, s: transcribe.Settings) -> bool:
    """Pages already done with other settings would mix two kinds of transcription."""
    if not run.get("pages"):
        return False
    return (run.get("profile"), run.get("notation"), run.get("language"), run.get("layout")) != \
        (s.profile, s.notation_name, s.language, s.layout)


def check_plan(src: Source) -> dict:
    """What `validate` would do: model B, the pages it would read again, and the settings to use."""
    out, out_b = output_dirs(src)
    run = validate.read_run(out, src)
    n = src.page_count()
    done = done_pages(out, src, n)
    if not run or not done:
        return {"ready": False}
    notation = notation_for(run.get("profile", "printed"), run.get("notation"))
    model_b = validate.DEFAULT_B.get(notation.name, validate.DEFAULT_B_OTHER)
    fallback = default_fallback([model_b])
    first = next(iter(run["pages"].values()), {})
    s = transcribe.Settings(model=model_b, fallback_model=fallback, profile=run.get("profile", "printed"),
                            notation=notation.name, language=run.get("language"),
                            layout=run.get("layout", "paragraphs"), diplomatic=run.get("diplomatic", False),
                            dpi=first.get("dpi", 150), workers=4, retries=5, timeout=180.0, deny_data_collection=False)
    return {"ready": True, "settings": s, "done": done, "out": out, "out_b": out_b}


# ---------------------------------------------------------------- background jobs

class Job:
    """One transcription or check running in the background; the page polls its summary."""

    def __init__(self, kind: str, src: Source, todo: list[int], count_dir: Path):
        self.kind = kind
        self.source = str(src.path)
        self.todo = todo
        self.count_dir = count_dir
        self.lines: deque[str] = deque(maxlen=300)
        self.state = "running"
        self.error: str | None = None
        self.result: dict = {}
        self.stop = threading.Event()
        self.started = time.time()
        self.cost_before = SPEND.cost

    def add(self, line: str) -> None:
        self.lines.append(line)

    def finished_pages(self) -> int:
        n = 0
        for i in self.todo:
            f = transcribe.page_file(self.count_dir, i)
            try:
                if f.stat().st_mtime >= self.started - 1:
                    n += 1
            except OSError:
                pass
        return n

    def summary(self) -> dict:
        return {"kind": self.kind, "source": self.source, "state": self.state, "error": self.error,
                "done": self.finished_pages(), "total": len(self.todo), "lines": list(self.lines)[-12:],
                "stopping": self.stop.is_set() and self.state == "running", "result": self.result,
                "seconds": round(time.time() - self.started)}


class App:
    def __init__(self, port: int):
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.logins: dict[str, tuple[str, float]] = {}  # state -> (PKCE verifier, when started)
        self.job: Job | None = None
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}/#token={self.token}"

    # ------------------------------------------------ sign-in

    def status(self, _: dict) -> dict:
        key, where = auth.stored_key()
        account: dict = {"signed_in": bool(key), "where": where}
        if key:
            info = auth.key_info(key)
            if info is None:
                account["warning"] = "OpenRouter could not be reached to check the key."
            elif "_status" in info:
                account.update(signed_in=False, warning="The saved key was rejected by OpenRouter. Sign in again.")
            else:
                account.update({k: info.get(k) for k in ("label", "usage", "limit", "limit_remaining")})
        native = {p: {"label": label, "saved": bool(auth.stored_key(p)[0])} for p, label in auth.NATIVE_LABELS.items()}
        models = [{"id": alias, "model": model, "note": MODEL_NOTES.get(alias, "")} for alias, model in MODEL_ALIASES.items()]
        models += [{"id": example, "model": example,
                    "note": f"direct to {auth.NATIVE_LABELS[p]}, without OpenRouter; needs a {auth.NATIVE_LABELS[p]} key"}
                   for p, example in DIRECT_EXAMPLES.items()]
        return {"account": account, "native": native, "models": models, "default_model": DEFAULT_MODEL,
                "version": __version__, "job": self.job.summary() if self.job else None}

    def login(self, body: dict) -> dict:
        limit = body.get("limit")
        if limit not in (None, ""):
            try:
                amount = round(float(limit), 2)
            except ValueError:
                raise AppError("The spending limit must be a number of US dollars, such as 10.") from None
            if not 0 < amount < 1e6:
                raise AppError("The spending limit must be more than zero, or left empty for no limit.")
            limit = f"{amount:g}"
        else:
            limit = None
        now = time.time()
        self.logins = {s: v for s, v in self.logins.items() if now - v[1] < LOGIN_TTL}
        verifier, challenge = auth.pkce_pair()
        state = secrets.token_urlsafe(16)
        self.logins[state] = (verifier, now)
        return {"url": auth.auth_url(challenge, f"http://localhost:{self.port}/callback", state, limit, None)}

    def callback(self, query: dict) -> tuple[str, str]:
        """(title, message) on failure; ('', '') once the key is saved."""
        pending = self.logins.pop(query.get("state", ""), None)
        if "code" not in query or pending is None:
            reason = query.get("error") or "this sign-in was not started from the open Transcriber page, or has expired"
            return "Sign-in did not finish", reason
        try:
            key = auth.exchange(query["code"], pending[0])
        except SystemExit as e:
            return "Sign-in did not finish", str(e).split("\n")[0]
        auth.save_key(key)
        return "", ""

    def logout(self, _: dict) -> dict:
        auth.delete_key()
        return {"ok": True}

    def save_native_key(self, body: dict) -> dict:
        provider = body.get("provider")
        if provider not in auth.NATIVE_LABELS:
            raise AppError("unknown provider")
        if body.get("remove"):
            auth.delete_key(provider)
            return {"ok": True}
        key = (body.get("key") or "").strip()
        if not key or len(key) > 500 or any(c.isspace() for c in key):
            raise AppError("That does not look like an API key. Copy it again from the provider's website.")
        auth.save_key(key, provider)
        return {"ok": True}

    # ------------------------------------------------ documents

    def pick(self, body: dict) -> dict:
        return {"path": pick_path("folder" if body.get("kind") == "folder" else "file")}

    def document(self, body: dict) -> dict:
        src = open_source(body.get("path", ""))
        info = summarize(src)
        out, _ = output_dirs(src)
        run = validate.read_run(out, src)
        n = info["pages"]
        done = done_pages(out, src, n)
        first = next(iter(run.get("pages", {}).values()), {}) if run else {}
        previous = None
        if run and done:
            previous = {"kind": kind_of(run), "language": run.get("language") or "",
                        "lines": run.get("layout") == "lines", "diplomatic": bool(run.get("diplomatic")),
                        "faint": first.get("dpi", 150) >= 200, "model": first.get("fallback_from") or first.get("model")}
        review = out / f"{src.stem}.review.html"
        return {"path": str(src.path), "name": src.name, "kind": src.kind, "pages": n,
                "text_layer": info["text_layer"], "suggested_kind": info["suggested_profile"],
                "per_page": info["per_page"], "output": str(out), "done": [i + 1 for i in done],
                "previous": previous, "review": review.exists(), "preview": src.preview(0, max_side=900)}

    def estimate(self, body: dict) -> dict:
        src = open_source(body.get("path", ""))
        s = make_settings(body.get("settings") or {})
        out, _ = output_dirs(src)
        n = src.page_count()
        pilot = body.get("mode") == "pilot"
        changed = mismatch(validate.read_run(out, src), s)
        spec = f"1-{min(PILOT_PAGES, n)}" if pilot else None
        e = transcribe.compute_estimate([src], s, out, spec, force=pilot or changed)
        return {**e, "mismatch": changed, "pilot_pages": min(PILOT_PAGES, n)}

    def start(self, body: dict) -> dict:
        src = open_source(body.get("path", ""))
        s = make_settings(body.get("settings") or {})
        check_credentials(s.models, s.deny_data_collection)
        out, _ = output_dirs(src)
        n = src.page_count()
        pilot = body.get("mode") == "pilot"
        force = pilot or mismatch(validate.read_run(out, src), s)
        wanted = transcribe.parse_pages(f"1-{min(PILOT_PAGES, n)}" if pilot else None, n)
        pdir = transcribe.pages_dir(out, src)
        todo = [i for i in wanted if force or not transcribe.page_file(pdir, i).exists()]
        job = self._new_job("pilot" if pilot else "transcribe", src, todo, pdir)
        spec = f"1-{min(PILOT_PAGES, n)}" if pilot else None

        def work() -> None:
            done, failed = transcribe.run([src], s, out, {}, spec, force, listener=job.add, stop=job.stop)
            job.result = {"done": done, "failed": failed, "markdown": str(out / f"{src.stem}.md")}

        self._run(job, work)
        return {"job": job.summary()}

    def check_estimate(self, body: dict) -> dict:
        src = open_source(body.get("path", ""))
        plan = check_plan(src)
        if not plan["ready"]:
            return {"ready": False}
        spec = ",".join(str(i + 1) for i in plan["done"])
        e = transcribe.compute_estimate([src], plan["settings"], plan["out_b"], spec, force=False)
        return {"ready": True, **e, "checked_pages": len(plan["done"])}

    def check(self, body: dict) -> dict:
        src = open_source(body.get("path", ""))
        plan = check_plan(src)
        if not plan["ready"]:
            raise AppError("Transcribe the document first (step 4 or 5).")
        s = plan["settings"]
        check_credentials(s.models, False)
        pdir_b = transcribe.pages_dir(plan["out_b"], src)
        todo = [i for i in plan["done"] if not transcribe.page_file(pdir_b, i).exists()]
        job = self._new_job("check", src, todo, pdir_b)

        def work() -> None:
            flagged = validate.validate(src, plan["out"], plan["out_b"], {}, workers=4,
                                        listener=job.add, stop=job.stop)
            job.result = {"flagged": flagged, "pages": len(plan["done"]),
                          "review": str(plan["out"] / f"{src.stem}.review.html")}

        self._run(job, work)
        return {"job": job.summary()}

    def _new_job(self, kind: str, src: Source, todo: list[int], count_dir: Path) -> Job:
        with self.lock:
            if self.job and self.job.state == "running":
                raise AppError("Something is already running. Wait for it to finish, or stop it.", 409)
            self.job = Job(kind, src, todo, count_dir)
            return self.job

    def _run(self, job: Job, work) -> None:
        def target() -> None:
            try:
                work()
            except SystemExit as e:  # configuration, credentials, credit: the message is meant for people
                job.error = str(e) or "the run stopped"
            except Exception as e:  # noqa: BLE001 - report anything else on the page too
                job.error = f"{type(e).__name__}: {e}"
            job.result["cost"] = SPEND.cost - job.cost_before
            job.state = "failed" if job.error else "stopped" if job.stop.is_set() else "done"

        threading.Thread(target=target, daemon=True).start()

    def stop(self, _: dict) -> dict:
        if self.job and self.job.state == "running":
            self.job.stop.set()
        return {"job": self.job.summary() if self.job else None}

    def job_status(self, _: dict) -> dict:
        return {"job": self.job.summary() if self.job else None}

    def page(self, body: dict) -> dict:
        src = open_source(body.get("path", ""))
        out, _ = output_dirs(src)
        n = src.page_count()
        number = int(body.get("page") or 1)
        if not 1 <= number <= n:
            raise AppError("no such page")
        f = transcribe.page_file(transcribe.pages_dir(out, src), number - 1)
        run = validate.read_run(out, src)
        meta = run.get("pages", {}).get(str(number), {}) if run else {}
        return {"page": number, "label": src.label(number - 1), "image": src.preview(number - 1),
                "text": f.read_text(encoding="utf-8") if f.exists() else None, "model": meta.get("model"),
                "used_fallback": bool(meta.get("used_fallback"))}

    def open(self, body: dict) -> dict:
        """Open only what this app made for the document: never an arbitrary path."""
        src = open_source(body.get("path", ""))
        out, _ = output_dirs(src)
        what = body.get("what")
        targets = {"text": (out / f"{src.stem}.md", "text"), "folder": (out / f"{src.stem}.md", "reveal"),
                   "review": (out / f"{src.stem}.review.html", "browser")}
        if what not in targets:
            raise AppError("unknown thing to open")
        path, how = targets[what]
        if not path.exists():
            raise AppError(f"{path.name} does not exist yet.")
        open_with_system(path, how)
        return {"ok": True}

    ROUTES = {"status": status, "login": login, "logout": logout, "key": save_native_key, "pick": pick,
              "document": document, "estimate": estimate, "start": start, "check-estimate": check_estimate,
              "check": check, "stop": stop, "job": job_status, "page": page, "open": open}


# ---------------------------------------------------------------- HTTP

def make_handler(app: App, server_ref: dict):
    allowed_hosts = {f"localhost:{app.port}", f"127.0.0.1:{app.port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the terminal for the run's own progress
            pass

        def _headers(self, status: int, ctype: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def _json(self, status: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _html(self, status: int, text: str) -> None:
            body = text.encode()
            self._headers(status, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            # A page elsewhere that points its own name at 127.0.0.1 still sends its own Host.
            if self.headers.get("Host") in allowed_hosts:
                return True
            self._json(403, {"error": "wrong host"})
            return False

        def do_GET(self):
            if not self._host_ok():
                return
            u = urlparse(self.path)
            if u.path == "/":
                return self._html(200, PAGE)
            if u.path == "/callback":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                title, message = app.callback(q)
                if not title:
                    self.send_response(303)
                    self.send_header("Location", "/#signed-in")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                import html
                return self._html(200, auth.PAGE.format(
                    title=html.escape(title),
                    body=html.escape(message) + '. <a href="/">Back to Transcriber</a> and try again.'))
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return
            u = urlparse(self.path)
            token = self.headers.get("X-Transcriber-Token") or ""
            if not secrets.compare_digest(token.encode(), app.token.encode()):
                return self._json(403, {"error": "This page is no longer connected to Transcriber. "
                                                 "Close it and start Transcriber again."})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "malformed request"})
            name = u.path.removeprefix("/api/")
            if name == "quit":
                self._json(200, {"ok": True})
                threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()
                return
            route = App.ROUTES.get(name)
            if route is None or not u.path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            try:
                self._json(200, route(app, body if isinstance(body, dict) else {}))
            except AppError as e:
                self._json(e.status, {"error": str(e)})
            except SystemExit as e:
                self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 - show it on the page rather than drop the connection
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def serve(port: int = 0, open_browser: bool = True) -> None:
    server_ref: dict = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), lambda *a: None)  # handler set below, once the port is known
    server.daemon_threads = True
    app = App(server.server_address[1])
    server.RequestHandlerClass = make_handler(app, server_ref)
    server_ref["server"] = server
    print("Transcriber is running. It should open in your web browser; if it does not, open this address:\n\n  "
          f"{app.url}\n\nKeep this window open while you work. To quit, use the Quit button on the page, "
          "close this window, or press Ctrl+C.\n", flush=True)
    if open_browser and not os.environ.get("TRANSCRIBER_NO_BROWSER"):
        threading.Timer(0.3, webbrowser.open, [app.url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("Transcriber has stopped.", flush=True)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transcriber</title>
<style>
:root{--bg:#f5f4f0;--panel:#fff;--ink:#1d1d1b;--muted:#66645e;--line:#e2e0da;--accent:#1f5fbf;--accent-ink:#fff;
--ok:#1e7a3c;--warn:#9a5b00;--bad:#b3261e;--soft:#eef3fb;--warnbg:#fff6e0;--badbg:#fdecea}
@media (prefers-color-scheme:dark){:root{--bg:#171716;--panel:#212120;--ink:#ecebe7;--muted:#a3a19a;--line:#3a3937;
--accent:#7aa7ff;--accent-ink:#0d1626;--ok:#6fcf8d;--warn:#f0b54a;--bad:#ff8a80;--soft:#1f2a3c;--warnbg:#3a2f15;--badbg:#3d1f1c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI","Noto Sans","PingFang SC","Noto Sans CJK SC",sans-serif}
header,main,footer{max-width:960px;margin:0 auto;padding:0 16px}
header{padding-top:28px;padding-bottom:8px}
header h1{margin:0;font-size:26px}
header p{margin:4px 0 0;color:var(--muted)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:16px 0}
.card h2{font-size:18px;margin:0 0 10px;display:flex;align-items:center;gap:10px}
.num{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:50%;background:var(--soft);color:var(--accent);font-size:14px;font-weight:700;flex:none}
.card.off{opacity:.55}
.card.off button,.card.off input,.card.off select{pointer-events:none}
p{margin:6px 0}
.muted{color:var(--muted)} .small{font-size:14px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:10px 0}
button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:8px;padding:8px 14px;cursor:pointer}
button:hover{border-color:var(--muted)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
button:disabled{opacity:.5;cursor:default}
button.link{border:none;background:none;color:var(--accent);padding:0;text-decoration:underline}
input[type=text],input[type=password],input[type=number],select{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 10px;min-width:0}
input[type=text]{flex:1 1 260px}
label{cursor:pointer}
.kinds{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;margin:8px 0 12px}
.kinds label{border:1px solid var(--line);border-radius:10px;padding:10px 12px;display:flex;gap:8px;align-items:flex-start}
.kinds label:has(input:checked){border-color:var(--accent);background:var(--soft)}
.kinds small{display:block;color:var(--muted)}
.checks label{display:flex;gap:8px;align-items:flex-start;margin:6px 0}
.checks small{color:var(--muted)}
details{margin:8px 0} summary{cursor:pointer;color:var(--muted)}
.status{padding:10px 12px;border-radius:8px;background:var(--soft);margin:8px 0}
.status.ok{color:var(--ok)} .status.warn{background:var(--warnbg);color:var(--warn)} .status.bad{background:var(--badbg);color:var(--bad)}
.doc{display:grid;grid-template-columns:140px minmax(0,1fr);gap:16px;align-items:start;margin-top:12px;overflow-wrap:anywhere}
.status,#checkCost,#error{overflow-wrap:anywhere}
.doc img{width:140px;border:1px solid var(--line);border-radius:6px;background:#fff}
.doc .name{font-weight:600;overflow-wrap:anywhere}
.viewer{margin-top:14px}
.viewer .bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.viewer .cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}
.viewer .scan{border:1px solid var(--line);border-radius:8px;overflow:auto;max-height:75vh;background:#fff}
.viewer .scan img{display:block;width:100%}
.viewer pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.7 inherit;font-family:inherit;border:1px solid var(--line);border-radius:8px;padding:12px 14px;max-height:75vh;overflow:auto;background:var(--panel)}
.job{z-index:5;background:var(--panel);border:1px solid var(--accent);border-radius:12px;padding:12px 16px;margin:16px 0;box-shadow:0 6px 24px rgba(0,0,0,.12)}
.job.running{position:sticky;bottom:12px}
.progress{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:8px 0}
.progress div{height:100%;background:var(--accent);width:0;transition:width .4s}
.log{font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow:auto;margin:6px 0 0}
.hidden{display:none!important}
footer{padding-bottom:40px;color:var(--muted);font-size:14px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
@media (max-width:700px){.viewer .cols{grid-template-columns:1fr}.doc{grid-template-columns:90px minmax(0,1fr)}.doc img{width:90px}}
</style>
</head>
<body>
<header>
  <h1>Transcriber</h1>
  <p>Turn scans and photos of documents into faithful text you can check against the original.</p>
</header>
<main>
<div id="expired" class="card status bad hidden">This page is no longer connected to Transcriber. Close it and start Transcriber again.</div>

<section class="card" id="s1">
  <h2><span class="num">1</span>Sign in</h2>
  <div id="signin"><p class="muted">Checking…</p></div>
</section>

<section class="card off" id="s2">
  <h2><span class="num">2</span>Choose a document</h2>
  <p class="muted">A PDF of scans, a single photo, or a folder of photos with one photo per page.</p>
  <div class="row">
    <button id="pickFile" class="primary">Choose a PDF or photo…</button>
    <button id="pickFolder">Choose a folder of photos…</button>
  </div>
  <details id="typeDetails"><summary>Or type its location</summary>
    <form id="pathForm" class="row"><input type="text" id="pathInput" placeholder="/Users/you/Documents/letters.pdf" aria-label="Location of the document"><button>Open</button></form>
    <p class="muted small">On a Mac, select the file in Finder and press Option-Command-C to copy its location. On Windows, hold Shift, right-click it and choose “Copy as path”.</p>
  </details>
  <div id="doc"></div>
</section>

<section class="card off" id="s3">
  <h2><span class="num">3</span>Describe it</h2>
  <p class="muted">This tells the model what to expect.</p>
  <div class="kinds" role="radiogroup" aria-label="Kind of document">
    <label><input type="radio" name="kind" value="printed" checked><span>Printed or typed<small>books, typescript, carbon copies, forms, newspapers</small></span></label>
    <label><input type="radio" name="kind" value="handwritten"><span>Handwritten<small>letters, diaries, notebooks, annotations</small></span></label>
    <label><input type="radio" name="kind" value="cjk"><span>Chinese, Japanese or Korean, printed<small>print, typescript, mimeograph (油印)</small></span></label>
    <label><input type="radio" name="kind" value="handwritten-cjk"><span>Chinese, Japanese or Korean, handwritten<small>manuscripts and letters</small></span></label>
  </div>
  <div class="row"><label for="language">Language and script</label><input type="text" id="language" placeholder="for example: German (Fraktur), French and Latin, Ottoman Turkish in Arabic script"></div>
  <div class="checks">
    <label><input type="checkbox" id="lines"><span>Keep every line break<br><small>for poems, registers and ledgers</small></span></label>
    <label><input type="checkbox" id="diplomatic"><span>Keep old letterforms as written<br><small>long s, ligatures and abbreviation marks</small></span></label>
    <label><input type="checkbox" id="faint"><span>Small or faint print<br><small>sends sharper page images; costs a little more</small></span></label>
    <label><input type="checkbox" id="private"><span>Only use services that do not store or train on the pages<br><small>may rule out some models</small></span></label>
  </div>
  <details><summary>Model</summary>
    <div class="row"><select id="model" aria-label="Model"></select></div>
    <p class="muted small" id="modelNote"></p>
    <p class="muted small">If a page fails or the model refuses it, Transcriber tries that page again with a different model family automatically.</p>
  </details>
</section>

<section class="card off" id="s4">
  <h2><span class="num">4</span>Try a few pages</h2>
  <p>Transcribe the first pages, then compare each with its scan: look for missing lines, spellings “corrected” by the model, and misread names or numbers. If something is wrong, change step 3 and try again.</p>
  <div class="row"><button id="pilot" class="primary">Try the first pages</button><span id="pilotCost" class="muted"></span></div>
  <div id="viewer" class="viewer hidden">
    <div class="bar"><button id="prevPage">‹ Previous</button><button id="nextPage">Next ›</button><strong id="pageLabel"></strong><span id="pageModel" class="muted small"></span></div>
    <div class="cols"><div class="scan"><img id="pageImg" alt="scan of the page"></div><pre id="pageText"></pre></div>
  </div>
</section>

<section class="card off" id="s5">
  <h2><span class="num">5</span>Transcribe the whole document</h2>
  <div id="fullInfo"></div>
  <div class="row"><button id="full" class="primary">Transcribe</button><span id="fullCost" class="muted"></span></div>
  <div class="row hidden" id="outputs"></div>
</section>

<section class="card off" id="s6">
  <h2><span class="num">6</span>Check it with a second model</h2>
  <p>A different model reads the pages again. Wherever the two disagree, or one skipped a line, you decide against the scan in a proofreading app. This usually costs 2 to 10 times as much as the transcription.</p>
  <div class="row"><button id="check" class="primary">Check with a second model</button><span id="checkCost" class="muted"></span></div>
  <div id="checkInfo"></div>
</section>

<div id="job" class="job hidden" role="status" aria-live="polite">
  <div class="row" style="margin:0;justify-content:space-between"><strong id="jobTitle"></strong><button id="stop">Stop</button><button id="dismiss" class="hidden">Close</button></div>
  <div class="progress"><div id="jobBar"></div></div>
  <div id="jobText" class="small"></div>
  <details><summary>Details</summary><div id="jobLog" class="log"></div></details>
</div>
<div id="error" class="card status bad hidden" role="alert"></div>
</main>
<footer><span id="version"></span><span>Finished pages are saved as they come in, so a stopped run continues where it left off. <button id="quit" class="link">Quit Transcriber</button></span></footer>

<script>
"use strict";
const $ = s => document.querySelector(s);
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : String(c));
  return e;
}
let TOKEN = null;
(function () {
  const m = location.hash.match(/token=([\w-]+)/);
  try { if (m) sessionStorage.setItem("transcriber-token", m[1]); TOKEN = m ? m[1] : sessionStorage.getItem("transcriber-token"); }
  catch (e) { TOKEN = m ? m[1] : null; }
  if (location.hash) history.replaceState(null, "", location.pathname);
})();
window.addEventListener("hashchange", () => { if (/token=/.test(location.hash)) location.reload(); });

async function api(path, body) {
  let r;
  try {
    r = await fetch("/api/" + path, {method: "POST", headers: {"Content-Type": "application/json", "X-Transcriber-Token": TOKEN || ""},
                                    body: JSON.stringify(body || {})});
  } catch (e) { throw new Error("Transcriber is not running any more. Start it again, then reload this page."); }
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (r.status === 403) $("#expired").classList.remove("hidden");
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}
function showError(e) { const box = $("#error"); box.textContent = e ? (e.message || String(e)) : ""; box.classList.toggle("hidden", !e); if (e) box.scrollIntoView({block: "nearest", behavior: "smooth"}); }
async function act(fn) { showError(null); try { return await fn(); } catch (e) { showError(e); } }
function usd(x) { if (x == null) return "price unknown"; if (x < 0.01) return "less than 1 cent"; return "$" + x.toFixed(2); }
function about(x) { if (x == null) return "price unknown"; if (x < 0.01) return "under 1 cent"; return "about $" + x.toFixed(2); }
function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

const S = {status: null, doc: null, est: {}, check: null, view: 0, polling: false};

// ---------------------------------------------------------------- step 1
async function loadStatus() {
  S.status = await api("status");
  $("#version").textContent = "Transcriber " + S.status.version;
  renderSignin();
  const sel = $("#model");
  if (!sel.options.length) {
    for (const m of S.status.models) sel.append(h("option", {value: m.id}, m.id === m.model ? m.id : m.id + " (" + m.model + ")"));
    sel.value = "flash";
    showModelNote();
  }
  if (S.status.job && S.status.job.state === "running") { showJob(S.status.job); poll(); }
}
function renderSignin() {
  const a = S.status.account, box = $("#signin");
  box.replaceChildren();
  if (a.signed_in) {
    let line = "Signed in to OpenRouter.";
    if (a.usage != null) line += " Used " + usd(a.usage) + " so far";
    if (a.limit != null) line += "; spending limit $" + Number(a.limit).toFixed(2) + ", " + usd(a.limit_remaining) + " left.";
    else if (a.usage != null) line += "; no spending limit on this key.";
    box.append(h("div", {class: "status ok"}, "✓ " + line));
    if (a.warning) box.append(h("div", {class: "status warn"}, a.warning));
    box.append(h("div", {class: "row"},
      h("button", {onclick: () => window.open("https://openrouter.ai/settings/credits", "_blank", "noopener")}, "Add credit"),
      h("button", {onclick: () => act(async () => { if (confirm("Sign out? You will need to sign in again before the next run.")) { await api("logout"); await loadStatus(); } })}, "Sign out")));
  } else {
    if (a.warning) box.append(h("div", {class: "status warn"}, a.warning));
    box.append(h("p", {}, "Transcriber sends page images to AI models through OpenRouter, which bills you directly. A typical page costs a fraction of a cent. Sign in or create a free account there, add a little credit, and approve. You come straight back here."));
    const limit = h("input", {type: "number", min: "1", step: "1", value: "10", style: "width:7em", "aria-label": "Spending limit in US dollars"});
    box.append(h("div", {class: "row"},
      h("button", {class: "primary", onclick: () => act(async () => { const r = await api("login", {limit: limit.value}); location.href = r.url; })}, "Sign in with OpenRouter"),
      h("label", {}, "Spending limit for this computer: $ "), limit));
    box.append(h("p", {class: "muted small"}, "The limit caps what Transcriber can ever spend from your account; leave it empty for no limit. You can change it later on openrouter.ai."));
  }
  const nat = h("details", {}, h("summary", {}, "Other providers (optional)"),
    h("p", {class: "muted small"}, "Only needed to use Google, OpenAI, xAI or Anthropic models directly instead of through OpenRouter. Keys are saved in this computer's keychain and are never shown again."));
  for (const [p, info] of Object.entries(S.status.native)) {
    const input = h("input", {type: "password", placeholder: info.label + " API key", autocomplete: "off", "aria-label": info.label + " API key"});
    nat.append(h("div", {class: "row"}, h("span", {style: "min-width:9em"}, info.label), info.saved ? h("span", {class: "muted"}, "✓ key saved") : input,
      info.saved ? h("button", {onclick: () => act(async () => { await api("key", {provider: p, remove: true}); await loadStatus(); })}, "Remove")
                 : h("button", {onclick: () => act(async () => { await api("key", {provider: p, key: input.value}); input.value = ""; await loadStatus(); })}, "Save")));
  }
  box.append(nat);
  $("#s2").classList.toggle("off", false);
}

// ---------------------------------------------------------------- step 2
async function pick(kind) {
  await act(async () => { const r = await api("pick", {kind}); if (r.path) await openDoc(r.path); });
}
async function openDoc(path) {
  const d = await api("document", {path});
  S.doc = d;
  const info = [h("div", {class: "name"}, d.name), h("div", {class: "muted small"}, d.path),
    h("p", {}, plural(d.pages, "page") + (d.kind === "pdf" ? " in a PDF." : (d.pages === 1 ? ", a photo." : ", one photo per page.")))];
  if (d.text_layer) {
    info.push(h("p", {class: "small"}, d.text_layer.clean
      ? "This PDF already contains text that looks clean. You may not need to transcribe it; check by searching or copying text from it in your PDF reader."
      : "This PDF contains text from old OCR that looks garbled; transcribing will help."));
  }
  if (d.per_page != null) info.push(h("p", {class: "small muted"}, "With the default model: " + about(d.per_page) + " a page, " + about(d.per_page * d.pages) + " for all " + plural(d.pages, "page") + "."));
  if (d.done.length) info.push(h("p", {class: "small"}, plural(d.done.length, "page") + " of " + d.pages + " already transcribed; the settings used then are filled in below."));
  info.push(h("p", {class: "small muted"}, "Results go into " + d.output));
  $("#doc").replaceChildren(h("div", {class: "doc"}, h("img", {src: d.preview, alt: "first page"}), h("div", {}, info)));
  if (d.previous) {
    const p = d.previous;
    document.querySelector(`input[name=kind][value="${p.kind}"]`).checked = true;
    $("#language").value = p.language; $("#lines").checked = p.lines; $("#diplomatic").checked = p.diplomatic; $("#faint").checked = p.faint;
    const known = S.status.models.find(m => m.model === p.model || m.id === p.model);
    if (known) $("#model").value = known.id; else if (p.model) { $("#model").append(h("option", {value: p.model}, p.model)); $("#model").value = p.model; }
    showModelNote();
  } else if (d.suggested_kind === "cjk") {
    document.querySelector("input[name=kind][value=cjk]").checked = true;
  }
  for (const id of ["#s3", "#s4", "#s5", "#s6"]) $(id).classList.remove("off");
  await refresh();
  if (d.done.length) showPage(d.done[0]); else $("#viewer").classList.add("hidden");
}

// ---------------------------------------------------------------- step 3
function settings() {
  return {kind: document.querySelector("input[name=kind]:checked").value, language: $("#language").value,
          lines: $("#lines").checked, diplomatic: $("#diplomatic").checked, faint: $("#faint").checked,
          private: $("#private").checked, model: $("#model").value};
}
function showModelNote() { const m = (S.status.models || []).find(m => m.id === $("#model").value); $("#modelNote").textContent = m ? m.note : ""; }
let timer = null;
function settingsChanged() { showModelNote(); clearTimeout(timer); timer = setTimeout(() => act(refresh), 350); }

// ---------------------------------------------------------------- steps 4-6
async function refresh() {
  if (!S.doc) return;
  const path = S.doc.path, s = settings();
  const [pilot, all, check] = await Promise.all([api("estimate", {path, settings: s, mode: "pilot"}),
    api("estimate", {path, settings: s, mode: "all"}), api("check-estimate", {path})]);
  S.est = {pilot, all}; S.check = check;
  $("#pilot").textContent = "Try the first " + plural(pilot.pilot_pages, "page");
  $("#pilotCost").textContent = about(pilot.cost);
  const done = S.doc.done.length, info = $("#fullInfo");
  info.replaceChildren();
  if (all.mismatch) info.append(h("div", {class: "status warn"}, "The pages already transcribed used different settings, so every page will be done again with the settings above."));
  if (all.pages === 0) {
    info.append(h("p", {}, "All " + plural(S.doc.pages, "page") + " are transcribed."));
    $("#full").classList.add("hidden"); $("#fullCost").textContent = "";
  } else {
    $("#full").classList.remove("hidden");
    $("#full").textContent = all.pages === S.doc.pages ? "Transcribe all " + plural(all.pages, "page") : "Transcribe the remaining " + plural(all.pages, "page");
    $("#fullCost").textContent = about(all.cost);
  }
  const outs = $("#outputs");
  outs.replaceChildren();
  if (done) {
    outs.append(h("button", {onclick: () => act(() => api("open", {path, what: "text"}))}, "Open the text"),
                h("button", {onclick: () => act(() => api("open", {path, what: "folder"}))}, "Show the files"));
  }
  outs.classList.toggle("hidden", !done);
  const ci = $("#checkInfo");
  ci.replaceChildren();
  if (!check.ready) {
    $("#check").disabled = true; $("#check").classList.remove("hidden"); $("#checkCost").textContent = "transcribe some pages first";
  } else if (!check.pages && S.doc.review) {
    $("#check").classList.add("hidden"); $("#checkCost").textContent = "Checked: both models have read every transcribed page.";
  } else {
    $("#check").disabled = false; $("#check").classList.remove("hidden");
    $("#check").textContent = check.pages ? "Check with a second model" : "Finish the check";
    $("#checkCost").textContent = check.pages ? plural(check.checked_pages, "page") + " with " + check.model + ", " + about(check.cost) : "no cost: the second reading is already done";
    if (check.checked_pages < S.doc.pages) ci.append(h("p", {class: "small muted"}, "Only the " + plural(check.checked_pages, "page") + " transcribed so far will be checked."));
  }
  if (S.doc.review) ci.append(h("div", {class: "row"}, h("button", {class: "primary", onclick: () => act(() => api("open", {path, what: "review"}))}, "Open the proofreading app"),
    h("span", {class: "muted small"}, "Settle each marked word against the scan, then use “Export final Markdown”.")));
}

async function start(mode) {
  await act(async () => {
    const est = mode === "check" ? S.check : S.est[mode];
    if (est && est.cost != null && est.cost > 1 && !confirm("This will cost about " + usd(est.cost) + ". Go ahead?")) return;
    if (est && est.cost == null && !confirm("The price of this model is unknown, so the cost cannot be estimated. Go ahead?")) return;
    const r = mode === "check" ? await api("check", {path: S.doc.path}) : await api("start", {path: S.doc.path, settings: settings(), mode});
    showJob(r.job); poll();
  });
}
const DONE_TITLES = {pilot: "Pilot finished", transcribe: "Transcription finished", check: "Check finished"};
const TITLES = {pilot: "Trying the first pages", transcribe: "Transcribing", check: "Checking with a second model"};
function showJob(j) {
  $("#job").classList.remove("hidden");
  $("#job").classList.toggle("running", j.state === "running");
  for (const b of ["#pilot", "#full", "#check", "#pickFile", "#pickFolder"]) $(b).disabled = j.state === "running";
  $("#jobTitle").textContent = j.state === "running" ? (TITLES[j.kind] || "Working") + "…" : j.state === "failed" ? "Something went wrong" : j.state === "stopped" ? "Stopped" : (DONE_TITLES[j.kind] || "Finished");
  $("#jobBar").style.width = (j.total ? Math.round(100 * j.done / j.total) : (j.state === "running" ? 5 : 100)) + "%";
  let text = j.total ? j.done + " of " + plural(j.total, "page") + " done" : "";
  if (j.stopping) text += " · stopping after the pages in progress";
  if (j.state === "done") text = j.kind === "check" ? "Finished: " + (j.result.flagged ? plural(j.result.flagged, "page") + " of " + j.result.pages + (j.result.flagged === 1 ? " needs" : " need") + " a look in the proofreading app." : "the two models agree on all " + plural(j.result.pages, "page") + ".") : "Finished: " + plural(j.result.done, "page") + " transcribed" + (j.result.failed ? ", " + j.result.failed + " failed (run again to retry only those)" : "") + ".";
  if (j.state === "stopped") text = "Stopped. Finished pages are saved; start again to continue.";
  if (j.state === "failed") text = "Stopped with a problem: " + j.error;
  if (j.state !== "running" && j.result && j.result.cost) text += " Cost reported by OpenRouter: " + usd(j.result.cost) + ".";
  $("#jobText").textContent = text;
  $("#jobText").className = "small" + (j.state === "failed" ? " status bad" : "");
  $("#jobLog").textContent = j.lines.join("\n");
  $("#stop").classList.toggle("hidden", j.state !== "running");
  $("#dismiss").classList.toggle("hidden", j.state === "running");
}
async function poll() {
  if (S.polling) return;
  S.polling = true;
  try {
    while (true) {
      await new Promise(r => setTimeout(r, 1000));
      const {job} = await api("job");
      if (!job) break;
      showJob(job);
      if (job.state !== "running") {
        if (S.doc && job.source === S.doc.path) {
          const keep = S.view;
          await openDoc(S.doc.path);
          if (job.kind === "pilot" && S.doc.done.length) showPage(S.doc.done[0]);
          else if (keep && S.doc.done.includes(keep)) showPage(keep);
          if (job.kind === "check" && job.state === "done") $("#s6").scrollIntoView({behavior: "smooth"});
          if (job.kind === "pilot") $("#viewer").scrollIntoView({behavior: "smooth", block: "start"});
        }
        await loadStatus();
        break;
      }
    }
  } catch (e) { showError(e); }
  S.polling = false;
}
async function showPage(n) {
  await act(async () => {
    const p = await api("page", {path: S.doc.path, page: n});
    S.view = n;
    $("#viewer").classList.remove("hidden");
    $("#pageLabel").textContent = p.label[0].toUpperCase() + p.label.slice(1) + " of " + S.doc.pages;
    $("#pageModel").textContent = p.model ? "read by " + p.model + (p.used_fallback ? " (after the first model failed)" : "") : "";
    $("#pageImg").src = p.image;
    $("#pageText").textContent = p.text == null ? "(not transcribed yet)" : p.text;
    const i = S.doc.done.indexOf(n);
    $("#prevPage").disabled = i <= 0; $("#nextPage").disabled = i < 0 || i >= S.doc.done.length - 1;
  });
}
function step(d) { const i = S.doc.done.indexOf(S.view); const n = S.doc.done[i + d]; if (n) showPage(n); }

$("#pickFile").onclick = () => pick("file");
$("#pickFolder").onclick = () => pick("folder");
$("#pathForm").onsubmit = e => { e.preventDefault(); act(() => openDoc($("#pathInput").value)); };
for (const el of document.querySelectorAll("#s3 input, #s3 select")) el.addEventListener(el.type === "text" ? "input" : "change", settingsChanged);
$("#pilot").onclick = () => start("pilot");
$("#full").onclick = () => start("all");
$("#check").onclick = () => start("check");
$("#stop").onclick = () => act(async () => showJob((await api("stop")).job));
$("#dismiss").onclick = () => $("#job").classList.add("hidden");
$("#prevPage").onclick = () => step(-1);
$("#nextPage").onclick = () => step(1);
$("#quit").onclick = () => act(async () => {
  if (S.status && S.status.job && S.status.job.state === "running" && !confirm("A run is still going. Quit anyway? Finished pages are kept.")) return;
  await api("quit");
  document.body.replaceChildren(h("main", {}, h("div", {class: "card"}, h("h2", {}, "Transcriber has stopped"), h("p", {}, "You can close this tab."))));
});
if (!TOKEN) $("#expired").classList.remove("hidden"); else act(loadStatus);
</script>
</body>
</html>
"""
