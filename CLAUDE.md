# archivist-transcribe

Whoever opened this repository in Claude Code is most likely a researcher (often a historian, not a programmer) who wants scanned or photographed documents turned into faithful text. Help them do that, not develop the tool, unless they ask for development.

- **Usage:** follow the skill in `skills/archivist-transcribe/SKILL.md`. It also loads automatically from `.claude/skills/`. It covers signing in, inspecting, piloting, running, validating, models and reporting.
- **Language:** explain things in plain words. Ask before uploading anyone's documents, before spending more than about a dollar, and before installing anything.
- **Documents outside this repo:** use their absolute paths and put the output next to them (`-o "/path/to/their/folder/output"`), not inside this repository.
- **The API key:** never read, print or ask for it. `archivist auth login` puts it in the system keychain without passing through you.

## Development

- Code is in `archivist_transcribe/`, run with `uv run archivist ...`.
- Tests: `uv run pytest`. They run offline against `tests/mock_openrouter.py`, which mocks OpenRouter including the OAuth flow, and never touch the real keychain.
- The prompt is assembled in `profiles.py`. Any change to it changes the `prompt_id` recorded with every page, which is intended: a transcription stays tied to the exact prompt that produced it.
- `compare.py` aligns transcriptions by token: one CJK character, or one word in other scripts. Add a test in `tests/test_compare.py` for any change in how markers are placed.
