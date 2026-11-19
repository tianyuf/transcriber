---
name: transcriber
description: Transcribe scanned archival documents into faithful, checkable text with the transcriber tool. Covers PDFs of scans and photos taken in an archive, in any language or script, printed, typed, mimeographed or handwritten (letters, diaries, notebooks, newspapers, and government files), and checking a transcription against a second model with a page-by-page proofreading app. Use it whenever someone wants text out of scanned or photographed documents, wants old OCR redone, wants a document made searchable, or asks whether a transcription can be trusted, even if they never say "transcribe" or "OCR". Not for born-digital PDFs whose text layer is already clean.
---

# transcriber

A command-line tool for historians, run with `uv`. It does five things:

- turns each page into an image and sends it to a vision model through OpenRouter or a native provider API with a strict "faithful transcriber" prompt;
- saves each page as soon as it is done, so an interrupted run resumes where it stopped;
- records which model and prompt produced each page;
- optionally checks the text against a second, independent model;
- builds a proofreading app where every doubtful reading is a decision taken against the scan.

## Find the tool

The tool's root is the nearest folder above this skill's base directory that contains a `pyproject.toml` with `name = "transcriber"`. Call it ROOT and run every command as:

```sh
uv run --project "ROOT" transcriber <command> ...
```

Run from the user's own working folder, so that relative paths and the `output/` folder land next to their documents. Quote every path: archive filenames often contain spaces, parentheses and non-Latin characters.

If `uv` is missing, ask the user before installing it. The installer is `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS and Linux, and `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` on Windows. The first `uv run` installs the Python dependencies by itself.

## Use from other programs

Python programs can embed the synchronous public API:

```python
import transcriber

result = transcriber.transcribe_file("letters.pdf", language="English")
validation = transcriber.validate_file("letters.pdf")
```

The result objects contain output paths, page/failure counts, models, and flagged-page counts. Options cover native models, fallback, page selection, workers, retries, DPI, cost limits, privacy, and credentials. `TranscriberError` replaces CLI `SystemExit` failures for library callers. Other languages can invoke the CLI and consume its Markdown, JSON, and HTML outputs.

## Workflow

### 1. Privacy and security

Pages go to the selected model provider. OpenRouter forwards pages to the provider serving the chosen model; the recommended Qwen 3.7 Flash is served by Alibaba Cloud. Native provider:model selections bypass OpenRouter and call that provider directly.

- **Offer `--deny-data-collection`.** It restricts the run to providers that do not store or train on the data.
- **Never read the key.** Do not read, print or ask for the API key, and never ask the user to paste a key into the chat.

### 2. Sign in (once per computer)

```sh
uv run --project "ROOT" transcriber auth status
```

If not signed in, run `transcriber auth login` in the foreground with a timeout of at least 300 seconds. Tell the user that a browser window will open. There they sign in to OpenRouter (or create a free account and add credit) and click to approve. The key goes straight into the system keychain; it never passes through you.

- **Spending cap:** suggest `--limit 10` (USD) to anyone new to this.
- **Remote machine (SSH):** use `--headless`. The user opens the printed address on any device and pastes back the code it shows. That code is single-use and expires in 10 minutes; it is not the key.

Native providers use API keys instead of OpenRouter OAuth. `transcriber auth login --provider openai` (or `grok`, `gemini`, `anthropic`) prompts with hidden input and stores the key in the system keychain or secure fallback file. `auth status --provider NAME` reports availability without calling the provider; `auth logout --provider NAME` removes the stored key. Environment variables remain supported and take precedence. Never read or print a key.

### 3. Look before spending

```sh
uv run --project "ROOT" transcriber inspect "letters.pdf" --sample 1,12,40
```

This reports the page count, whether a PDF already has a usable text layer, and a cost estimate, and saves the sampled pages as JPEGs under `inspect/`. Look at those images yourself (Read them) to decide:

| What you see | Options |
|---|---|
| Printed or typed text, carbon copies, forms, newspapers (any language) | `--profile printed` (default) |
| Handwriting: letters, diaries, notebooks, annotations | `--profile handwritten` |
| Chinese, Japanese or Korean print, typescript, 油印 | `--profile cjk` |
| Handwritten Chinese, Japanese or Korean | `--profile handwritten --notation cjk` |
| Known language or script (always helps) | `--language "German (Fraktur)"`, `"Ottoman Turkish, Arabic script"`, `"French and Latin"` |
| Poetry, registers, ledgers, or the user wants line-for-line output | `--layout lines` |
| The user wants long s, ligatures and abbreviation marks kept | `--diplomatic` |
| Small print, footnotes, faint mimeograph | `--dpi 200` |

If the text layer looks clean, say so. Transcription may be unnecessary (`transcriber inspect FILE --show-text`).

### 4. Pilot on a few pages

```sh
uv run --project "ROOT" transcriber transcribe "letters.pdf" --pages 1-3 --language "English"
```

Compare each pilot page with its scan. Read `output/<name>/pages/page_0001.md` next to the sample image, and check for dropped lines, "corrected" spellings, translated passages, and misread names or numbers. Show the user a short excerpt beside your assessment, not whole pages. Adjust the profile, language or dpi and rerun those pages with `--force` before going on.

### 5. The full run

```sh
uv run --project "ROOT" transcriber transcribe "letters.pdf" --language "English" --estimate
```

State the estimated cost. Go ahead without asking when it is a few cents; ask first when it is more than about a dollar, or more than the user indicated. `--max-cost N` makes the tool refuse to start above N USD.

For more than about 10 pages, run in the background with output redirected to a log file, and tell the user where it is. Pages already done are skipped, so after failures or an interruption, run the same command again.

### 6. Check it

```sh
uv run --project "ROOT" transcriber validate "letters.pdf"
```

This transcribes the pages again with an independent model B into `output-b/`, the same prompt and settings but a different model family (Gemini 3.1 Flash-Lite, or Qwen 2.5 VL 72B for CJK). It then aligns the two versions word by word (character by character for CJK) and writes into `output/`:

- `<name>.review.html`, the proofreading app: the scan beside the text, with every disagreement as a clickable choice. Decisions are saved in the browser; "Export final Markdown" writes the result. **This is the deliverable for human review.** Give the user its path and ask them to open it in a browser.
- `<name>.merged.md`: A's text with the markers, for people who prefer a text editor.
- `<name>.comparison.html`: flagged pages, worst first.

Validation costs more than the transcription itself; say so before running it. Add `-c output-c` to bring in a tiebreaker (Qwen3 VL 235B) on flagged pages only: wherever it sides with A or B, the marker disappears, which leaves fewer decisions for the user. Exit code 1 means some pages were flagged; that is the normal outcome, not an error.

Why two models: one model can drop a whole line without a trace, while another may quietly "correct" the source. Never use the same model as A and B; a model tends to agree with itself.

## Models

**Recommend `qwen/qwen3.7-flash` (the default).** It is fast and very inexpensive (a fraction of a cent per page), and good on print and typescript in most languages. Use it unless there is a reason not to:

- **It refuses pages** (often politically sensitive material, because the provider applies content rules). Use `--fallback-model 30b` to recover failed pages automatically, or redo just those pages with `-m 30b --pages N --force`. The run log records which model produced each page.
- **Hard handwriting.** Still start with flash, then validate. If B disagrees a lot, look at a few pages yourself and tell the user plainly how reliable the result is.

`transcriber models` lists aliases and prices. OpenRouter aliases include `gemini`, `grok`, `openai`, and `anthropic`; any other OpenRouter model with image input also works with `-m`.

Native APIs use `provider:model`: `gemini:gemini-3.1-flash-lite`, `grok:grok-4.3`, `openai:gpt-6-luna`, or `anthropic:claude-haiku-5-5`. They accept `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `XAI_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` respectively, or keys stored with `transcriber auth login --provider PROVIDER`. Environment variables take precedence. Never read or print these values. `--deny-data-collection` cannot be enforced on native APIs and rejects them; use OpenRouter when that option is required.

