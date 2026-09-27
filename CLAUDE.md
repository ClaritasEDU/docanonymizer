# CLAUDE.md - Doc Anonymizer Project

## Who I Am

Product builder, not a coder. I bring requirements and product vision; Claude Code handles implementation. I have 25 years in product development, 4 startups, and currently serve as COO of a Catholic church and school in Austin. I think in systems and outcomes, not syntax.

---

## How to Work With Me

- **Bias toward action.** Don't argue, just build. Politely push back if my thinking needs development, but do it once and move on.
- **Minimize questions.** Make reasonable judgment calls and tell me what you chose. Don't stall waiting for me to decide something you can decide yourself.
- **No filler.** Cut "It is worth noting," "In conclusion," "As previously mentioned," and similar phrases entirely.
- **Direct, honest assessments.** I prefer hard truths over reassuring ones. Don't hedge when I need a definitive answer.
- **When I ask you to debug something, test with ferocity.** Find the actual root of every problem. Don't let me find the same bug twice.
- **Logging is not optional.** Every module ships with structured logging from day one. See Logging Standards below.
- **No shortcuts.** If something needs checking, check it. If a file needs reading, read it. Take the extra step so I don't have to ask twice.

---

## Writing and Communication Style

- No em-dashes. Use a single hyphen sparingly if needed.
- Minimal colons.
- Conversational and direct tone.
- When drafting user-facing text: warm, grounded, no jargon.

---

## Development Workflow

1. Requirements live in `business_spec.md` and `doc-anonymizer-prd.md`
2. Session history lives in `session_notes.md`
3. Code in Claude Code
4. All paths use Mac conventions (`~/doc-anonymizer/` as root)
5. Terminal commands must always start with `cd` to the correct directory - never assume I'm already there
6. Always provide foolproof install and run instructions

---

## Privacy Mandate (Non-Negotiable)

This tool exists specifically to keep sensitive documents off public LLMs. Every architectural decision must reinforce that guarantee.

- **Zero external calls** - the only network traffic allowed is to configured local LLM endpoints
- **Endpoint guard** - warn loudly before saving any non-localhost URL as an endpoint
- **No logging of document content** - log metadata (file name, char count, PII counts) but never log actual text from the document or the PII values themselves
- **Temp file hygiene** - files in `/uploads` must be deleted immediately after processing completes or fails, not on a schedule

---

## Logging Standards (Required in Every Module)

Use Python's `logging` module at `INFO` level by default. `DEBUG` available via `LOG_LEVEL=DEBUG` env var.

Every log entry must include: ISO timestamp, log level, module label in brackets.

```
2026-05-01T14:32:01Z [INFO] [extractor] DOCX extracted: 4,218 chars across 12 paragraphs
2026-05-01T14:32:03Z [INFO] [llm] Endpoint: LM Studio | Chunk 1/3: 312 tokens, 8 PII found, 2.1s
2026-05-01T14:32:05Z [INFO] [mapper] Replacement map: 3 PERSON, 3 EMAIL, 2 PHONE, 1 ADDRESS
```

Required events to log in every run:
- App startup: port, configured endpoints, active endpoint
- File received: name, size, type
- Extraction complete: char count, page or sheet count
- Each LLM chunk call: endpoint nickname, chunk index, token count, response time, PII items found
- Replacement map built: count per PII type, total unique entities
- File written: output path, format, size
- Key file saved: path
- Any error: full traceback
- Unanonymize started: key file loaded, entity count
- Unanonymize complete: output path

**Never log:** raw document text, PII values, or anything from the replacement map values (the original sensitive strings).

---

## Ongoing Documentation Duties

After every significant Claude Code session, append to `session_notes.md`:
- Date
- What was built or changed
- Any decisions made or assumptions taken
- Any open issues or known bugs discovered
- Next steps

After any scope change that affects core product decisions, update `business_spec.md` to reflect the current state of intent. Do not let it drift from what is actually being built.

Both files are living documents. Keep them current. They are the memory of this project.

---

## Tech Stack

