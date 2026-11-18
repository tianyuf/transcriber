"""A stand-in for OpenRouter, so the tests run offline and cost nothing.

Implements the endpoints the tool uses: the model list, chat completions (answers come from
a JSON file mapping model -> sha256 of the page image -> text), the OAuth consent page
(approves immediately and redirects to the callback), the key exchange (checks PKCE),
and the current-key endpoint.

    python tests/mock_openrouter.py PORT ANSWERS.json
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

KEY = "sk-or-v1-mocktestkey0000000000000000"
CODES: dict[str, str] = {}  # code -> code_challenge


def make_handler(answers: dict):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, status: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/v1/models":
                return self.send(200, {"data": [
                    {"id": m, "pricing": {"prompt": "0.0000001", "completion": "0.0000004"}} for m in answers]})
            if u.path == "/auth":  # the consent page: approve at once
                code = f"code{len(CODES)}"
                CODES[code] = q["code_challenge"]
                back = {"code": code}
                if "state" in q:
                    back["state"] = q["state"]
                self.send_response(302)
                self.send_header("Location", q["callback_url"] + "?" + urlencode(back))
                self.end_headers()
                return
            if u.path == "/api/v1/key":
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    return self.send(401, {"error": {"message": "bad key"}})
                return self.send(200, {"data": {"label": "sk-or-v1-moc...000", "usage": 0.0123,
                                                "limit": 10, "limit_remaining": 9.9877}})
            self.send(404, {"error": {"message": "not found"}})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/v1/auth/keys":
                challenge = CODES.pop(body.get("code"), None)
                digest = hashlib.sha256(body.get("code_verifier", "").encode()).digest()
                if challenge is None or base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge:
                    return self.send(403, {"error": {"message": "Invalid code or code_verifier"}})
                return self.send(200, {"key": KEY, "user_id": "user_mock"})
            if self.path == "/api/v1/chat/completions":
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    return self.send(401, {"error": {"message": "bad key"}})
                url = body["messages"][0]["content"][1]["image_url"]["url"]
                image = base64.b64decode(url.split(",", 1)[1])
                text = answers.get(body["model"], {}).get(hashlib.sha256(image).hexdigest())
                if text is None:
                    return self.send(404, {"error": {"message": f"no mock answer for {body['model']}"}})
                return self.send(200, {
                    "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1500, "completion_tokens": 300, "cost": 0.0003}})
            self.send(404, {"error": {"message": "not found"}})

    return H


if __name__ == "__main__":
    port, answers_file = int(sys.argv[1]), sys.argv[2]
    with open(answers_file, encoding="utf-8") as f:
        answers = json.load(f)
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(answers)).serve_forever()
