# Doc Anonymizer

Local document anonymizer. Strips PII from documents using any local LLM endpoint (Ollama, LM Studio, llama.cpp, or any OpenAI-compatible local server). The original document never leaves the machine.

See `business_spec.md` for the why and `doc-anonymizer-prd.md` for the full spec.

---

## Install (Mac)

```bash
cd ~/doc-anonymizer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional - LibreOffice unlocks `.doc`, `.ppt`, `.odt`, `.ods`, `.odp`:

```bash
brew install --cask libreoffice
```

A local LLM is required. Easiest setup:

```bash
brew install ollama
ollama pull llama3.2
ollama serve
```

---

## Run

```bash
cd ~/doc-anonymizer
source .venv/bin/activate
python run.py
```

Then open `http://localhost:5000`.

---

## The AI round trip

1. **ANONYMIZE tab:** drop a spreadsheet (or any document), click `[ DETECT PII ]`, review the preview, click `[ CONFIRM AND SCRUB ]`.
2. Every name, email, phone, and address is now a unique 12-character identifier like `[PERSON_3A4F9C2B1D0E]`. The verified output appears on screen.
3. Click `[ COPY FOR AI ]` and paste into ChatGPT, Claude, or any AI tool. The copy starts with one sentence asking the AI to keep identifiers exactly as written.
4. **UNANONYMIZE tab:** paste the AI's answer (or drop the file it gave you) and click `[ RESTORE TEXT ]` / `[ RESTORE FILE ]`. Every identifier becomes the real value again.

Notes:

- Leave all keys selected. Identifiers are never reused across files, so restoring against every key is safe, and it means you never have to remember which key goes with which answer.
- The AI can lowercase identifiers, drop the brackets, escape them, relabel them (`[DONOR_...]`), or keep only the hex - all of those restore. Anything the keys can't resolve is listed in red and left as-is, never guessed.
- To restore on another machine, copy the `.key.json` over and use `[ IMPORT KEY FILE ]`.

The first time the app starts, it writes a default `endpoints.json` pointing at `http://localhost:11434` (Ollama). Use the `[ MANAGE ENDPOINTS ]` panel in the UI to add or change endpoints.

---

## Tests

```bash
cd ~/doc-anonymizer
source .venv/bin/activate
python -m pytest tests/ -v
```

The test suite mocks the LLM and runs against an isolated temp directory. No network or local LLM required.

---

## Project layout

```
~/doc-anonymizer/
  run.py                 # entrypoint
  app/
    server.py            # Flask routes
    config.py            # env config
    logging_setup.py     # structured ISO/level/module logging
    endpoints.py         # LLM endpoint manager
    llm.py               # adapter (ollama / openai-compat)
    extractors.py        # per-format text extraction
    chunker.py           # token-aware chunking
    detector.py          # LLM detection orchestrator
    ids.py               # 12-char ID generation + uniqueness ledger
    mapper.py            # entity registry, one unique ID per value, replacement map
    replacer.py          # single-pass replacement engine (used by scrub, verify, preview)
    restorer.py          # tolerant identifier matching for AI output
    scrubber.py          # surface replace + deep ZIP scrub
    verifier.py          # post-scrub verification (map + regex)
    key_files.py         # key file save/load/list/import
    unanonymize.py       # file restore pipeline
    github_mgr.py        # GitHub connections + push
    pipeline.py          # session orchestrator
  static/
    styles.css           # terminal-monochrome tokens (PRD 12.2)
    app.js               # vanilla frontend
  templates/
    index.html           # single-page app shell
  tests/                 # pytest suite (LLM mocked)
  uploads/  output/  keys/   # runtime data (gitignored); keys/ also holds issued_ids.ledger
  endpoints.json  github.json  anonymizer.log   # runtime config + log (gitignored)
```

---

## Privacy contract (non-negotiable)

- The only network traffic permitted is to local LLM endpoints (localhost / RFC1918 / link-local) and, on explicit user action, GitHub.
- Document text and PII values never appear in `anonymizer.log`. Only metadata (counts, types, timings).
- `/uploads/` is purged after each session.
- `github.json` holds the GitHub PAT and is in `.gitignore`. Never commit it.
- Download and GitHub push are blocked until the post-scrub verification pass returns zero residual matches.

---

## Web edition (Netlify)

`web/index.html` is a second, standalone version of the tool - one HTML file
with zero dependencies that runs entirely inside the browser tab. Nothing is
ever uploaded: the "site" is just static code, and the deploy's security policy
(`connect-src 'none'` in `netlify.toml`) makes it technically impossible for
the page to send your document anywhere. Close the tab and nothing remains.

How it differs from the local app:

| | Local app | Web edition |
|---|---|---|
| PII detection | Local LLM (17 categories) | Pattern matching (9 categories) + your custom terms list |
| Formats | pdf docx xlsx csv txt + more | txt md csv docx xlsx |
| Where it runs | Your Mac | Any browser, still 100% on-device |
| Outputs | Anonymized file + key.json | Anonymized file + decoder ring .txt + key.json |

Pattern matching is weaker than an LLM at spotting names, so the web edition
adds a CUSTOM TERMS box - paste the names and organizations you know are in the
document and they are guaranteed to be caught. The preview step still shows
every match for you to confirm or reject before anything is produced.

Key files are interchangeable: a key.json produced by the web edition works in
the local app's unanonymize tab (use `[ IMPORT KEY FILE ]`), and vice versa.
Both editions use the same unique 12-character identifiers and the same
matching and restore rules. The web edition supports the same AI round trip:
`[ COPY FOR AI ]` on the output, and a paste box on the DEANONYMIZE tab that
restores the AI's answer against one or more key files. Its record of issued
IDs lives in the browser's own storage (random IDs only, never document
content).

### Deploy to Netlify (one-time, ~2 minutes)

Option A - drag and drop, no account linking:

1. Go to https://app.netlify.com/drop
2. Drag the `web` folder from this repo onto the page
3. Done. Netlify gives you a URL immediately.

Option B - connect the repo (auto-redeploys when the code changes):

1. Netlify dashboard: Add new site > Import an existing project
2. Pick GitHub and select `christreadaway/docanonymizer`
3. Netlify reads `netlify.toml` automatically (publish dir: `web`, no build command)
4. Deploy

To test locally before deploying, just open the file in a browser:

```bash
cd ~/doc-anonymizer
open web/index.html
```
