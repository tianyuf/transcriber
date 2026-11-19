"""The whole workflow against a local mock of OpenRouter: sign in, transcribe, validate, review.

Runs the real CLI in subprocesses. Never touches the real keychain or OpenRouter.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pymupdf
import pytest

import transcriber
from transcriber.sources import Source

ROOT = Path(__file__).resolve().parent.parent
PAGES = [
    "Dear Mrs Hale,\nThank you for your letter of March 3.\nI shall be in Leeds on Tuesday.",
    "The second page of the letter.\nWith kind regards,\nArthur Pemberton",
    "A third page about the harvest accounts.",
]
A, B, C = "qwen/qwen3.7-flash", "google/gemini-3.1-flash-lite", "qwen/qwen3-vl-235b-a22b-instruct"
NATIVE = "gpt-6-luna"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("world")
    pdf = tmp / "letters.pdf"
    doc = pymupdf.open()
    for text in PAGES:
        page = doc.new_page()
        page.insert_text((72, 100), text, fontsize=12)
    doc.save(pdf)
    photos = tmp / "photos"
    photos.mkdir()
    with pymupdf.open(pdf) as d:
        for i in (0, 1):
            d[i].get_pixmap(dpi=100).save(photos / f"IMG_{i + 1}.png")

    def hashes(src):
        return [hashlib.sha256(src.render(i, 150)[0]).hexdigest() for i in range(src.page_count())]

    hp, hi = hashes(Source(pdf)), hashes(Source(photos))
    answers = {A: {}, B: {}, C: {}, NATIVE: {}}
    for h, text in zip(hp, PAGES):
        answers[B][h] = text
        answers[C][h] = text
        answers[A][h] = text
        answers[NATIVE][h] = text
    # A drops a line on page 1 and misreads a name on page 2; B and C read both correctly
    answers[A][hp[0]] = "Dear Mrs Hale,\nI shall be in Leeds on Tuesday."
    answers[A][hp[1]] = "The second page of the letter.\nWith kind regards,\nArthur Pembertom"
    for h, text in zip(hi, PAGES):
        answers[A][h] = text
    (tmp / "answers.json").write_text(json.dumps(answers))

    port = free_port()
    server = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_openrouter.py"), str(port), str(tmp / "answers.json")])
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/v1/models", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
    env.update(OPENROUTER_BASE_URL=f"http://127.0.0.1:{port}", OPENAI_BASE_URL=f"http://127.0.0.1:{port}/v1",
               OPENAI_API_KEY="sk-or-v1-mocktestkey0000000000000000", TRANSCRIBER_NO_KEYRING="1",
               TRANSCRIBER_CONFIG_DIR=str(tmp / "config"), PYTHONIOENCODING="utf-8", COLUMNS="200")
    yield {"tmp": tmp, "pdf": pdf, "photos": photos, "env": env}
    server.terminate()


def cli(world, *args, check=None):
    r = subprocess.run([sys.executable, "-m", "transcriber", *args], cwd=world["tmp"], env=world["env"],
                       capture_output=True, text=True, timeout=120)
    if check is not None:
        assert r.returncode == check, r.stdout + r.stderr
    return r


def test_1_not_signed_in(world):
    r = cli(world, "transcribe", str(world["pdf"]), check=1)
    assert "auth login" in r.stderr


def test_2_login_with_pkce(world):
    p = subprocess.Popen([sys.executable, "-m", "transcriber", "auth", "login", "--no-browser"],
                         cwd=world["tmp"], env=world["env"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    for line in p.stdout:
        if "/auth?" in line:
            url = line.strip()
            break
    assert url and "code_challenge_method=S256" in url and "callback_url=http%3A%2F%2Flocalhost%3A" in url
    page = httpx.get(url, follow_redirects=True, timeout=10)  # the mock approves and redirects to the callback
    assert "Signed in" in page.text
    rest = p.stdout.read()
    assert p.wait(timeout=30) == 0, rest
    assert "sk-or-v1-mocktestkey" not in rest  # the key is never printed
    cred = Path(world["env"]["TRANSCRIBER_CONFIG_DIR"]) / "credentials.json"
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600
    r = cli(world, "auth", "status", check=0)
    assert "sk-or-v1-moc...000" in r.stdout and "mocktestkey" not in r.stdout


def test_3_estimate_sends_nothing(world):
    r = cli(world, "transcribe", str(world["pdf"]), "--estimate", check=0)
    assert "3 page(s) to transcribe" in r.stdout
    assert not (world["tmp"] / "output").exists()


def test_4_transcribe_pdf(world):
    cli(world, "transcribe", str(world["pdf"]), "--language", "English", check=0)
    out = world["tmp"] / "output"
    md = (out / "letters.md").read_text()
    assert md.startswith("---\nsource: \"letters.pdf\"") and "status: unreviewed machine transcription" in md
    assert "<!-- page 3 -->" in md and "Arthur Pembertom" in md
    run = json.loads((out / "letters" / "run.json").read_text())
    assert run["profile"] == "printed" and run["notation"] == "editorial" and run["pages"]["1"]["model"] == A
    r = cli(world, "transcribe", str(world["pdf"]), check=0)  # resuming: nothing left to do
    assert "3 already done" in r.stdout


def test_5_validate_finds_the_dropped_line_and_the_misreading(world):
    r = cli(world, "validate", str(world["pdf"]), check=1)
    assert "possible dropped lines" in r.stdout
    out = world["tmp"] / "output"
    merged = (out / "letters.merged.md").read_text()
    assert "{+B: Thank you for your letter of March 3.}" in merged
    assert "{Pembertom|Pemberton}" in merged
    app = (out / "letters.review.html").read_text()
    assert '"t": "drop"' in app and '"opts": ["Pembertom", "Pemberton"]' in app and 'lang="en"' in app
    assert (world["tmp"] / "output-b" / "letters" / "pages" / "page_0001.md").exists()
    run_b = json.loads((world["tmp"] / "output-b" / "letters" / "run.json").read_text())
    assert run_b["fallback_model"] == "openai/gpt-6-luna"


def test_6_tiebreaker_settles_the_vote(world):
    cli(world, "validate", str(world["pdf"]), "-c", "output-c")
    merged = (world["tmp"] / "output" / "letters.merged.md").read_text()
    assert "{+BC: Thank you for your letter of March 3.}" in merged  # C confirms the dropped line
    assert "Arthur Pemberton" in merged and "{Pembertom|Pemberton}" not in merged  # C sides with B: no marker left


def test_7_folder_of_photos(world):
    cli(world, "transcribe", str(world["photos"]), "-o", "photo-output", check=0)
    md = (world["tmp"] / "photo-output" / "photos.md").read_text()
    assert "<!-- page 2 · IMG_2.png -->" in md and "Arthur Pemberton" in md


def test_8_default_fallback_model(world):
    cli(world, "transcribe", str(world["pdf"]), "-o", "fallback-output", "--pages", "3", "--force",
        "-m", "missing-model", check=0)
    run = json.loads((world["tmp"] / "fallback-output" / "letters" / "run.json").read_text())
    page = run["pages"]["3"]
    assert page["model"] == B and page["fallback_from"] == "missing-model" and page["used_fallback"]
    assert (world["tmp"] / "fallback-output" / "letters" / "pages" / "page_0003.md").read_text().strip() == PAGES[2]


def test_9_native_openai(world):
    cli(world, "transcribe", str(world["pdf"]), "-o", "native-output", "--pages", "1",
        "--model", f"openai:{NATIVE}", "--no-fallback", check=0)
    out = world["tmp"] / "native-output"
    run = json.loads((out / "letters" / "run.json").read_text())
    assert run["pages"]["1"]["model"] == f"openai:{NATIVE}"
    assert (out / "letters" / "pages" / "page_0001.md").read_text().strip() == PAGES[0]


def test_10_python_api(world, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", world["env"]["OPENAI_BASE_URL"])
    monkeypatch.setenv("OPENAI_API_KEY", world["env"]["OPENAI_API_KEY"])
    result = transcriber.transcribe_file(
        world["pdf"], output=world["tmp"] / "api-output", model=f"openai:{NATIVE}", use_fallback=False,
        pages="1",
    )
    assert result.ok and result.pages_done == 1 and result.models == (f"openai:{NATIVE}",)
    assert result.markdown.name == "letters.md" and result.run_log.exists()


def test_11_max_cost_rejects_unknown_native_price(world):
    r = cli(world, "transcribe", str(world["pdf"]), "-o", "uncosted-output",
            "--model", "openai:custom-vision-model", "--max-cost", "1", check=3)
    assert "price unknown" in r.stdout and "cannot enforce --max-cost" in r.stderr
    assert not (world["tmp"] / "uncosted-output").exists()


def test_12_validation_rejects_fallback_same_as_b(world):
    r = cli(world, "validate", str(world["pdf"]), "--fallback-model", "gemini", check=1)
    assert "must differ from model B" in r.stderr


def test_13_logout(world):
    r = cli(world, "auth", "logout", check=0)
    assert "Removed" in r.stdout
    assert not (Path(world["env"]["TRANSCRIBER_CONFIG_DIR"]) / "credentials.json").exists()
