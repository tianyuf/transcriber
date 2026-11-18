# archivist-transcribe

Faithful, checkable transcriptions of scanned archival documents (PDFs or photos taken in an archive) in any language or script, printed, typed or handwritten.

Each page image goes to a vision model on OpenRouter with a strict "faithful transcriber" prompt. A second, independent model checks the result. You then settle every disagreement in a proofreading app, with the scan beside the text.

It is made to be used through [Claude Code](https://claude.com/claude-code): give Claude this repository and your documents, and it handles the commands.

## Using it with Claude Code

**Option 1: open the repository.** Clone it and start Claude Code in the folder:

```sh
git clone https://github.com/tianyuf/archivist-transcribe
cd archivist-transcribe
claude
```

Then ask in your own words, for example: *"Transcribe the PDFs in ~/Research/Wiener letters. They're typed English letters from the 1940s."*

**Option 2: install it as a plugin**, so it works in any folder. In Claude Code:

```
/plugin marketplace add tianyuf/archivist-transcribe
/plugin install archivist-transcribe@archivist-transcribe
```

Either way, Claude will:

1. ask whether you are allowed to upload the pages (many archives restrict this);
2. have you sign in to OpenRouter in your browser, once per computer;
3. look at a few pages to choose settings;
4. transcribe a short pilot and show you how it reads;
5. tell you the cost before the full run;
6. offer to check the result with a second model and give you the proofreading app.

## What it costs

You pay OpenRouter directly for what you use. With the recommended model, **Qwen 3.7 Flash**, a typical page costs a small fraction of a cent, so a 300-page file comes to well under a dollar. Checking with a second model costs more, typically 2 to 10 times the transcription. The tool prints an estimate before every run and the actual charge afterwards. You can cap spending when you sign in (`archivist auth login --limit 10`) or per run (`--max-cost 2`).

## Privacy and permissions

Pages are sent to OpenRouter and, through it, to the model's provider; Qwen 3.7 Flash is served by Alibaba Cloud.

- **Check your terms.** Check your archive's terms of use for reproductions before uploading.
- **Restrict providers.** `--deny-data-collection` limits a run to providers that do not store or train on your data.
- **Your key stays local.** Your OpenRouter key is stored in your operating system's keychain (macOS Keychain, Windows Credential Manager, or Secret Service on Linux). It is never written into your project folders and never shown to Claude.

## Using it directly

You need [uv](https://docs.astral.sh/uv/). It installs everything else on first use.

```sh
uv run archivist auth login                      # sign in to OpenRouter in the browser (once)
uv run archivist inspect letters.pdf --sample 1-3
uv run archivist transcribe letters.pdf --language English
uv run archivist validate letters.pdf            # second model + proofreading app
uv run archivist models                          # recommended models and current prices
```

A source can be a PDF, a single image, or a folder of photos (one photo per page, in natural filename order). Useful options for `transcribe`:

| option | for |
|---|---|
| `--profile printed` (default) / `handwritten` / `cjk` | printed or typed material in any language; manuscripts; Chinese, Japanese or Korean |
| `--language "German (Fraktur)"` | tells the model what to expect; always helps |
| `--notation cjk` | CJK markers (`□` `〔字〕`) with another profile, e.g. for Chinese manuscripts |
| `--layout lines` | keep every line break, for verse, registers and ledgers |
| `--diplomatic` | keep long s, ligatures and abbreviation marks as written |
| `--pages 1-3` | a pilot, or redoing particular pages (with `--force`) |
| `--dpi 200` | small or faint print (PDFs) |
| `-m 30b` | another model; see `archivist models` |
| `--estimate`, `--max-cost N` | know the cost before anything is sent |

Runs are resumable: every page is saved the moment it is done, so after an interruption you just rerun the same command.

## Output

```
output/letters.md                  all pages, with front matter recording source, models, prompt and dates
output/letters/pages/page_0001.md  one file per page
output/letters/run.json            which model and prompt produced each page, and when
output/letters.review.html         after validate: the proofreading app
output/letters.merged.md           after validate: the text with review markers
```

Review markers (editorial notation; the CJK notation is shown in brackets):

| marker | meaning |
|---|---|
| `[illegible]` (`□`) | the model could not read a word (character) |
| `[word?]` (`〔字〕`) | the model's best guess |
| `{A\|B}` (`〔A\|B〕`) | the two models read it differently |
| `{+B: …}` (`【补B:…】`) | a line only the second model saw; the first probably skipped it |

In the proofreading app you settle each marker by clicking a reading or typing a correction, then export the final Markdown. Your decisions are saved in the browser as you go, and can be exported and imported to continue on another computer.

## Why two models

There is no answer key for a scan, so the check is agreement between independent readers. In testing on archival material, one model occasionally dropped a whole line without any sign, while a stronger one tended to "correct" the source by modernizing spellings and fixing typists' errors. A faithful model as A and an independent model as B catch both kinds of error. A third model (`validate -c output-c`) can break ties on the pages that disagree.

## Citing

Cite the original document. Where a methods note is appropriate, say that it was transcribed with archivist-transcribe, giving the models and dates from the output's front matter, and whether you proofread it against the scans. Unreviewed output is a machine transcription. Check it against the page images before quoting.

## Development

```sh
uv run pytest     # offline: tests run against a mock of OpenRouter, including the sign-in flow
```

See `CLAUDE.md` for how the code is organized.

## Licence

MIT; see `LICENSE`. It depends on PyMuPDF, which is AGPL-licensed: fine for running the tool yourself, but check its terms before offering it as a hosted service.
