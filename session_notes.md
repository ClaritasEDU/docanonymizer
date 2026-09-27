# Session Notes: Doc Anonymizer

This file is a running log of Claude Code sessions. Append after every significant session. Do not edit previous entries.

---

## Session 001 - 2026-05-01

**Type:** Requirements and planning (Claude Chat - not yet in Claude Code)

### What Was Done

Conceptualized and fully specced a local document anonymizer tool from scratch. The idea originated as a browser-based artifact using the public Anthropic API, but the privacy requirement was identified immediately - the original document cannot touch any public LLM. The design pivoted to a local Flask app using Ollama or any compatible local LLM.

**PRD written and iterated through three versions:**

v1.0 - Initial spec covering PDF/XLSX/DOCX/CSV, Ollama as sole LLM backend, basic PII scrubbing with per-type placeholders.

v1.1 - Expanded to support any local LLM endpoint, not just Ollama. Added multi-endpoint manager with nicknames, health checks, OpenAI-compatible API style (covering LM Studio, llama.cpp, Jan, Koboldcpp, LocalAI). Added non-local endpoint guard.

v1.2 - Expanded file type support to full matrix: PDF, XLSX/XLS, CSV, DOCX/DOC, PPTX/PPT, ODT/ODS/ODP, TXT/RTF/HTML. Tiered output (format-preserving vs text-only) documented per format. LibreOffice dependency added for legacy and ODF formats.

**Key design decisions made:**

- Identifier format: `[TAG_XXXX]` where XXXX is 4-character uppercase hex (0000-FFFF = 65,536 permutations per type)
- Entity linking: same hex suffix across all PII types for the same real-world person (`[PERSON_3A4F]`, `[EMAIL_3A4F]`, `[PHONE_3A4F]`)
- Replacement order: sort by string length descending to prevent partial match collisions
- Key file stores full replacement map and entity registry for reversibility
- Key file schema includes endpoint URL and api_style so the record is self-documenting
- No database, no auth, no external dependencies of any kind
- Temp files deleted immediately after processing, not on a schedule
- Logging must never capture raw document text or PII values - metadata only

**Companion files created:**
- `doc-anonymizer-prd.md` - full product requirements document (v1.2)
- `CLAUDE.md` - workstyle instructions and project rules for Claude Code
- `business_spec.md` - rationale and scope summary
- `session_notes.md` - this file

### Open Questions (Unresolved)

1. Auto-delete policy for `/uploads` and `/output` directories - leave to user or auto-purge after N hours?
2. XLSX formula cells containing PII - scrub, skip, or warn only?
3. Key file encryption at rest - AES password protection as optional feature?
4. LLM timeout mid-chunk - resume from last completed chunk or restart?
5. OpenAI-compatible endpoints vary on whether `model` field is required - Claude Code should make the model field configurable per endpoint (already in schema)

### Next Steps

- Stand up Flask skeleton with endpoint manager and health check
- Build file extraction layer (pdfplumber, python-docx, openpyxl, python-pptx)
- Build LLM adapter layer (ollama + openai styles behind single `llm_call()` function)
- Build entity registry and replacement map logic
- Build anonymize pipeline end-to-end with XLSX as first test format
- Build unanonymize pipeline
- UI: single HTML page per spec in PRD Section 12
- Integration test: full round-trip anonymize + unanonymize on a real donor list

---

<!-- Append new sessions below this line -->

---

## Session 001 Addendum - 2026-05-01 (continued)

**Design direction finalized:**

- Full light and dark mode support via CSS custom properties and `prefers-color-scheme`, with manual toggle persisted to `localStorage`
- Terminal-esque aesthetic throughout - monospace font for entire UI (not just log panel), black and white color system, ASCII-style progress indicators (`[ ]`, `[>]`, `[x]`, `[!]`), no rounded buttons, no decorative elements
- Hard rule: buttons must always contrast against their surface. Primary buttons use solid fill. Disabled states are explicitly muted. Secondary and destructive buttons follow defined border/text patterns.
- Full CSS token spec added to PRD Section 12.2 with exact hex values for both modes
- Button contract, layout, accessibility requirements, and WCAG AA contrast mandate documented in PRD Section 12.3-12.5 and CLAUDE.md

PRD bumped to v1.2 final. All four project files updated. Ready for Claude Code.


---

## Session 001 Addendum 2 - 2026-05-01 (continued)

**Scope additions finalized for v1.3:**

PII types expanded from 6 to 17. Added: FINANCIAL, DOB, SID, IP, USERNAME, GRADE, MEDICAL, IMMIGRATION, DEMO, RELIGION, GENDER. Organized into Standard and Sensitive tiers (visual grouping only - no functional difference). SANITIZE ALL master toggle added as the default on-load state.