Automatic per-page recovery is enabled by default. It uses a non-Chinese model family on OpenRouter, normally Google Gemini; if that duplicates the primary, Transcriber selects a distinct OpenAI, Anthropic, or xAI model. No fallback option is needed. Override it with `--fallback-model MODEL`, or disable it with `--no-fallback`. Each model gets up to the configured retries. The fallback also handles empty, truncated, repetitive, malformed, and explicitly refused output. `run.json` records the actual model and fallback source. Online validation selects a fallback for its B/C runs too; offline validation does not. A native primary still defaults to an OpenRouter fallback, so use `--no-fallback` for native-only credentials or choose a native fallback explicitly. Estimates include worst-case fallback use; retries cost extra.

## Review markers

Everything in a marker means "check this against the scan". Never silently resolve a marker yourself; that is the user's call, made in the review app.

| editorial (default) | cjk | meaning | written by |
|---|---|---|---|
| `[illegible]` | `□` | unreadable word (character) | model |
| `[word?]` | `〔字〕` | best guess, unsure | model |
| `{A\|B}` | `〔A\|B〕` | the models read it differently (A\|B\|C with a tiebreaker) | validate |
| `{A\|}` `{\|B}` | `〔A\|〕` `〔\|B〕` | a short span only one model has | validate |
| `{+B: …}` | `【补B:…】` | a line only B saw: A probably dropped it (`+BC` when C saw it too) | validate |

Handwriting profile only: `~~struck~~` for deletions, `{^added}` for insertions, `[other hand:]` for a different hand.

## Output

```
output/<name>.md                    all pages, with YAML front matter: source, models, prompt id, dates, status
output/<name>/pages/page_0001.md    one file per page, written as it finishes
output/<name>/run.json              per-page provenance: actual model, fallback source, prompt id, dpi, time, attempts
```

For a folder of photos, `<name>` is the folder name, pages follow natural filename order (IMG_2 before IMG_10), and each page records its photo's filename.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Not signed in" / OpenRouter HTTP 401 | `transcriber auth login` |
| A native model requires a credential | Set its environment variable or run `transcriber auth login --provider NAME`; `transcriber models` lists the names. Do not print the value. |
| HTTP 402 | Out of credit or over the key's limit: https://openrouter.ai/settings/credits |
| HTTP 404 "no provider" | Wrong model id, or a privacy setting excludes every provider of this model. Try without `--deny-data-collection`, or another model. |
| Many HTTP 429 | Rate limited: lower `--workers` to 2. If every page fails with 429, the model is overloaded upstream; switch model rather than raising retries. |
| A page says it was "cut off" | The model hit its output limit. Rerun that page (`--pages N --force`), or use `--dpi 200`. |

## Reporting back

- **The numbers:** pages done, failed and blank, fallbacks used, OpenRouter charges and native token usage, and the output paths.
- **The quality:** a short excerpt of one page next to your honest view of it.
- **The status:** this is a machine transcription, unreviewed until the user has gone through the review app.
- **Never** paste a whole transcription into the chat, and never print the API key or the contents of the credentials file.
