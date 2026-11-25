# transcriber

People using this repository are often researchers, including historians rather than programmers, who want scanned or photographed documents turned into faithful text. Help them do that, not develop the tool, unless they ask for development.

The standalone CLI is the primary interface. Agent integrations are optional; keep operating instructions tool-neutral so the project works with different agents, editors, shells, and automation environments.

- **Usage:** follow the portable workflow in `skills/transcriber/SKILL.md`. It covers signing in, inspecting, piloting, running, validating, models and reporting.
- **Language:** explain things in plain words. Ask before spending more than about a dollar or installing anything.
- **Documents outside this repo:** use their absolute paths and put the output next to them (`-o "/path/to/their/folder/output"`), not inside this repository.
- **API keys:** never read, print or ask for them in chat. OpenRouter OAuth and native hidden-prompt login put credentials in the system keychain or secure fallback file without passing through you.

## Development

- Code is in `transcriber/`, run with `uv run transcriber ...`.
- `api.py` provides the public Python API (`transcribe_file`, `validate_file`) with structured results and `TranscriberError`; test its contract in `tests/test_api.py`. Native credential storage is in `auth.py` and tested in `tests/test_auth.py` without a real keychain.
- `app.py` is the browser app (`transcriber app`, or bare `transcriber` at a terminal): a stdlib HTTP server on 127.0.0.1 with the page inline as `PAGE`, calling the same `transcribe`/`validate`/`auth` code as the CLI. Every API call needs the random token from the opened address and a localhost `Host` header; keep both checks, never send a key back to the page, and only open files the app itself wrote. `Transcriber.command` and `Transcriber.bat` are double-click launchers for it. Test it in `tests/test_app.py` against the OpenRouter mock.
- `transcribe.run` accepts a `listener` (plain-text log lines) and a `stop` event, which the app uses for progress and its Stop button; `compute_estimate` returns the cost estimate as data. When a CLI run needs a missing key at a terminal, `auth.require_key`/`require_native_key` sign in on the spot (`auth.AUTO_LOGIN`, set only by the CLI commands).
- Tests: `uv run pytest`. They run offline against `tests/mock_openrouter.py`, which mocks OpenRouter including the OAuth flow and a native Responses endpoint, and never touch the real keychain.
- `providers.py` routes OpenRouter and native Gemini, xAI, OpenAI, and Anthropic calls. Keep provider request/response tests in `tests/test_providers.py` offline with `httpx.MockTransport`; never make live paid calls in tests.
- Per-page fallback lives in `transcribe.py` and is enabled by default. `providers.py` chooses a distinct non-Chinese OpenRouter model family unless the user overrides it or passes `--no-fallback`. The page's actual model and fallback source must remain in `run.json`; test recovery behavior in `tests/test_fallback.py`.
- The prompt is assembled in `profiles.py`. Any change to it changes the `prompt_id` recorded with every page, which is intended: a transcription stays tied to the exact prompt that produced it.
- `compare.py` aligns transcriptions by token: one CJK character, or one word in other scripts. Add a test in `tests/test_compare.py` for any change in how markers are placed.
- `skills/transcriber/SKILL.md` is the canonical agent workflow. Discovery symlinks are provided under `.agents/skills/`, `.opencode/skills/`, and `.claude/skills/`; keep them pointed at the canonical skill rather than copying its contents.
- `plugin.json` is the portable Agent Plugins manifest. `.claude-plugin/` is the Claude Code marketplace adapter, which Grok can also read through its Claude compatibility support. OpenCode uses skill discovery rather than the Claude plugin manifest. Do not assume plugin manifests or installation commands are interchangeable across hosts.