Mandatory pre-scrub preview panel added. User reviews all detected PII spans highlighted inline with proposed placeholders. Can deselect false positives. Must click CONFIRM AND SCRUB to proceed. No skip path exists.

Deep scrub guarantee added (Section 5.9). The output file is always freshly constructed - never a modified copy of the original binary. DOCX: walks every XML file in the ZIP archive, strips tracked changes (w:ins/w:del), clears comments.xml, zeros author metadata in docProps. XLSX: same ZIP walk, clears cell comments, zeros author metadata. PDF: already clean .txt output in v1. Regex net added as secondary scanner: email, phone, SSN, IP, credit card patterns.

Post-scrub verification pass added (Section 5.10). Re-extracts all text from output file, runs replacement map in reverse plus regex patterns. Download and GitHub push are disabled until verification passes. Result logged as pass/fail with match types only - matched values never logged.

GitHub repository push added (Section 4.10). User configures named connections (owner/repo, branch, PAT). After a verified scrub, user can push anonymized file to any configured repo branch. github.json added to mandatory .gitignore. Only the anonymized output is ever transmitted.

External network policy clarified: LLM endpoints must be localhost or LAN. GitHub API (api.github.com) is the only permitted external call, and only on explicit user action.

Two open questions added: (5) verification false positive whitelist workflow; (6) manual span tagging in preview panel.

PRD is v1.3 final. All files updated. Spec is complete and ready for Claude Code.

---

## Session 002 - 2026-05-01 (Claude Code, branch `claude/implement-screens-design-Wvu9O`)

**Type:** Implementation - first code session. Materialized the screens design from the Claude Design handoff bundle.

### What Was Built

`screens.html` at the repo root. A single static page that renders all six artboards from the design handoff (Anonymize happy path: Empty -> File loaded -> Detecting -> Preview -> Scrubbing -> Verified) at 1100px artboard width with macOS browser chrome around each. The dark mode terminal aesthetic is applied throughout, drawing color tokens from PRD 12.2 and the button/typography rules from CLAUDE.md and PRD 12.3-12.4.

The original handoff prototype was React + UMD CDN. That stack violates CLAUDE.md's rule "Single HTML page, vanilla JS, no external CSS frameworks or CDNs," so it was rewritten in vanilla HTML/CSS/JS (no React, no Babel, no CDN). Visual output is reproduced; the prototype's pan/zoom design canvas chrome (rename, drag-reorder, focus mode) was intentionally omitted - those are design tool affordances, not part of the app.

### Decisions and Assumptions

- **Artboard sizing.** Prototype hardcoded each artboard to 1100x780 with internally scrolling content. For a static page where focus mode does not exist, content cropping is a real UX bug. Switched to `min-height: 780px; height: auto` so each card grows to fit its content. Width stays at 1100px to preserve the design intent.
- **Status bar layout.** The status bar's content (endpoint label + dropdown + 3 secondary buttons) does not fit in 760px even with shrunk paddings. Allowed the status bar to wrap to 2 rows via `flex-wrap: wrap`; flagged below as something to revisit.
- **`[DARK]` toggle placement.** Prototype puts it in the status bar; PRD 12.4 says it goes in the header bar. Followed the prototype since the user's instruction was to implement the design. The PRD-vs-prototype divergence is noted; pick a side when the real toggle is wired up.
- **Browser chrome inclusion.** Kept the chrome wrapper from the prototype because the design intentionally frames each screen as a localhost browser session. This is design language, not a real implementation detail; the eventual Flask app's HTML will not include chrome.

### Bugs Found and Fixed (this session)

Comprehensive headless test pass with Playwright revealed six bugs across the implementation. All fixed before commit.

1. **Critical** - status dot collapsed to 0 width. Default flex `min-width: auto` shrank the 10x10 dot to invisible. Fix: `flex-shrink: 0` on `.da-dot`.
2. **High** - "Ollama (local)" wrapped to two lines under flex squeeze. Fix: `white-space: nowrap` on status-bar text spans plus `flex-shrink: 0` on all status-bar children.
3. **High** - status bar buttons overflowed past the right edge of the 760px content area. Fix: reduced sm-button padding (14 -> 10) and font (13 -> 12), added `flex-wrap: wrap` so any remaining overflow falls to a second row inside the status bar background instead of overhanging.
4. **Medium** - all six artboards cropped content at the bottom because the card was a hard 780px and the chrome body had `overflow: hidden`. Fix: `min-height: 780px; height: auto` on `.dc-card`, `overflow: visible` and flex chain on the chrome body so the wrapper grows with its child.
5. **Low** - results panel had multi-line vertical gaps between rows. Cause: `white-space: pre` on `.da-results` rendered the template literal's formatting whitespace between sibling divs as visible blank lines. Fix: scoped `white-space: pre` to direct child rows only.
6. **Low** - missing focus states (PRD 12.3 mandates `outline: 2px solid var(--border-focus)`); buttons defaulted to `type=submit`. Fix: added `:focus-visible` rules; added `type="button"` to every button.

