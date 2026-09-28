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
ollama pull llama3.1:8b
ollama serve
```

**Which model.** Measured on a messy 25-row pledge sheet (names, emails,
phones, addresses, plus spouses and phone numbers inside free-text notes):

| Model | PII caught | Notes |
|---|---|---|
| llama3.2 (3B) alone | 91 of 112 | before the app's safety layers |
| llama3.2 + app safety layers | 111 of 112 | missed one spouse name in a note |
| llama3.1:8b + app safety layers | 112 of 112 | about 3x slower than llama3.2 |

Use `llama3.1:8b` for anything sensitive (needs about 5 GB of free memory).
`llama3.2` is faster and fine for simple lists, but always read the preview
for names inside free-text notes. To switch models: `[ MANAGE ENDPOINTS ]` >
`[ EDIT ]` > MODEL `llama3.1:8b` > `[ SAVE ]`. A brand-new install picks it up
from `.env` instead: `DEFAULT_MODEL=llama3.1:8b`.

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

## Community ids for spreadsheets (Family Graph)

Rosters and parishioner lists can carry one lifelong id per person (`[I…]`, I plus 16 hex) and per household (`[F…]`, F plus 16 hex), the same id in every file, every year, and every product. Family Graph issues and remembers those ids. Doc Anonymizer never mints one, it asks Family Graph.

This layer applies only to spreadsheets (xlsx, xls, ods, csv) and only when Family Graph is configured. PDFs, DOCX, PPTX, scans, and text files make zero Family Graph calls and are anonymized exactly as before.

**Connect it (one time).** Family Graph runs on the same machine or inside the firewall. Issue a key that carries only the `roster` scope. On a hand-built box:

```bash
cd ~/familygraph
node bin/family-graph.js issue-key docanonymizer roster
```

On the managed Spark server (Family Graph runs in Kubernetes there), run it inside the Family Graph pod, as root on the server:

```bash
k3s kubectl -n familygraph exec -it deploy/familygraph -- node bin/family-graph.js issue-key docanonymizer roster
```

Then in Doc Anonymizer click `[ FAMILY GRAPH: OFF ]`, enter the URL (`http://127.0.0.1:3500` when Family Graph is on the same machine, `http://[box-ip]:30500` for the managed Spark server), paste the key, pick the roster type (SCHOOL, PARISH, OTHER), `[ TEST ]`, `[ SAVE ]`. Settings live in `familygraph.json` (0600, gitignored). The key is never shown again or logged. A non-local URL is refused, with no override, because the call carries the whole roster. Changing the URL requires re-entering the key.

Doc Anonymizer always listens on 127.0.0.1:5000 and refuses any other Host name (a LAN name or IP in the browser gets a 403).

- **Managed Spark server.** Doc Anonymizer is not deployed there; only Family Graph and MissionIQ are. Run Doc Anonymizer on your Mac with a local model (Ollama) and point its Family Graph URL at `http://[box-ip]:30500`. That roster traffic crosses the school network as plain HTTP, so use it only from a staff network. The server's own AI model is not reachable from the LAN at a school site, by design.
- **Hand-built box** with both apps side by side. Reach Doc Anonymizer from your Mac through an SSH tunnel (`ssh -L 5000:127.0.0.1:5000 [user]@[spark-host]`, then open http://127.0.0.1:5000).

**What happens on a roster.**

1. After detection, the rows go to Family Graph as a dry-run plan. Nothing is written there.
2. The preview shows `COMMUNITY IDS` counts plus a card for each person or household the rules could not settle: `[ SAME PERSON ]` / `[ NEW PERSON ]` / `[ NOT A PERSON ]` (households get SAME / NEW HOUSEHOLD). `[ CONFIRM AND SCRUB ]` stays disabled until every `[?]` is answered.
3. `[ CONTINUE WITHOUT COMMUNITY IDS ]` is an explicit choice. If Family Graph is unreachable the app says so and never silently falls back.
4. On confirm, the commit goes to Family Graph first, then the scrub runs. Name cells become `[I…]`, the household column becomes `[F…]` (or a FAMILY_ID column is appended and removed again on restore). Surnames, free-text mentions, emails, phones, and addresses keep their per-value tokens.
5. The key file records the identity registry, every rewritten cell, and the Family Graph source, so restoring the file puts back exactly what was there and restoring an AI answer turns `[I…]` back into the name.

A one-column sheet is sent only when its header is a name header (Name, Student Name, Full Name), and every person on it becomes a review item.

**Retry safety.** Every commit carries an idempotency key. If the outcome is unknown (timeout, dropped connection, 5xx) the file is frozen: decisions cannot change, and the only ways on are `[ CONFIRM AND SCRUB ]` again, which resends the identical request (Family Graph replays the stored result, nothing is written twice), or `[ CANCEL ]`. If the Family Graph URL or key changed while frozen, the resend is not sent. Decisions Family Graph reports as stale are dropped and the preview says why.

**Timeouts** (seconds, in `.env`): `FAMILYGRAPH_TIMEOUT_S` for quick calls (default 10), `FAMILYGRAPH_ROSTER_TIMEOUT_S` for plan and commit (default 600). A 2,000-row roster planned in about 9 s in testing (longer on a small machine), and Family Graph holds its other requests until it finishes.

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
    backstop.py          # deterministic detection after the LLM (patterns + recall)
    ids.py               # 12-char ID generation + uniqueness ledger
    mapper.py            # entity registry, one unique ID per value, replacement map
    replacer.py          # single-pass replacement engine (used by scrub, verify, preview)
    restorer.py          # tolerant identifier matching for AI output
    scrubber.py          # surface replace + deep ZIP scrub
    verifier.py          # post-scrub verification (map + regex)
    key_files.py         # key file save/load/list/import
    unanonymize.py       # file restore pipeline
    github_mgr.py        # GitHub connections + push
    familygraph.py       # Family Graph client (settings, plan, commit, lookup)
    community.py         # community ids on spreadsheets (review items, [I…]/[F…] cells)
    pipeline.py          # session orchestrator
  static/
    styles.css           # terminal-monochrome tokens (PRD 12.2)
    app.js               # vanilla frontend
  templates/
    index.html           # single-page app shell
  tests/                 # pytest suite (LLM mocked)
  uploads/  output/  keys/   # runtime data (gitignored); keys/ also holds issued_ids.ledger
  endpoints.json  github.json  familygraph.json  anonymizer.log   # runtime config + log (gitignored)
```

---

## Privacy contract (non-negotiable)

- The only network traffic permitted is to local LLM endpoints (localhost / RFC1918 / link-local), for spreadsheets a local or LAN Family Graph (no override, proxy settings ignored), and, on explicit user action, GitHub.
- Document text and PII values never appear in `anonymizer.log`. Only metadata (counts, types, timings). Community ids, names, and candidate data are never logged either.
- Files in `/uploads/` are deleted as soon as processing finishes or fails, and any leftovers are cleared at startup.
- The app listens on 127.0.0.1 only. Requests with a foreign Host header are refused (DNS rebinding), and every state-changing request must come from the app's own page.
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