- **Backend:** Python 3.11+, Flask
- **LLM integration:** Ollama-style and OpenAI-compatible local endpoints via adapter layer
- **File handling:** pdfplumber, python-docx, openpyxl, python-pptx, LibreOffice (system, optional)
- **Frontend:** Single HTML page, vanilla JS, no external CSS frameworks or CDNs
- **Config:** `.env` for environment, `endpoints.json` for LLM backends, `*.key.json` for anonymization keys
- **No database.** No auth. No cloud dependencies of any kind.

---

## UI Design Rules (Non-Negotiable)

The aesthetic is terminal-esque, black and white. Think `htop` or a monochrome IDE. Functional. No decoration.

**Typography:** Full UI uses monospace only - `ui-monospace, 'Cascadia Code', 'Menlo', monospace`. No mixed font stacks. No external font CDNs.

**Color:** Defined entirely via CSS custom properties. Two complete sets - dark mode (default) and light mode. Switch via `prefers-color-scheme` plus a manual `[DARK]` / `[LIGHT]` toggle that persists to `localStorage`. Full color token spec is in PRD Section 12.2.

**Buttons must never blend into the surface they sit on.** This is a hard rule.
- Primary buttons: solid `--accent-bg` fill, `--accent-fg` text. Always visually dominant.
- Disabled buttons: clearly muted - `--bg-inset` background, `--text-dim` text, `cursor: not-allowed`.
- Secondary buttons: transparent background, `--border` border, visible on all surfaces.
- Destructive buttons: `--danger` border and text.
- No border-radius beyond 2px. No pill buttons.

**Contrast:** All text/background pairs must meet WCAG AA (4.5:1 minimum). Primary button combinations exceed AAA. Verify `--danger` values against both surface colors before shipping.

**Focus states:** `outline: 2px solid var(--border-focus)` on all interactive elements. Never `outline: none`.

**Progress display:** Monospace status characters - `[ ]` pending, `[>]` active, `[x]` done, `[!]` error. No spinners, no progress bars, no animation beyond a blinking cursor on the active step.

**Results display:** Monospace summary table with ASCII divider lines. Numbers right-aligned. Clean, scannable, no decoration.

---

## Key Business Rules (Do Not Break)

1. Every distinct PII value gets its own identifier: `[PERSON_3A4F9C2B1D0E]`, `[EMAIL_7C1B0A94E2D3]`. An identifier is NEVER shared by two values and NEVER reissued - not within a file, not across files (enforced against every key in `/keys` plus `keys/issued_ids.ledger`). This replaced the v1.3 shared-suffix rule on 2026-09-27 (owner decision) because a shared suffix makes AI output ambiguous to restore. Relationships the model reports (Jane's email belongs to Jane) are recorded as `linked_to` in the key file, never by sharing an ID.
2. Identifiers are 12-character uppercase hex with at least one letter and one digit, and never digits-E-digits (Excel scientific notation)
3. Replacement is single-pass, leftmost-longest (longest original wins at a position), and never rewrites inside an existing placeholder. Short numbers (no letters, under 7 digits) match only as whole numbers
4. All replacements must be reversible via the key file - unanonymize must restore exactly, including AI output where the identifiers were lowercased, un-bracketed, markdown-escaped, relabeled, or reduced to the bare hex. Anything that cannot be resolved is reported, never guessed
5. Key file records: session ID, original filename, endpoint used, model used, full replacement map, entity registry
6. Preview is mandatory - user must confirm before any file is written. No skip path.
7. Output file is always freshly constructed - never a modified copy of the original binary
8. Deep scrub covers all hidden layers: tracked changes, comments, author metadata, full ZIP XML walk for DOCX/XLSX
9. Download and GitHub push are disabled until the post-scrub verification pass returns zero residual PII
10. Verification logs pass/fail and match types only - matched PII values are never written to logs
11. github.json must be in .gitignore - verify this before first commit. It contains PATs.
12. GitHub API is the only permitted external network call, and only on explicit user action. LLM endpoints must be localhost or LAN only.
