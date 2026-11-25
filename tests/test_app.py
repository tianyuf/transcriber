"""The browser app against the local OpenRouter mock: sign in, open a document, pilot, full run, check.

Runs the real app server in a subprocess. Never touches the real keychain, OpenRouter, a file
dialog or a browser.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pymupdf
import pytest

from transcriber import transcribe
from transcriber.sources import Source

ROOT = Path(__file__).resolve().parent.parent
PAGES = [
    "Dear Mrs Hale,\nThank you for your letter of March 3.\nI shall be in Leeds on Tuesday.",
    "The second page of the letter.\nWith kind regards,\nArthur Pemberton",
    "A third page about the harvest accounts.",
    "A fourth page, a postscript about the weather.",
]
A, B = "qwen/qwen3.7-flash", "google/gemini-3.1-flash-lite"
KEY = "sk-or-v1-mocktestkey0000000000000000"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out")


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("app")
    docs = tmp / "My Archive"  # spaces, as in real folder names
    docs.mkdir()
    pdf = docs / "letters.pdf"
    doc = pymupdf.open()
    for text in PAGES:
        doc.new_page().insert_text((72, 100), text, fontsize=12)
    doc.save(pdf)
    src = Source(pdf)
    hashes = [hashlib.sha256(src.render(i, 150)[0]).hexdigest() for i in range(src.page_count())]
    answers = {A: dict(zip(hashes, PAGES)), B: dict(zip(hashes, PAGES))}
    answers[A][hashes[1]] = PAGES[1].replace("Pemberton", "Pembertom")  # a misreading for the check to find
    (tmp / "answers.json").write_text(json.dumps(answers))

    mock_port = free_port()
    mock = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_openrouter.py"), str(mock_port),
                             str(tmp / "answers.json")])
    wait_for(lambda: _up(f"http://127.0.0.1:{mock_port}/api/v1/models"), 10)
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
    env.update(OPENROUTER_BASE_URL=f"http://127.0.0.1:{mock_port}", TRANSCRIBER_NO_KEYRING="1",
               TRANSCRIBER_CONFIG_DIR=str(tmp / "config"), PYTHONIOENCODING="utf-8", OPENAI_API_KEY="")
    server = subprocess.Popen([sys.executable, "-m", "transcriber", "app", "--no-browser"], cwd=tmp, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    for line in server.stdout:
        m = re.search(r"http://localhost:(\d+)/#token=([\w-]+)", line)
        if m:
            url = m
            break
    assert url, "the app did not print its address"
    threading.Thread(target=server.stdout.read, daemon=True).start()  # keep the pipe from filling up
    port, token = int(url.group(1)), url.group(2)
    yield {"tmp": tmp, "pdf": pdf, "port": port, "token": token, "server": server, "env": env}
    server.terminate()
    mock.terminate()


def _up(url):
    try:
        return httpx.get(url, timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def call(app, name, body=None, token=None, status=200):
    r = httpx.post(f"http://127.0.0.1:{app['port']}/api/{name}", json=body or {}, timeout=60,
                   headers={"X-Transcriber-Token": app["token"] if token is None else token})
    assert r.status_code == status, r.text
    assert KEY not in r.text  # no response ever carries the key
    return r.json()


def wait_job(app):
    def finished():
        job = call(app, "job")["job"]
        return job if job["state"] != "running" else None
    return wait_for(finished)


SETTINGS = {"kind": "printed", "language": "English", "model": "flash"}


def test_1_page_and_guards(app):
    base = f"http://127.0.0.1:{app['port']}"
    page = httpx.get(base + "/")
    assert page.status_code == 200 and "<title>Transcriber</title>" in page.text
    # another site pointing its own name at this computer still sends its own Host
    assert httpx.get(base + "/", headers={"Host": f"evil.example:{app['port']}"}).status_code == 403
    call(app, "status", token="", status=403)
    call(app, "status", token="wrong", status=403)
    assert httpx.get(base + "/api/status").status_code == 404  # the API is POST only


def test_2_sign_in(app):
    status = call(app, "status")
    assert status["account"]["signed_in"] is False and status["job"] is None
    call(app, "login", {"limit": "nonsense"}, status=400)
    url = call(app, "login", {"limit": "5"})["url"]
    assert "limit=5" in url and f"callback_url=http%3A%2F%2Flocalhost%3A{app['port']}%2Fcallback" in url
    consent = httpx.get(url, follow_redirects=False)  # the mock approves at once
    callback = consent.headers["location"].replace("localhost", "127.0.0.1")
    back = httpx.get(callback, follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/#signed-in"
    again = httpx.get(callback)  # a code and state are single-use
    assert "did not finish" in again.text
    status = call(app, "status")
    assert status["account"]["signed_in"] is True and status["account"]["label"] == "sk-or-v1-moc...000"
    assert status["account"]["limit"] == 10


def test_3_document_and_estimates(app):
    call(app, "document", {"path": str(app["tmp"] / "missing.pdf")}, status=400)
    doc = call(app, "document", {"path": f"'{app['pdf']}'"})  # pasted with quotes
    assert doc["pages"] == 4 and doc["done"] == [] and doc["previous"] is None
    assert doc["preview"].startswith("data:image/jpeg;base64,")
    assert doc["output"] == str(app["pdf"].parent / "output")
    pilot = call(app, "estimate", {"path": str(app["pdf"]), "settings": SETTINGS, "mode": "pilot"})
    assert pilot["pages"] == 3 and pilot["pilot_pages"] == 3 and pilot["cost"] > 0
    assert pilot["fallback_model"] == B
    assert call(app, "check-estimate", {"path": str(app["pdf"])}) == {"ready": False}
    call(app, "check", {"path": str(app["pdf"])}, status=400)


def test_4_pilot(app):
    job = call(app, "start", {"path": str(app["pdf"]), "settings": SETTINGS, "mode": "pilot"})["job"]
    assert job["kind"] == "pilot" and job["total"] == 3
    job = wait_job(app)
    assert job["state"] == "done" and job["done"] == 3 and job["result"]["done"] == 3, job
    assert any("page 1 ok" in line for line in job["lines"])
    out = app["pdf"].parent / "output"
    assert (out / "letters" / "pages" / "page_0003.md").exists() and not (out / "letters" / "pages" / "page_0004.md").exists()
    page = call(app, "page", {"path": str(app["pdf"]), "page": 2})
    assert "Pembertom" in page["text"] and page["model"] == A and page["image"].startswith("data:image/jpeg")
    doc = call(app, "document", {"path": str(app["pdf"])})
    assert doc["done"] == [1, 2, 3]
    assert doc["previous"] == {"kind": "printed", "language": "English", "lines": False, "diplomatic": False,
                               "faint": False, "model": A}


def test_5_changed_settings_redo_every_page(app):
    changed = {**SETTINGS, "language": "German"}
    e = call(app, "estimate", {"path": str(app["pdf"]), "settings": changed, "mode": "all"})
    assert e["mismatch"] is True and e["pages"] == 4
    same = call(app, "estimate", {"path": str(app["pdf"]), "settings": SETTINGS, "mode": "all"})
    assert same["mismatch"] is False and same["pages"] == 1


def test_6_full_run_and_check(app):
    call(app, "start", {"path": str(app["pdf"]), "settings": SETTINGS, "mode": "all"})
    job = wait_job(app)
    assert job["state"] == "done" and job["total"] == 1 and job["result"]["done"] == 4, job
    md = (app["pdf"].parent / "output" / "letters.md").read_text()
    assert "<!-- page 4 -->" in md and "postscript" in md
    e = call(app, "check-estimate", {"path": str(app["pdf"])})
    assert e["ready"] and e["model"] == B and e["checked_pages"] == 4 and e["pages"] == 4
    call(app, "check", {"path": str(app["pdf"])})
    job = wait_job(app)
    assert job["state"] == "done" and job["result"]["flagged"] >= 1 and job["result"]["pages"] == 4, job
    review = app["pdf"].parent / "output" / "letters.review.html"
    assert review.exists() and '"opts": ["Pembertom", "Pemberton"]' in review.read_text()
    assert (app["pdf"].parent / "output-b" / "letters" / "pages" / "page_0004.md").exists()
    assert call(app, "document", {"path": str(app["pdf"])})["review"] is True


def test_7_opens_only_what_it_made(app):
    call(app, "open", {"path": str(app["pdf"]), "what": "something-else"}, status=400)
    call(app, "open", {"path": str(app["tmp"] / "missing.pdf"), "what": "text"}, status=400)


def test_8_native_keys_are_saved_but_never_shown(app):
    call(app, "key", {"provider": "openai", "key": "has spaces"}, status=400)
    call(app, "key", {"provider": "nonsense", "key": "x"}, status=400)
    r = httpx.post(f"http://127.0.0.1:{app['port']}/api/key", json={"provider": "openai", "key": "sk-native-secret-1"},
                   headers={"X-Transcriber-Token": app["token"]})
    assert r.status_code == 200 and "sk-native-secret" not in r.text
    status = httpx.post(f"http://127.0.0.1:{app['port']}/api/status", json={},
                        headers={"X-Transcriber-Token": app["token"]}).text
    assert "sk-native-secret" not in status and json.loads(status)["native"]["openai"]["saved"] is True
    call(app, "key", {"provider": "openai", "remove": True})
    assert call(app, "status")["native"]["openai"]["saved"] is False


def test_9_native_model_without_key_is_explained(app):
    r = call(app, "start", {"path": str(app["pdf"]), "settings": {**SETTINGS, "model": "openai:gpt-6-luna"},
                            "mode": "pilot"}, status=400)
    assert "OpenAI API key" in r["error"]
    r = call(app, "start", {"path": str(app["pdf"]), "settings": {**SETTINGS, "model": "openai:gpt-6-luna",
                                                                   "private": True}, "mode": "pilot"}, status=400)
    assert "privacy option" in r["error"]


def test_10_sign_out_then_quit(app):
    call(app, "logout")
    assert call(app, "status")["account"]["signed_in"] is False
    r = call(app, "start", {"path": str(app["pdf"]), "settings": SETTINGS, "mode": "pilot"}, status=400)
    assert "Sign in" in r["error"]
    call(app, "quit")
    assert app["server"].wait(timeout=10) == 0


def test_stop_lets_running_pages_finish_and_starts_no_more(monkeypatch, tmp_path):
    pdf = tmp_path / "five.pdf"
    doc = pymupdf.open()
    for i in range(5):
        doc.new_page().insert_text((72, 100), f"page {i}", fontsize=12)
    doc.save(pdf)
    stop = threading.Event()
    calls = []

    def fake_page(src, index, s, credentials, log):
        calls.append(index)
        stop.set()  # someone presses Stop while the first page is being read
        return transcribe.PageResult(index=index, text=f"text {index}", model=s.model, attempts=1, seconds=0.0)

    monkeypatch.setattr(transcribe, "transcribe_page", fake_page)
    s = transcribe.Settings(model="m", profile="printed", language=None, layout="paragraphs", diplomatic=False,
                            dpi=72, workers=1, retries=1, timeout=5, deny_data_collection=False)
    lines = []
    done, failed = transcribe.run([Source(pdf)], s, tmp_path / "out", {"openrouter": "test"},
                                  listener=lines.append, stop=stop)
    assert len(calls) <= 2 and done == len(calls) and failed == 5 - len(calls)
    assert any("stopping" in line for line in lines)
    assert all("[" not in line or "[/" not in line for line in lines)  # listeners get plain text