### Tests Run

- Playwright headless render of all six artboards in Chromium, full-page screenshots to `/tmp/screen-{1..6}.png`. Manual visual review against the prototype.
- Per-screen structural assertions (artboard count, status bar present, tabs present, primary/disabled button counts, progress steps, log lines, PII rows, placeholders, verified line text).
- Console error / page error capture: zero errors after fixes.
- WCAG AA contrast spot-check: all measured text/background pairs at 5.92:1 or higher; primary buttons and active tabs at 21:1 (well past AAA).
- Keyboard focus check: first Tab focuses the endpoint select with the spec'd 2px white outline.
- Disabled-button click test: disabled `[ DETECT PII ]` correctly blocks click events.

### Open Issues / Known Limitations

- Status bar wraps to a second row at 760px width. Acceptable for a design mock; if/when the real header is built, either trim the bar's content or move `[ DARK ]` to the header per PRD 12.4.
- The screens are static art: no upload, detection, scrub, or unanonymize behavior is wired. This is intentional - the design canvas is a visual deliverable for the operator to react to before implementation begins.
- Filename cleanup: the `(1)` artifact from the original upload was dropped from `CLAUDE.md`, `business_spec.md`, and `session_notes.md` later in this same session.

### Next Steps

- Operator review of `screens.html` to confirm the screen flow and visual direction before any backend work begins.
- Once approved, scaffold the Flask skeleton from PRD 11 and wire the screens into Jinja templates (or keep them as a static reference and rebuild equivalent markup behind the Flask routes).
- Decide the `[ DARK ]` toggle placement and reconcile the prototype/PRD divergence.

---

## Session 003 - 2026-05-01 (Claude Code, branch `claude/implement-screens-design-Wvu9O`)

**Type:** Implementation - full v1.3 build per PRD.

### What Was Built

Complete Flask app with vanilla-JS frontend implementing the entire PRD. 12 Python modules under `app/`, single-page UI under `templates/` and `static/`, runtime data dirs `uploads/`, `output/`, `keys/`. Top-level entrypoint `run.py`.

**Python modules (`app/`):**

| Module | Responsibility |
|---|---|
| `config.py`              | env-driven config; `DOCANON_ROOT` override for tests; runtime data paths separated from source-tree paths so Flask can always find templates/static |
| `logging_setup.py`       | structured ISO-Z / level / module logger writing to `anonymizer.log` and stdout |
| `endpoints.py`           | LLM endpoint manager: persisted to `endpoints.json`, RFC1918/loopback validation guard, parallel health checks |
| `llm.py`                 | adapter contract `llm_call(prompt, endpoint=...)` for `ollama` and `openai` API styles |
| `extractors.py`          | per-format text extraction (PDF, DOCX, XLSX, CSV, PPTX, ODT/ODS/ODP via LibreOffice, TXT/RTF/HTML); returns format-aware `ExtractResult` |
| `chunker.py`             | tiktoken-aware chunking with 200-token overlap; chars-fallback when tiktoken is missing |
| `mapper.py`              | `EntityRegistry`: 4-char hex IDs, entity-linking via `linked_to`, longest-first replacement map for collision-free substitution |
| `detector.py`            | LLM detection orchestrator; tolerant JSON-array parser handles fences and prose; per-chunk error recovery |
| `scrubber.py`            | surface-replace per format + ZIP-level deep scrub: tracked changes (`<w:ins>`/`<w:del>`) stripped, `word/comments.xml` cleared, `xl/comments*.xml` dropped, `dc:creator` / `cp:lastModifiedBy` zeroed, alt-text descr stripped, raw-XML map substitution across every part |
| `verifier.py`            | post-scrub re-extraction + map-reverse + regex net (EMAIL/PHONE/SSN/IP/CREDIT_CARD); strips placeholders before regex to avoid false positives |
| `key_files.py`           | `*.key.json` save/load per PRD 5.7 schema |
| `unanonymize.py`         | reverses the replacement map (longest-first) and routes through the same writers |
| `github_mgr.py`          | connection storage in `github.json` (PAT redacted in API), `repos/{owner}/{name}` test, `PUT contents` push with auto-fetched SHA |
| `pipeline.py`            | session orchestrator: extract -> detect -> preview -> confirm -> scrub -> verify; runs detection / scrub on background threads so HTTP returns immediately |
| `server.py`              | Flask routes per the API plan in the file's docstring |

**Frontend:**

- `templates/index.html` - single page, no CDN, no framework. Sections: header, status bar, endpoint manager panel, GitHub manager panel, mode tabs, file drop zone, PII grid, detect button, detection progress, preview, scrub progress, results, GitHub push panel, log panel.
- `static/styles.css` - dark + light tokens via `prefers-color-scheme` plus a manual `[data-theme]` override that persists to `localStorage`.
- `static/app.js` - vanilla JS: state object, fetch wrapper, theme toggle, endpoint CRUD + health, GitHub CRUD + test, drop-zone + change handler, PII grid renderer, detection polling, preview placeholder rendering with click-to-deselect, scrub polling, results table, GitHub push panel, log tail.

### Decisions and Assumptions

- **Per-process in-memory sessions.** Single-user local tool; no need for Redis or DB. `pipeline._SESSIONS` is a dict guarded by a lock. Sessions disappear on restart. Outputs stay in `output/`; key files in `keys/`.
- **Background threads for long ops.** Upload, detection, and scrub are launched on `threading.Thread` so the HTTP request returns instantly. The frontend polls `/status` and `/results` until done. This avoids long-held connections for 30s+ LLM calls.
- **Open Question 1 (auto-cleanup of `/uploads` and `/output`).** Decided: `/uploads/` is purged immediately after the session completes (or is cancelled). `/output/` is kept for the user to manage manually - it's their anonymized work product.
- **Open Question 2 (XLSX formulas containing PII).** Decided: flag and warn, do not silently rewrite. The scrubber surfaces `formula_warnings` to the UI so the operator can review.
- **Open Question 4 (LLM timeout mid-chunk).** Decided: log and skip the failed chunk, continue with the rest. The verifier catches anything missed. A more aggressive resume policy can come in v2.
- **Deep scrub ordering.** The deep-scrub ZIP walk now applies the raw map substitution **before** the targeted handlers (metadata-zero, comments-clear). This is defense-in-depth: even if a placeholder lands in `<dc:creator>` via the raw pass, the explicit handler still empties it.
- **Per-row `white-space: pre` for results.** Same lesson as the screens.html session: scoped to row divs to keep template-literal whitespace from rendering as blank lines.
- **Theme toggle.** Honors `prefers-color-scheme` until the user explicitly clicks `[ DARK ]` / `[ LIGHT ]`, at which point `data-theme` on `<html>` wins and persists to `localStorage`.
- **Module names.** `app.server` (not `app.app`) for the Flask app so the entrypoint reads `python run.py` cleanly without shadowing the package name.

### Bugs Found and Fixed (this session)

Discovered via the test suite + headless UI rendering:

1. **High - XLSX deep-scrub broke OOXML namespace.** The `<dc:creator>` zeroing handler self-closed the tag, dropping its `xmlns:dc` attribute, which made openpyxl reject the file on reload. Fix: keep the original opening tag (with all attributes) and empty only the inner text via a backref-replace.
2. **High - test isolation broke template loading.** `DOCANON_ROOT` was overriding `TEMPLATES_DIR`, so under tests Flask couldn't find `templates/index.html`. Fix: split `SOURCE_ROOT` (always anchored to `app/__init__.py`'s parent) from runtime `ROOT`. Templates and static now use `SOURCE_ROOT`.
3. **Medium - drop zone collapsed to inline width.** `<label>` is inline by default. Added `display: block` to `.dropzone`.
4. **Medium - DETECT button lost the file reference after innerHTML rewrite.** Captured the chosen file in `state.anonFile` so the button click path reads from state, not the (now destroyed) DOM input. Same pattern for unanon zones via `state.unanon`.
5. **Low - PII grid rendered alphabetically.** Server was passing `sorted(VALID_TAGS)`. Added `mapper.TAG_ORDER` (the spec's order) and routed the index template + detector prompt through it.
6. **Low - metadata-zero handler ran before raw substitution.** That left "Jane Smith" in `<dc:creator>` because the regex didn't tolerate `xmlns:dc`. Reordered: raw substitution first, then targeted handlers (defense in depth) - and the regex was made permissive.

### Tests Run

`pytest tests/` - **58/58 passing** in ~2 seconds. Suite mocks the LLM (no network, no local Ollama required) and isolates each test against a fresh `DOCANON_ROOT` temp directory.

Coverage areas:

- `test_mapper.py` (10) - hex uniqueness, entity linking by hex / by text, longest-first sort, drop, invalid tag rejection, merge_chunks input sanitization, 500-entity stress.
- `test_endpoints.py` (8) - default state, CRUD, validation, RFC1918 / loopback / link-local detection, atomic write semantics, unreachable-endpoint health check.
- `test_extractors.py` (7) - txt, csv, html (script + style + tags stripped), xlsx, docx (header/footer/tables walked), pptx (speaker notes captured), unsupported-suffix rejection.
- `test_scrubber_verifier.py` (10) - text / csv / xlsx / docx surface replace, deep-scrub raw-XML pass, metadata zeroing, tracked-changes (`<w:ins>`/`<w:del>`) stripping with manual XML injection, formula warnings, verifier pass / map-residue fail / regex-residue fail / placeholder-immunity.
- `test_unanonymize.py` (3) - text + xlsx round-trip; longest-first reversal collision test.
- `test_detector.py` (6) - JSON extraction (direct / fenced / prose / empty), full detection with mocked llm_call, error-recovery on failing chunk.
- `test_server.py` (12) - health, endpoint CRUD via API, invalid-payload rejection, local-URL check, index page render, unsupported-upload rejection, full upload -> detect -> confirm -> verify -> download flow with mocked LLM, unanonymize via API, GitHub PAT redaction, **download blocked when verification fails**.
- `test_privacy.py` (2) - **end-to-end pipeline run + log scan: zero PII values appear anywhere in `anonymizer.log`**; mapper logger-level scan.

Plus a Playwright headless run against the live Flask server: status bar populated, 17 PII rows in spec order, 6 sensitive flags, SANITIZE-ALL toggle locks/unlocks individual checkboxes, theme toggle round-trips, manage panels open/close, tabs switch, file upload enables DETECT button, endpoint add round-trips through the API. **Zero console errors.**

### Open Issues / Known Limitations

- The detector's prompt is well-instructed but model quality varies. Real-world testing against Llama 3.2, Mistral 7B, and Phi-3 should follow before the operator runs production data through it. Consider a small built-in prompt-tuning panel in v2.
- PDF and PPTX output are text-only in v1 (per PRD 8). DOCX / XLSX preserve formatting fully.
- Open Questions 3, 5, 6 from PRD remain open - key file encryption, regex-whitelist after a verify miss, and manual span tagging in the preview. None are blockers for v1.
- The frontend log panel polls only on user action (refresh / after each pipeline transition). A streaming server-sent events upgrade would be nicer in v2.
- Status bar still wraps to two rows under 760px. Same call as Session 002: acceptable for now, revisit when the visual design lands.

### Next Steps

- Operator runs the app against a real local LLM (Ollama/Llama 3.2) on a sample donor list and validates the detection quality.
- Add a small fixture corpus to `tests/` (sanitized DOCX with tracked changes, sample XLSX with formulas, PDF with text) so the test suite exercises real document shapes, not only synthesized ones.
- Wire up the `[ HOOKS ]` for resolving Open Questions 3 / 5 / 6 if the operator wants them in v2.

---

## Session 004 - 2026-05-01 (Claude Code, branch `claude/implement-screens-design-Wvu9O`)

**Type:** Feature add - on-screen text output for copy / paste.

### What Was Built

A new `[ VIEW ON SCREEN ]` action in the Results panel. After a verified anonymize run, the operator can open an inline read-only textarea showing the anonymized text and `[ COPY ALL ]` it to the clipboard. The downloadable file and key file are still produced and verified - the screen view is an additional output that supports the most common downstream workflow (paste clean text into a chat, an email, or a public LLM) without the round trip through Downloads / a viewer app.

### How It Works

- `app/pipeline.py::anonymized_text(sess)` re-extracts the text from the verified output. For text-native formats (`.txt` / `.csv` / `.html` / `.rtf`) it reads the file directly; for binary formats (`.xlsx` / `.docx`) it routes through the existing extractor so the operator gets the readable text content.
- `GET /api/anonymize/<sid>/text` returns `{text, char_count, filename}`. Same verification gate as the file download (`403` until `verify_result.passed`).
- `templates/index.html` adds a `block-screen-text` panel with a `<textarea>`, `[ COPY ALL ]`, `[ HIDE ]`, and a metadata hint.
- `static/app.js` wires the buttons. Copy uses `navigator.clipboard.writeText` with an `execCommand("copy")` fallback for offline / non-secure contexts. Visual feedback flips the button label to `[ COPIED ]` for 1.2s on success.
- `static/styles.css` styles the textarea against the same dark/light tokens.

### Decisions

- **File still produced.** The screen view is additive, not a replacement. The deep-scrub guarantee (PRD 5.9) is anchored to a freshly built file, and the verifier reads from that file. Skipping file generation would weaken the guarantee. So the file is always built, verified, and offered for download; the screen view re-extracts from it.
- **Re-extraction at view time.** The text returned to the screen comes from the *verified output file*, not from an in-memory transformation. That means whatever the operator copies is exactly what would be in the downloaded file - same source of truth.
- **Verification gate enforced on the new endpoint.** The text endpoint refuses (403) until the verification pass succeeds, identical to the download endpoint. No bypass.
- **Privacy.** No new external traffic. The text never leaves localhost (and even then, only inside the user's own browser session). The endpoint never logs document text - only `char_count` is logged via the wider request log.

### Tests Run

`pytest tests/` - **61/61 passing**. Three new server tests:

1. `test_anonymize_view_text_returns_scrubbed_content` - end-to-end: upload txt, detect, scrub, verify, hit `/text`, assert PII string is gone and a `[PERSON_…]` placeholder is present.
2. `test_anonymize_view_text_blocked_until_verified` - force a verify failure, hit `/text`, assert HTTP 403.
3. `test_anonymize_view_text_xlsx_reextracts_cells` - XLSX upload + scrub + view: assert the text view contains placeholders (re-extracted from the binary output).

Plus a Playwright live UI smoke run with a stand-in mock Ollama on port 11434 (a tiny `http.server` returning the canned LLM response). End-to-end: upload, detect, confirm, verify, click `[ VIEW ON SCREEN ]`, read the textarea value (`Hello [PERSON_EB17] - have a good day`), click `[ COPY ALL ]` and verify the button flips to `[ COPIED ]`, click `[ HIDE ]` and verify the panel collapses. Zero console errors.

### Open Issues

- The clipboard API requires a secure context in some browsers when served over HTTP. We have an `execCommand` fallback but Safari / older browsers may need an HTTPS reverse proxy (out of scope for v1 - this is a localhost dev tool).
- The textarea has no character ceiling; very large outputs (50,000+ chars) will render fine but feel sluggish on copy. If that becomes a real issue, paginate or virtualize. Not seen in v1 testing.

### Next Steps

- Operator's call: should there be an option to *only* render to screen and skip the file output entirely? The trade-off is the deep-scrub / verification guarantee. Current call is "always build and verify a file even if the operator only wants screen text" because skipping it would mean inventing a parallel verification path. Easy to reconsider if the operator wants pure-text-only as a faster path for trusted small docs.

---

## 2026-07-17 - Deep code review + web edition (Netlify)

### What was built or changed

1. **`CODE_REVIEW.md`** - full line-by-line review of the v1.3 codebase.
   Three critical findings, all confirmed with repro scripts:
   - C1: verifier regex false positives (any 10-digit number, any dotted-quad)
     permanently block clean output with no override path
   - C2: a failed LLM chunk is silently skipped and detection reports complete -
     PII in that chunk ships unscrubbed if it has no rigid format
   - C3: PII split across DOCX runs ("Ja" + "ne Smith") survives both scrub
     passes; the verifier catches it but the user gets a dead end
   Plus high findings on temp-file hygiene (uploads survive failures,
   LibreOffice conversions leak to /tmp), server-side endpoint locality not
   enforced, and XLSX formula results invisible to the verifier. Two of these
   contradict what business_spec.md claims - flagged as spec drift in the doc.
   Existing suite still 61/61 passing.

2. **`web/index.html`** - the web edition. One self-contained file, no
   frameworks, no CDNs, no build step. Runs entirely in the browser tab:
   - Anonymize: drop .txt/.md/.csv/.docx/.xlsx, pick PII categories (9 regex
     detectors + custom terms box), preview with click-to-reject, then three
     outputs: anonymized file, human-readable decoder ring .txt, and a
     key.json that is schema-compatible with the local app's key files
   - Deanonymize: drop the anonymized doc + either the key.json or the
     decoder ring .txt, get the restored file
   - Own minimal ZIP reader/writer (browser-native DecompressionStream, STORE
     output) so DOCX/XLSX need no libraries
   - DOCX replacement is paragraph-aware across runs - it handles the
     split-run case the local app fails on (C3). Worth porting back.
   - Verification gates downloads: map residue is a hard block, generic regex
     residue is a warning only (the C1 lesson applied)

3. **`netlify.toml`** - publishes `web/`, sets CSP with connect-src 'none' so
   the deployed page cannot make network calls even in principle.

4. README: web edition section with deploy instructions. Business spec:
   web edition scope added.

### Decisions made

- **Web edition is 100% client-side.** A server-based deploy would mean
  uploading documents to Netlify, which the privacy mandate forbids. Static
  hosting + in-browser processing keeps the guarantee: the document never
  leaves the user's device, enforced by CSP rather than promised by policy.
- **Regex detection instead of LLM in the browser.** No local LLM is reachable
  from a hosted page. Compensated with the custom-terms box (deterministic
  catches for known names) and the mandatory preview.
- **Decoder ring is a separate human-readable .txt** in addition to key.json,
  per the product request. Both round-trip; the deanonymizer accepts either.
- **Web verification only hard-fails on map residue.** Generic pattern hits
  are surfaced as warnings, so clean documents with invoice numbers or version
  strings are not dead-ended (the C1 bug avoided by design).

### Tests run

- Backend: pytest 61/61 passing (unchanged).
- Web: Playwright against real Chromium - 30 checks passing. TXT round trip
  byte-identical via both key.json and decoder ring; DOCX with split runs,
  header, table, and bold formatting anonymized, validated by python-docx,
  restored text identical to original; XLSX cells replaced and restored,
  validated by openpyxl; false-positive guards verified (invoice number and
  version string untouched); web key.json loads under the backend's
  load_key_file schema.

### Open issues / known limitations

- Web PERSON/ADDRESS detection is heuristic. It will miss unusual names and
  flag some capitalized phrases - the preview and custom terms are the
  mitigations. Not a substitute for the local LLM path on sensitive docs.
- Web edition does not read PDFs (no in-browser extractor without a heavy
  library). PDF users should use the local app.
- XLSX numeric-typed cells (an SSN stored as a raw number) are not detected
  in the web edition; text cells are.
- The three critical backend findings in CODE_REVIEW.md are documented but
  NOT yet fixed - that is the next work item.

### Next steps

1. Fix C1, C2, C3 in the local app (C3 can port the web edition's
   paragraph-aware replacement).
2. H1 temp-file cleanup to match what business_spec.md already promises.
3. Deploy `web/` to Netlify (operator action - see README).

---

## 2026-07-17 (later) - Local app hardened; direction confirmed local-first

Operator direction mid-session - deploy stays local. The local Flask app IS
the browser experience (run it on the Mac, use it at localhost:5000), so all
robustness work landed there. The web/ folder remains in the repo as an
optional extra but nothing depends on deploying it.

### What was built or changed

Every critical and high finding from CODE_REVIEW.md is fixed, plus most
mediums and the low-hanging lows:

- **C1 verifier policy.** Map residue (actual detected PII still present)
  hard-fails and blocks release. Generic regex hits are warnings shown in the
  results panel - clean documents with invoice numbers or version strings are
  no longer dead-ended. Patterns tightened: phones need separators, IP octets
  range-checked and version-like strings skipped, card numbers Luhn-checked.
- **C2 chunk failures.** Each chunk retries 3x with backoff, then the whole
  run aborts with a clear error. No more silent skips. Upload is deleted on
  abort. business_spec decision 3 rewritten to match.
- **C3 split runs.** New run-aware XML replacement pass (ported from the web
  edition) joins each paragraph's text runs, finds matches across run
  boundaries, and writes placeholders back preserving formatting. Applied to
  DOCX body/headers/footers/footnotes/endnotes/comments and XLSX shared
  strings and inline strings.
- **H1 temp hygiene.** cleanup_session_files() runs on every terminal state:
  uploads, LibreOffice conversion tempdirs, staged files, and failed outputs
  (quarantine - a failed-verify output still contains PII) are all deleted.
  Completion signals (error / verify_result) are set only after cleanup so
  the UI never observes a half-cleaned state. Unanonymize cleans its
  conversion dirs too and no longer overwrites earlier restores.
- **H2 endpoint locality.** Server rejects non-local endpoint URLs unless the
  payload carries allow_nonlocal=true; the browser sets that flag only after
  an explicit confirm. Every LLM call to a non-local endpoint logs a loud
  warning.
- **H3 formula blindness.** Verifier now also scans the raw XML of every part
  in DOCX/XLSX outputs (tags stripped), so formula literals, alt text, and
  metadata can never escape the map scan.
- **M1** stable placeholders (first tag wins, extra tags recorded on the
  entity). **M2** preview spans claim non-overlapping regions longest-first.
  **M3** registry listing in prompts capped at 40 entities. **M4** comments
  parts emptied instead of dropped, comment anchors stripped from
  document.xml. **M5** extraction now reads footnotes/endnotes/text boxes, so
  PII there reaches detection. **M6** image-only PDFs error clearly instead
  of releasing an empty "verified" output.
- **Custom terms in the local UI.** Textarea on the anonymize panel, one term
  per line, optional TAG: prefix. Terms present in the document are
  guaranteed catches even if the LLM misses them.
- Frontend: dropzone listener leak fixed (event delegation, bound once),
  endpoint/github rows no longer inject via innerHTML, results panel shows
  verification warnings distinctly from failures.

### Tests run

- Backend suite grown 61 -> 76, all passing across repeated runs. New
  coverage: verifier warning policy + false-positive repro, deep XML formula
  scan, split-run DOCX scrub with formatting check, detector retry + abort,
  temp hygiene on success/failure/cancel, output quarantine, double-confirm
  no-op, custom terms, non-local endpoint guard, stable tags, text box
  extraction + scrub.
- Live browser end-to-end (Playwright + real Flask + mock Ollama): 13 checks
  passing - custom terms flow, no false-positive block, downloads, on-screen
  text, byte-identical unanonymize round trip, uploads/ empty after run,
  non-local endpoint refused after declining the warning. Log verified free
  of PII values afterward.
- Fixed a test-suite flake: pipeline worker threads could outlive their test;
  completion signals now set after cleanup and conftest joins stragglers.

### Open issues

- Sessions never expire from server memory (fine for a single-user local
  tool; restart clears).
- /api/unanonymize/download/<name> serves any file in output/ by name.
- PPTX rebuild and PDF format-preserving output remain v2 scope.

### Next steps

1. Operator validation run on real documents with the local LLM.
2. Consider porting the run-aware pass to PPTX slide XML when PPTX rebuild
   lands in v2.

---

## Session 005 - 2026-07-18 (Claude Code, branch `claude/file-attach-anonymize-bugs-ei75qt`)

**Type:** Bug fixes (local Flask app frontend)

### What Was Done

Fixed two UI bugs reported from live use:

1. **Attach file needed two tries.** The dropzone is a `<label>` wrapping the
   file input, so the browser opens the file picker natively on click - but the
   JS also called `input.click()` on the same click, firing the picker twice.
   Removed the manual click forwarding; one click now opens exactly one picker.
   Confirmed with Playwright against a live server: old code fired 2 picker
   activations per click, fixed code fires 1.

2. **PII type checkboxes showed the not-allowed cursor and could not be
   clicked.** The app started in SANITIZE ALL mode which set `disabled` on every
   checkbox, and the unlock (clicking the SANITIZE ALL button to switch modes)
   was undiscoverable. Reworked the model: checkboxes are always enabled and all
   checked by default. Unchecking any box drops out of sanitize-all
   automatically (button goes outlined); clicking [ SANITIZE ALL ] re-checks
   everything (button goes solid). Removed the dead disabled-cursor CSS and the
   unused refreshDetectButton helper.

### Decisions / Assumptions

- Sensitive-tier rows (GRADE, MEDICAL, etc.) still show [SENSITIVE] with no
  checkbox - they are always scrubbed by design, unchanged.
- The web edition (`web/index.html`) has neither bug (input sits outside the
  dropzone div, checkboxes never disabled) - no changes there.

### Tests Run

- Playwright live-browser checks, 8/8 passing: single picker activation per
  click, file attaches and DETECT enables after one pick, zero disabled
  checkboxes, uncheck/re-check flows, button state, row-label toggling.
- Backend suite: 76 passed.

### Next Steps

1. Operator re-test of the attach and PII selection flow on the Mac.

---

## Session 006 - 2026-08-06 (Claude Code, branch `claude/school-config-files-wvn98e`)

**Type:** Suite-integration review + role assignment (docs only, no code)

### What Was Done

This repo was pulled into the ClaritasEDU cross-suite session (parentpoint /
teacherAIde / chamberlain / beacon) for a final "everything working together"
review, and came out of it with an assigned role: the owner designated Doc
Anonymizer as the deep-scrub applied to school newsletters before they go
into any frontier model outside ParentPoint's own guarded extraction
pipeline.

The review that preceded the ruling was code-level, not doc-level. Verified
directly: the local-only endpoint guard (`app/endpoints.py is_local_url` —
loopback/RFC1918/link-local only, non-local rejected), which is the
make-or-break property for this role (the document must never touch a
hosted model DURING anonymization); the 17-category detection set, which
covers the strict-exclusion classes in parentpoint's
`PROTECTED_DATA_CLASSES.md` boundary map (MEDICAL, GRADE, DOB, SID,
IMMIGRATION, DEMO, RELIGION); the reversible entity-linked key.json
(`unanonymize.py`), which makes a future scrub -> frontier-extract ->
un-scrub pipeline architecturally possible; the post-scrub verification
gate; and the web edition's CSP (`connect-src 'none'`).

### Decisions Recorded (owner)

1. Newsletters -> any frontier model (outside ParentPoint's pipeline): run
   through this tool first.
2. Newsletters -> Beacon: PII may stay intact — newsletters already go out
   to every family, so their content is school-published information. (The
   canonical write-up of both rulings lives in
   `parentpoint/PROTECTED_DATA_CLASSES.md` Part 1.)
3. Whether this tool ever wires INTO ParentPoint's automated pipeline is
   deliberately undecided — parentpoint tracker B50. Its privacy contract
   (local LLM only) means the realistic future host is a school's on-prem
   box, not ParentPoint's cloud functions. Nothing here changes today: the
   tool stays exactly what it is, used manually.

### Changes In This Repo

`business_spec.md` "Who Uses This" gained the suite-role paragraph. No code.

### Next Steps

1. None for this repo — B50 (in parentpoint) is the pointer if the
   pipeline-integration question ever becomes a build.
