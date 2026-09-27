# PRD: Local Document Anonymizer
**Version:** 1.4 (2026-09-27: unique 12-char identifiers, AI round trip - sections 4.5, 5.1-5.3, 5.5, 5.7, 5.8)  
**Status:** Ready for Build  
**Target:** Claude Code

---

## 1. What This Is

A locally-hosted web application that strips PII from documents using any local LLM endpoint — no document content ever reaches a public LLM or external server. The user can configure and switch between multiple locally-running LLM backends (Ollama, LM Studio, llama.cpp server, or any OpenAI-compatible local endpoint). It also maintains a reversible key file so anonymized documents can be exactly restored.

---

## 2. Who It's For

A single user (or small team) who needs to sanitize sensitive documents before sharing them with public AI tools, external analysts, or third parties — while preserving the ability to reconnect anonymized output back to original identities.

---

## 3. User Stories

- As a user, I want to drop in a PDF, Word doc, Excel, or CSV file and receive a clean copy with all PII replaced by consistent, labeled placeholders.
- As a user, I want every name, email, phone, and address to get its own unique identifier that is never reused, so nothing can be mixed up when I restore.
- As a user, I want to paste the anonymized output into any AI tool, bring the AI's answer back, and have every identifier turned back into the real value - even if the AI changed the brackets, case, or label.
- As a user, I want to download the anonymized file in its original format (not converted to plain text).
- As a user, I want to save a local key file so I can unanonymize a document at any time without depending on any external service.
- As a user, I want to configure multiple local LLM endpoints and choose which one to use for each session.
- As a user, I want the app to confirm my selected LLM is reachable before I process anything.
- As a user, I want a clear processing log I can review or copy for debugging.

---

## 4. Core Features

### 4.1 File Upload
- Drag-and-drop or click-to-browse
- Accepted formats (priority order):
  - **Tier 1 (full format-preserving output):** PDF, XLSX, XLS, CSV
  - **Tier 2 (full format-preserving output):** DOCX, DOC
  - **Tier 3 (text extraction + plain text output):** PPTX, PPT, ODT, ODS, ODP, TXT, RTF, HTML
- Max file size: 50MB (configurable via env var)
- File is written to a local `/uploads` temp directory, processed, then deleted
- UI clearly indicates output format for each file type before processing (e.g., "PPTX - text output only")

### 4.2 PII Type Selection

A `[ SANITIZE ALL ]` master toggle sits above the individual checkboxes. When active, all types are selected and the individual checkboxes are visually locked (still visible, all checked, not independently togglable). Clicking `[ SANITIZE ALL ]` again deactivates it and restores individual control. Default state on first load: SANITIZE ALL active.

Individual PII categories (all checked by default, independently togglable when SANITIZE ALL is off):

| Label | Tag Prefix | Description | Examples |
|---|---|---|---|
| Full Names | PERSON | Personal full names, first-only if contextually clear | Jane Smith, Dr. Torres, Coach Mike |
| Email Addresses | EMAIL | Any email address | jane@school.org |
| Phone Numbers | PHONE | Any phone format, including extensions | (512) 555-0198, +1-800-555-0100 x4 |
| Physical Addresses | ADDRESS | Street, city, state, zip, unit numbers | 123 Main St, Austin TX 78701 |
| SSN / Tax IDs | ID | SSN, EIN, ITIN, passport numbers | 123-45-6789 |
| Organizations | ORG | Company, school, church, nonprofit names | St. Theresa School, Acme Corp |
| Financial Data | FINANCIAL | Account numbers, routing numbers, credit card numbers | Acct #4482, routing 021000021 |
| Dates of Birth | DOB | Full or partial birthdates | 03/14/1992, born March 1992 |
| Student / Employee IDs | SID | Any internal ID number tied to a person | Student ID 40021, Badge #8812 |
| IP Addresses | IP | IPv4 and IPv6 addresses | 192.168.1.100 |
| Usernames / Handles | USERNAME | Social media handles, login usernames | @jsmith, user: jsmith22 |
| Grades / GPA | GRADE | Letter grades, GPA, test scores tied to individuals | A+, GPA 3.87, scored 94 |
| Medical / Health Info | MEDICAL | Diagnoses, medications, IEP status, health conditions | IEP, Type 1 diabetes, Adderall |
| Immigration Status | IMMIGRATION | Visa type, citizenship status, DACA references | DACA recipient, F-1 visa |
| Race / Ethnicity | DEMO | Explicit racial or ethnic identifiers tied to individuals | Hispanic, African American |
| Religion | RELIGION | Religious affiliation tied to individuals | Catholic, Muslim, attends mosque |
| Gender / Pronouns | GENDER | Gender identity or pronouns when tied to a named individual | she/her, non-binary |

**UI layout:** `[ SANITIZE ALL ]` button at top (primary style when active, secondary when inactive). Below it, a two-column grid of checkboxes. Each row: tag prefix in monospace caps + plain-English label + brief example in muted `--text-secondary` color.

**Sensitivity tiers (visual grouping only - no functional difference):**
- Standard: PERSON, EMAIL, PHONE, ADDRESS, ID, ORG, FINANCIAL, DOB, SID, IP, USERNAME
- Sensitive: GRADE, MEDICAL, IMMIGRATION, DEMO, RELIGION, GENDER

Sensitive tier checkboxes carry a `[SENSITIVE]` label in `--warning` color. Informational only.

### 4.3 Entity-Linked Identifier System
This is the core logic. See Section 5 (Business Rules) for full spec.

### 4.4 Anonymize Mode

Processing pipeline in order:

1. **Extract** - pull all text and structure from the source file
2. **Detect** - send chunks to local LLM; build the replacement map
3. **Preview** - show the user what will be replaced before any file is written (see 4.11)
4. **Confirm** - user reviews preview and clicks `[ CONFIRM AND SCRUB ]` to proceed, or `[ CANCEL ]`
5. **Scrub** - apply replacements and deep-clean the output file (see 5.9)
6. **Verify** - run a post-scrub scan to confirm zero residual PII before offering download (see 5.10)
7. **Output** - save anonymized file locally; save key file; offer download and optional GitHub push
8. **Summary** - display count of replacements per PII type, verification result, processing time

### 4.5 Unanonymize Mode (restore AI output)
- **Keys:** a picker lists every saved key in `/keys` (original file, date, ID count). Current-format keys are selected by default - IDs never overlap across keys, so restoring against all of them is safe. Legacy 4-char keys are flagged `[LEGACY 4-CHAR]` and opt-in. `[ IMPORT KEY FILE ]` copies a key from another machine into `/keys`.
- **Input A - paste text:** the AI's answer pasted into a text box. Restored on screen with `[ COPY ALL ]` and `[ DOWNLOAD .TXT ]`. Nothing is written to disk.
- **Input B - a file:** the file the AI gave back (.txt .md .csv .xlsx .docx and the other supported formats) or the anonymized file itself. Output keeps the input's format: `{input_name}_restored.{ext}` (an `_anon_<session>` suffix is dropped). Restoring never strips the file's comments or metadata.
- **Tolerant matching (see 5.8):** exact, lowercased, markdown-escaped, un-bracketed, relabeled, and bare-hex identifiers all restore.
- **Report:** count restored per type; how many were relabeled by the AI or appeared as a bare ID (restored by ID); every unresolved identifier listed and left as-is; for files, any identifier that could not be written back (`residual`).
- Never requires network access

### 4.6 Key File Management
- Keys stored as JSON in a local `/keys` directory
- Filenames: `{original_filename}_{8-char-hex-session-id}.key.json`
- Key file is offered for download at completion
- User can also browse and load any existing key file

### 4.7 Processing Log
- All operations written to `anonymizer.log` in the app root
- Log visible in the UI in a collapsible panel
- Log entries include: timestamp, file name, PII counts per type, model used, processing time, any errors
- Log content is copy-pasteable for debugging

### 4.8 LLM Backend Manager
- User can configure one or more local LLM endpoints via a settings panel
- Each endpoint has: a user-defined nickname, a base URL, an API style (Ollama or OpenAI-compatible), and a model name
- Endpoints are persisted to a local `endpoints.json` file in the app root
- On page load: all configured endpoints are health-checked in parallel
- Status indicator per endpoint: green (reachable), red (unreachable), gray (unchecked)
- Active endpoint selector: a dropdown in the status bar lets the user pick which endpoint to use for the current session
- If no endpoints are configured or none are reachable: show setup instructions, block processing
- Last-used endpoint is remembered across sessions (persisted to `endpoints.json`)

### 4.9 Document Preview (Pre-Scrub)

Before any file is written, the app presents a preview panel showing exactly what will change. The user must explicitly confirm before scrubbing proceeds.

**Preview panel contents:**
- The full extracted text of the document rendered in the monospace log panel style
- Every detected PII span highlighted inline with its proposed placeholder. Example: `Jane Smith` renders as `[PERSON_3A4F]` in `--warning` color with the original text shown in a tooltip on hover
- A summary table above the preview: count of each PII type found, total entities, total replacement instances
- Any PII the user wants to un-mark (false positives) can be deselected via checkbox next to each highlighted span. Deselected spans are excluded from the replacement map before scrubbing proceeds.
- `[ CONFIRM AND SCRUB ]` primary button and `[ CANCEL ]` secondary button

**Preview is mandatory.** There is no "skip preview" option. The confirm step is the last human checkpoint before irreversible replacement.

**Large documents:** if extracted text exceeds 50,000 characters, show the first 10,000 chars with a `[ LOAD MORE ]` control and a note: "Showing first 10,000 of N characters. All detected PII will be scrubbed regardless of scroll position."

### 4.10 GitHub Repository Push

After a successful anonymize run (post-verification), the results panel shows a `[ PUSH TO GITHUB ]` secondary button.

**Flow:**
1. User clicks `[ PUSH TO GITHUB ]`
2. Inline panel opens below results
3. User selects a configured repo connection from a dropdown, or clicks `[ + CONNECT REPO ]`
4. User confirms target branch (pre-filled from saved connection, editable)
5. User optionally sets destination path within the repo (default: repo root, filename preserved)
6. User clicks `[ PUSH FILE ]`
7. App calls GitHub Contents API, shows success (with link to file on GitHub) or error inline

**What gets pushed:** the anonymized output file only. Key file never leaves local storage.

**GitHub connections** are stored in `github.json` in the app root (managed via UI, never hand-edited).

Each connection stores:
- Nickname
- Repository (`owner/repo` format)
- Default branch (e.g., `main`)
- Default destination path
- GitHub Personal Access Token (PAT) - stored in `github.json`, never logged, never displayed after initial entry (masked with `***`)

**PAT permissions required:** `repo` scope (or `public_repo` for public repos only).

**GitHub Connection Manager** (collapsible panel, same pattern as endpoint manager):
- List of configured connections: nickname, repo, branch, status
- `[ + ADD CONNECTION ]` button
- Add/edit form: nickname, repo (owner/repo), branch, destination path, PAT field (password input, show/hide toggle)
- `[ TEST CONNECTION ]` button per entry - calls `GET /repos/{owner}/{repo}` to verify PAT and repo access
- `[ EDIT ]` and `[ DELETE ]` per row

---

## 5. Business Rules and Logic

### 5.1 Identifier Format
- Format: `[TAG_XXXXXXXXXXXX]` where the suffix is 12 uppercase hexadecimal characters (48 bits)
- Every ID contains at least one letter A-F and at least one digit (never mistaken for a number or a word in AI output), and never has the shape digits-E-digits (Excel would read it as scientific notation)
- Examples: `[PERSON_3A4F9C2B1D0E]`, `[EMAIL_7C1B0A94E2D3]`, `[ADDRESS_0D1E2F3A4B5C]`
- Where brackets are illegal (spreadsheet sheet names and the formula references to them) the bracket-free form is used: `ORG_B9442179E0EE`
- v1.3 used 4-char IDs; those keys still restore (5.8)

### 5.2 Uniqueness (Critical Rule)
**Owner decision 2026-09-27 - supersedes v1.3 entity linking.** v1.3 gave all of an entity's PII one shared suffix (`[PERSON_3A4F]`, `[EMAIL_3A4F]`). That made AI output ambiguous to restore (an answer citing just the ID could mean the name or the email) and could give two name variants one placeholder. Now:

- Every distinct original value gets its own ID. One ID maps to exactly one value.
- The same exact value repeated anywhere keeps its ID (counts and joins still work).
- An ID is never issued twice, within a file or across files. A candidate is rejected if it is in the current session, in any key file in `/keys`, in `keys/issued_ids.ledger` (append-only list of every released ID - random IDs only, no PII), or already issued by the running process. IDs are recorded to the ledger when the key file is saved.
- The model may still return `linked_to` (the ID of an already-seen value this one belongs with). It is stored on the entity in the key file as a relationship; it never changes the ID.

### 5.3 Replacement Application
- Single pass, leftmost-longest: scanning left to right, the longest original starting at a position wins (prevents "Jane" replacing before "Jane Smith"). All originals are compiled into one trie-shaped pattern, so a text is scanned once (linear, not values x cells)
- Existing placeholders are never rewritten - a short value like "2B1D" can't match inside `[PERSON_3A4F9C2B1D0E]`
- Short numbers (no letters, under 7 digits: grades, room numbers, ZIPs) match only as whole numbers - "94" does not match in 1945, 94.5, 1,945, or a spreadsheet row index. Longer numbers keep substring matching
- XLSX cells stored as numbers or dates are matched on the same text the model saw and become text cells when replaced
- Replace ALL occurrences of each PII string, including in headers, footers, tables, metadata
- Case-sensitive matching (LLM is instructed to return text exactly as it appears)
- If the same string appears in multiple PII categories (edge case), the first-matched category wins

### 5.4 Chunking Strategy
- Text is chunked to fit within the selected model's context window (default 2,000 tokens per chunk; configurable per endpoint)
- Chunks overlap by 200 tokens to avoid splitting entities across boundaries
- After all chunks are processed, the full replacement map is deduplicated before application

### 5.5 LLM Prompt Design
System prompt instructs the local model to:
- Return ONLY a JSON array, no explanation, no markdown fences
- Each item: `{"text": "exact text as it appears", "type": "TAG", "linked_to": "12-char id or null"}` (`linked_to` is recorded as a relationship only - see 5.2)
- Temperature: 0 (deterministic)
- Include every occurrence, even repeats

The prompt must include the current entity registry so the model can recognize already-seen entities and return the correct `linked_to` value.

### 5.6 Format Preservation

| Format | Extraction Library | Output Format | Format Preserved? |
|---|---|---|---|
| PDF | `pdfplumber` | `.txt` | No - text only (v1) |
| XLSX / XLS | `openpyxl` | `.xlsx` | Yes - cell values, formatting, formulas |
| CSV | built-in `csv` | `.csv` | Yes |
| DOCX | `python-docx` | `.docx` | Yes - paragraph runs, tables, headers/footers |
| DOC | `python-docx` via LibreOffice convert | `.docx` | Partial - converted first |
| PPTX | `python-pptx` | `.txt` | No - text extracted from slides only |
| PPT | `python-pptx` via LibreOffice convert | `.txt` | No |
| ODT / ODS / ODP | LibreOffice convert to DOCX/XLSX/PPTX first | varies | Partial |
| TXT / RTF / HTML | direct read | `.txt` | No - text only |

**Format-specific rules:**
- **PDF:** Extract text per page via `pdfplumber`. Output anonymized `.txt`. Note in UI that formatting is not preserved. (v2: ReportLab or WeasyPrint rebuild.)
- **XLSX:** Replace cell values in-place using `openpyxl`. Preserve cell formatting. Formulas are left untouched; if a formula string contains matched PII, log a warning and flag in the results panel.
- **CSV:** Replace inline, output clean CSV.
- **DOCX:** Replace text in-place within XML runs via `python-docx`, walking paragraph runs and table cells. Preserve bold, italic, font, color.
- **DOC:** Convert to DOCX via LibreOffice (`soffice --headless --convert-to docx`) before processing. Output as DOCX.
- **PPTX:** Extract text from slide shapes via `python-pptx`. Output anonymized `.txt`. (v2: in-place slide text replacement.)
- **PPT:** Convert to PPTX via LibreOffice before processing.
- **ODT/ODS/ODP:** Convert to DOCX/XLSX/PPTX via LibreOffice before processing.
- **TXT/RTF/HTML:** Read as plain text, apply replacements, output `.txt`.

**LibreOffice dependency:** DOC, PPT, ODT/ODS/ODP conversion requires LibreOffice installed locally (`brew install --cask libreoffice` on Mac). If LibreOffice is not found, those formats are disabled in the UI with an install prompt.

### 5.7 Key File Schema
```json
{
  "session_id": "a3f9c1b2",
  "original_filename": "donor_list.xlsx",
  "created_at": "2026-05-01T14:32:00Z",
  "id_format": "hex12",
  "llm_endpoint": "http://localhost:11434",
  "llm_api_style": "ollama",
  "model_used": "llama3.2",
  "pii_types_scrubbed": ["names", "emails", "phones"],
  "entity_registry": {
    "3A4F9C2B1D0E": {"types": ["PERSON"], "values": {"PERSON": "Jane Smith"}},
    "7C1B0A94E2D3": {"types": ["EMAIL"], "values": {"EMAIL": "jane.smith@company.com"},
                     "linked_to": "3A4F9C2B1D0E"},
    "0D1E2F3A4B5C": {"types": ["PHONE"], "values": {"PHONE": "555-234-9988"},
                     "linked_to": "3A4F9C2B1D0E"}
  },
  "replacement_map": {
    "Jane Smith": "[PERSON_3A4F9C2B1D0E]",
    "jane.smith@company.com": "[EMAIL_7C1B0A94E2D3]",
    "555-234-9988": "[PHONE_0D1E2F3A4B5C]",
    "Bob Torres": "[PERSON_11C2D3E4F5A6]"
  }
}
```

### 5.8 Unanonymize Logic
- Load every selected key and merge into one index: 12-char ID -> (tag, original); legacy 4-char (tag, ID) -> original
- If two selected keys give the same identifier different values: refuse, name both keys, restore nothing
- One scan with a tolerant token grammar. Recognized forms: `[TAG_ID]`, any case, `\[TAG\_ID\]` (markdown-escaped), `TAG_ID` without brackets, a relabeled tag (`[DONOR_ID]` - restored by ID, counted as relabeled), and a bare 12-char ID (restored by ID, counted as untagged). Brackets are consumed only as a pair around one token, so `[A, B]` lists survive
- Legacy 4-char IDs restore only with their tag present; a bare 4-char hex is never touched
- Identifier-shaped tokens not in the keys (wrong length, unknown ID) are left untouched and reported
- Files use the same format-aware writers as anonymize (split Word runs handled) with the deep-clean handlers off; the output is re-extracted afterwards and any identifier the keys could still resolve is reported as `residual`
- Output filename: `{input_name}_restored.{ext}`

### 5.9 Deep Scrub - Zero Residual PII Guarantee

Replacing visible text is not enough. Modern document formats embed PII in hidden layers that survive a naive find-and-replace. The anonymizer must destroy PII in every layer or rebuild the file from scratch.

**Threat surfaces by format:**

**DOCX:**
- Revision history (`<w:ins>`, `<w:del>` tracked changes) - may contain original text before edits
- Comments (`word/comments.xml`) - may contain names, emails
- Document properties / metadata (`docProps/core.xml`, `docProps/app.xml`) - Author, Last Modified By, Company
- Custom XML parts
- Header and footer content (separate XML files - must be scrubbed separately)
- Textboxes inside drawing objects
- Alt text on images

Approach: after text replacement via `python-docx`, unpack the DOCX as a ZIP, walk every XML file in the archive, apply the full replacement map as a string substitution pass across raw XML, strip all revision history (`<w:ins>` and `<w:del>` elements removed entirely, keeping only final text), strip all comments (`word/comments.xml` cleared), strip author metadata from `docProps/core.xml` (set `<dc:creator>`, `<cp:lastModifiedBy>` to empty string), repack as new DOCX. The output is always a freshly constructed file, never a modified copy of the original.

**XLSX:**
- Named ranges that reference cell addresses containing PII
- Cell comments / notes
- Document properties (Author, Company in `docProps/core.xml`)
- Defined names in `xl/workbook.xml`
- Custom XML parts
- Formula strings (flag and warn if a formula contains a matched PII string - do not silently scrub formulas as this could break calculations)
- Chart titles and data labels
- Pivot table field names

Approach: after cell value replacement via `openpyxl`, unpack as ZIP, raw XML pass across all files in archive with full replacement map, strip `docProps/core.xml` author fields, strip all cell comments (`xl/comments*.xml` cleared), repack as new XLSX.

**PDF:**
- PDF metadata (Author, Creator, Producer, Keywords in document Info dictionary)
- XMP metadata stream
- Annotations and comments
- Form field values
- Embedded file attachments
- JavaScript actions
- Document outline (bookmark) text

Approach: extract text only (already producing `.txt` output in v1). The `.txt` output has no hidden layers. If/when PDF rebuild is added in v2, the rebuilt PDF must be constructed fresh via ReportLab - never by modifying the original PDF binary.

**CSV / TXT:**
- No hidden layers. Replacement pass on raw text is sufficient.

**PPTX:**
- Speaker notes (separate XML from slide content)
- Slide master and layout text
- Document properties
- Comments
- Alt text on images and shapes

Approach: after text extraction (v1 outputs `.txt`), no hidden layer risk. In v2 when in-place PPTX scrubbing is added, apply same ZIP unpack / XML walk / repack pattern as DOCX.

**Rule:** The output file must never be a modified copy of the original binary. It must always be a file constructed fresh by the application, assembled only from scrubbed content. This is the only way to guarantee that format-layer artifacts (undo history, temp streams, recovery data) from the original cannot persist.

### 5.10 Post-Scrub Verification Pass

After the output file is written, before it is offered for download or pushed to GitHub, the app runs a verification scan.

**Process:**
1. Re-extract all text from the output file using the same extraction pipeline used in step 1
2. Run the full replacement map in reverse - search the extracted text for any string that appears as a value in the replacement map (i.e., any original PII string) - outside of placeholders. A short original can legitimately occur inside a 12-char ID (`94` inside `[GRADE_3A94F2B1C0DE]`); that is not leaked PII. The raw-XML scan of DOCX/XLSX parts also ignores XLSX `<v>` cell values (numbers and shared-string indexes, already covered by the cell extraction) and number-only XML nodes with fewer than 7 digits (word counts, offsets)
3. Also run a set of fast regex patterns for common PII formats as a secondary net:
   - Email pattern: `[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}`
   - Phone pattern: common North American and international formats
   - SSN pattern: `\d{3}-\d{2}-\d{4}`
   - IP pattern: `\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}`
   - Credit card pattern: `\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}`
4. If any match is found: block download, show error in results panel, log the failure with the match type (not the matched value), and offer the user the option to retry or abort
5. If no matches: show `[VERIFIED: 0 RESIDUAL PII DETECTED]` in `--success` color in results panel. Enable download and GitHub push buttons.

**Verification result is logged:** pass/fail, match count if failed, match types if failed. The matched values themselves are never logged.

**The download and GitHub push buttons are disabled until verification passes.** No exceptions.

---

## 6. Data Requirements

### 6.1 Local Storage
```
/app-root/
  app.py                  # Flask application
  requirements.txt
  endpoints.json          # LLM endpoint configs; managed via UI
  github.json             # GitHub connection configs; managed via UI (PATs stored here - do not commit)
  anonymizer.log          # Rolling log file
  /templates/
    index.html            # Single-page UI
  /uploads/               # Temp; files deleted after processing
  /keys/                  # Key files; user manages retention
  /output/                # Anonymized and restored files; user manages retention
```

### 6.2 External Network Calls - Explicit Allowlist

The only permitted external network calls are:
1. **Local LLM endpoints** - localhost or LAN addresses only. App warns before saving any non-local URL.
2. **GitHub API** (`api.github.com`) - only when the user explicitly initiates a push via the GitHub push panel. Only the anonymized output file is transmitted. The original file and key file are never transmitted.

All other external network access is prohibited. No telemetry, no analytics, no update checks, no cloud storage.

### 6.3 Environment Variables (`.env`)
```
DEFAULT_ENDPOINT_URL=http://localhost:11434
DEFAULT_ENDPOINT_STYLE=ollama
DEFAULT_MODEL=llama3.2
MAX_UPLOAD_MB=50
CHUNK_TOKENS=2000
CHUNK_OVERLAP_TOKENS=200
PORT=5000
LOG_LEVEL=INFO
GITHUB_API_URL=https://api.github.com
```

### 6.4 Endpoints Config File (`endpoints.json`)
Persisted to app root. Managed via UI — not hand-edited by user.
```json
{
  "last_used": "ollama-local",
  "endpoints": [
    {
      "id": "ollama-local",
      "nickname": "Ollama (local)",
      "base_url": "http://localhost:11434",
      "api_style": "ollama",
      "model": "llama3.2",
      "chunk_tokens": 2000
    },
    {
      "id": "lmstudio-local",
      "nickname": "LM Studio",
      "base_url": "http://localhost:1234",
      "api_style": "openai",
      "model": "mistral-7b-instruct",
      "chunk_tokens": 2000
    }
  ]
}
```

**`api_style` values:**
- `ollama` - uses `POST /api/generate` with `{"model", "prompt", "stream": false}`
- `openai` - uses `POST /v1/chat/completions` with `{"model", "messages", "temperature", "stream": false}` (compatible with LM Studio, llama.cpp server, Jan, and others)

---

## 7. Integrations and Dependencies

### 7.1 Runtime Dependencies
| Package | Purpose |
|---|---|
| `flask` | Local web server |
| `python-dotenv` | Env config |
| `requests` | LLM API calls |
| `pdfplumber` | PDF text extraction |
| `python-docx` | DOCX/DOC read/write |
| `openpyxl` | XLSX/XLS read/write |
| `python-pptx` | PPTX/PPT text extraction |
| `tiktoken` | Token counting for chunking |
| LibreOffice (system) | DOC, PPT, ODT/ODS/ODP conversion (optional; disables those formats if absent) |

### 7.2 Local LLM Backend Support

The app communicates with local LLM servers via a thin adapter layer. Two API styles are supported:

**Ollama style** (`api_style: "ollama"`)
- Health check: `GET {base_url}/api/tags` - returns 200 with model list if running
- Inference: `POST {base_url}/api/generate`
  ```json
  {"model": "llama3.2", "prompt": "...", "stream": false, "options": {"temperature": 0}}
  ```
- Response: `data["response"]`
- Compatible with: Ollama

**OpenAI-compatible style** (`api_style: "openai"`)
- Health check: `GET {base_url}/v1/models` - returns 200 if running
- Inference: `POST {base_url}/v1/chat/completions`
  ```json
  {"model": "...", "messages": [{"role": "user", "content": "..."}], "temperature": 0, "stream": false}
  ```
- Response: `data["choices"][0]["message"]["content"]`
- Compatible with: LM Studio, llama.cpp server, Jan, Koboldcpp, LocalAI, and any OpenAI-spec local server

**Adapter contract:** All backend calls go through a single `llm_call(prompt: str) -> str` function that selects the right adapter based on the active endpoint's `api_style`. No other part of the app calls LLM endpoints directly.

**Non-local endpoint guard:** On save, the app checks that every endpoint's `base_url` resolves to localhost or a private IP range (10.x, 172.16-31.x, 192.168.x). If a public URL is entered, show a warning: "This endpoint may send your document to an external server. Are you sure?" Require explicit confirmation to save.

### 7.3 No Auth Required
- Localhost only; no login, no sessions beyond the active request

---

## 8. Out of Scope (v1)

- Multi-user support or any networked deployment
- PDF output format preservation (text output only for PDFs in v1; v2 target)
- In-place PPTX scrubbing with format preservation (text output only in v1; v2 target)
- OCR for scanned or image-only PDFs
- Batch processing of multiple files at once
- Automatic key file association (user manually selects key file for unanonymize)
- Custom PII type definitions beyond the 17 built-in types
- Audit trail or version history beyond the log file
- Cloud key storage or backup
- Pushing to branches other than the repo default or a user-specified branch (no PR creation, no branch management)

**Security note for build agent:** `github.json` contains PATs and must be added to `.gitignore` automatically during project setup. Claude Code must verify this before first commit.

---

## 9. Open Questions

1. Should the app auto-delete files from `/uploads` and `/output` after N hours, or leave retention entirely to the user?
2. For XLSX files containing formulas that reference cells with PII - should those formula strings be scrubbed, left alone, or flagged with a warning?
3. Should the key file be optionally password-protected (AES encryption at rest)?
4. What happens if the LLM endpoint times out mid-chunk? Should processing resume from the last completed chunk, or restart from scratch?
5. If the post-scrub verification pass finds a regex match that is a false positive (e.g., a product serial number that looks like a phone number), should the user be able to whitelist that pattern and re-run verification without re-scrubbing?
6. Should the preview panel allow the user to manually add PII spans the LLM missed (i.e., a text-selection highlight-and-tag UI)?

---

## 10. Success Criteria

- A user can drag in a donor list XLSX, anonymize it, share the anonymized version with an external analyst, and then fully restore the original data using only the local key file.
- The same person's name, email, and phone all share the same hex suffix in the output.
- No bytes of the original document are transmitted outside localhost at any point. Only the anonymized output file may leave the machine, and only when the user explicitly initiates a GitHub push.
- Post-scrub verification passes on every successful anonymize run (zero residual PII detected by both map-based and regex-based scans).
- A DOCX file with tracked changes, comments, and revision history produces an output file with all three stripped and zero author metadata.
- DOCX and XLSX output files open correctly in Microsoft Office and Google Docs with formatting intact.
- Processing a 500-row CSV completes in under 60 seconds on a mid-range Mac with a local 7B model.
- The log file contains enough detail that any processing error can be diagnosed without re-running.
- Unanonymize produces a byte-for-byte equivalent document to the original (content-equivalent; binary identity not required due to file format rebuilding).

---

## 11. Logging Infrastructure (Required in All Code)

Every module must write structured log entries to `anonymizer.log` using Python's `logging` module configured at `INFO` level by default, `DEBUG` available via env var `LOG_LEVEL=DEBUG`.

**Required log events:**
- App startup: port, configured endpoints, active endpoint, GitHub connections count
- File received: name, size, type
- Extraction complete: char count, page/sheet count
- Each LLM chunk call: endpoint nickname, chunk index, token count, response time, PII items found
- Replacement map built: total unique entities, count per type
- File written: output path, format, size
- Key file saved: path
- Any error: full traceback
- Unanonymize started: key file loaded, entity count
- Unanonymize complete: output path
- Preview shown: entity count, replacement count per type
- User confirmed scrub: timestamp
- User cancelled scrub: timestamp
- Deep scrub complete: layers cleaned per format (tracked changes stripped, comments stripped, metadata cleared)
- Verification pass result: PASS or FAIL; if FAIL: match count and match types (not values)
- GitHub push initiated: repo, branch, destination path, file size
- GitHub push result: success (with SHA) or failure (with HTTP status)

Log entries must include ISO timestamp, log level, and a short module label. Example:
```
2026-05-01T14:32:01Z [INFO] [extractor] DOCX extracted: 4,218 chars across 12 paragraphs
2026-05-01T14:32:03Z [INFO] [llm] Endpoint: Ollama (local) | Chunk 1/3: 312 tokens, 8 PII found, 2.1s
2026-05-01T14:32:05Z [INFO] [mapper] Replacement map: 3 PERSON, 3 EMAIL, 2 PHONE, 1 ADDRESS
2026-05-01T14:32:06Z [INFO] [writer] Output saved: ~/doc-anonymizer/output/donors_anon_a3f9c1b2.xlsx
```

---

## 12. UI Spec

Single HTML page served by Flask. No external CSS frameworks or CDNs. Vanilla JS only. System font stack only (`ui-monospace, 'Cascadia Code', 'Menlo', monospace` for mono; `system-ui, -apple-system, sans-serif` for labels and UI chrome).

---

### 12.1 Design Motif

**Terminal-esque, black and white.** The aesthetic is a high-contrast command-line tool dressed up as a web app - not a consumer SaaS. Think `htop` or a monochrome IDE. Every element earns its place by being functional. No decorative gradients, no rounded pill buttons, no soft shadows.

The full UI is built in monospace. No mixed font stacks - even labels, status text, and section headers use the mono font. The exception is the app title, which may use a slightly larger weight of the same mono font.

---

### 12.2 Color System

Implement using CSS custom properties. Light and dark mode switch via `prefers-color-scheme` media query with a manual toggle in the status bar that writes to `localStorage`.

**Dark mode (default):**
```css
--bg-primary:    #0a0a0a;   /* near-black page background */
--bg-surface:    #141414;   /* cards, panels, drop zone */
--bg-inset:      #1e1e1e;   /* inputs, log panel, code areas */
--border:        #2e2e2e;   /* all borders */
--border-focus:  #ffffff;   /* focused input border */
--text-primary:  #f0f0f0;   /* main text */
--text-secondary:#888888;   /* muted labels, metadata */
--text-dim:      #444444;   /* placeholder text, disabled states */
--accent:        #ffffff;   /* primary action color */
--accent-bg:     #ffffff;   /* primary button background */
--accent-fg:     #000000;   /* primary button text */
--danger:        #ff4444;   /* errors, warnings, destructive */
--success:       #00cc66;   /* success states, online indicators */
--warning:       #ffaa00;   /* caution states */
--tag-bg:        #1e1e1e;   /* PII tag chips */
--tag-border:    #444444;
--tag-text:      #cccccc;
--tag-active-bg: #ffffff;
--tag-active-text: #000000;
```

**Light mode:**
```css
--bg-primary:    #f5f5f5;
--bg-surface:    #ffffff;
--bg-inset:      #ebebeb;
--border:        #cccccc;
--border-focus:  #000000;
--text-primary:  #111111;
--text-secondary:#666666;
--text-dim:      #aaaaaa;
--accent:        #000000;
--accent-bg:     #111111;
--accent-fg:     #ffffff;
--danger:        #cc0000;
--success:       #007a3d;
--warning:       #996600;
--tag-bg:        #ebebeb;
--tag-border:    #bbbbbb;
--tag-text:      #333333;
--tag-active-bg: #111111;
--tag-active-text: #ffffff;
```

---

### 12.3 Button Rules

Buttons must always be visually distinct from the surface they sit on. No exceptions.

**Primary button** (Run, Download, Test Connection):
- Background: `--accent-bg`
- Text: `--accent-fg`
- Border: none
- Hover: 80% opacity
- Disabled: `--bg-inset` background, `--text-dim` text, `--border` border, cursor not-allowed
- Minimum height: 40px
- Padding: 0 20px
- Font: monospace, same size as body (14px)
- No border-radius or maximum 2px

**Secondary button** (Manage Endpoints, Copy Log, Clear):
- Background: transparent
- Text: `--text-primary`
- Border: 1px solid `--border`
- Hover: `--bg-inset` background
- Same sizing as primary

**Destructive button** (Delete endpoint):
- Border: 1px solid `--danger`
- Text: `--danger`
- Background: transparent
- Hover: `--danger` background, `--accent-fg` text

**Mode tabs** (Anonymize | Unanonymize):
- Active tab: `--accent-bg` background, `--accent-fg` text, no border
- Inactive tab: transparent background, `--text-secondary` text, 1px solid `--border` bottom only
- No border-radius

---

### 12.4 Layout and Sections

Max content width: 760px, centered. Page padding: 24px. Section spacing: 32px vertical.

**Sections (top to bottom):**

1. **Header bar** - App title (`DOC-ANON` or `ANONYMIZER` in monospace caps), light/dark toggle (text: `[DARK]` / `[LIGHT]`, no icon), version string
2. **Status bar** - Active endpoint: `> ENDPOINT: {nickname} [{model}]` in mono. Status dot (`●`) in `--success` or `--danger`. Endpoint selector dropdown. `[ MANAGE ENDPOINTS ]` and `[ MANAGE GITHUB ]` secondary buttons open their respective panels.
3. **Endpoint Manager Panel** (collapsible, hidden by default, inset surface) - List of configured endpoints, each row: status dot, nickname, URL, api_style, model, `[ EDIT ]` and `[ DELETE ]` buttons. "[ + ADD ENDPOINT ]" secondary button at bottom. Add/edit opens inline form: fields for nickname, base URL, api_style (radio: `OLLAMA` / `OPENAI-COMPAT`), model name, chunk token limit. `[ TEST ]` and `[ SAVE ]` buttons. Non-local URL shows inline warning in `--danger` color before allowing save.
4. **Mode tabs** - `[ ANONYMIZE ]` `[ UNANONYMIZE ]` tab strip
5. **File drop zone** - Dashed border (`--border`), background `--bg-surface`. Center text: `> DROP FILE HERE` on first line, `  or [ BROWSE ]` on second. After selection: filename, size, detected type, output format note (e.g., `OUTPUT: .txt (text only)`). Dashed border becomes solid `--accent` on drag-over.
6. **PII type selector** (anonymize mode only) - `[ SANITIZE ALL ]` primary button (full-width) at top. When active: solid fill, all checkboxes below shown checked and dimmed. When inactive: secondary style, checkboxes individually interactive. Below: two-column grid of checkboxes. Each row: `TAG` prefix in monospace + plain label + example in `--text-secondary`. Sensitive-tier rows carry a `[SENSITIVE]` badge in `--warning` color.
7. **Key file selector** (unanonymize mode only) - `[ SELECT KEY FILE ]` secondary button. After selection: displays key filename and creation date parsed from JSON.
8. **Run button** - Full-width primary button. Text: `[ DETECT PII ]` (anonymize) or `[ RUN UNANONYMIZE ]`. Disabled until file selected and active endpoint reachable. Initiates extraction and LLM detection only - does not scrub yet.
9. **Detection progress** (shown during LLM detection) - Monospace step list. `[ ]` pending, `[>]` active with blinking cursor, `[x]` done, `[!]` error:
    ```
    [x] extracting document...
    [x] chunk 1/4: 8 pii found
    [>] chunk 2/4: detecting pii_
    [ ] building replacement map
    [ ] preparing preview
    ```
10. **Preview panel** (shown after detection, before scrub) - Full extracted text in scrollable inset (`--bg-inset`, monospace 12px, max-height 400px). PII spans highlighted in `--warning` color with placeholder shown inline. Hover shows tooltip with original text and tag type. Each span has a small deselect checkbox for false positives. Above panel: `DETECTED: N entities / M replacements across X types`. Below panel: `[ CONFIRM AND SCRUB ]` primary button and `[ CANCEL ]` secondary button.
11. **Scrub progress** (shown after confirm) - Same monospace step list:
    ```
    [x] applying replacements...
    [x] deep scrub: tracked changes stripped
    [x] deep scrub: comments cleared
    [x] deep scrub: metadata sanitized
    [>] verification scan running_
    [ ] output ready
    ```
12. **Results panel** (shown after verification) - Monospace summary block:
    ```
    ----------------------------------------
    COMPLETE: donors_anon_a3f9c1b2.xlsx
    VERIFIED: 0 RESIDUAL PII DETECTED
    ----------------------------------------
    PERSON    4 entities   12 replacements
    EMAIL     4 entities    4 replacements
    PHONE     3 entities    6 replacements
    ADDRESS   1 entity      2 replacements
    ----------------------------------------
    TOTAL    12 entities   24 replacements
    ----------------------------------------
    ```
    VERIFIED line in `--success` color. Failure: `[!] VERIFICATION FAILED: N matches` in `--danger`, download blocked.
    Below: `[ DOWNLOAD FILE ]` and `[ DOWNLOAD KEY ]` primary buttons (disabled if verification failed). `[ PUSH TO GITHUB ]` secondary button (disabled if verification failed).
13. **GitHub push panel** (inline, opens on `[ PUSH TO GITHUB ]`) - Connection dropdown + `[ + CONNECT REPO ]`. Branch input (editable, pre-filled). Destination path input. `[ PUSH FILE ]` primary button. Inline result: success with file link or error with HTTP status.
14. **Log panel** - `[ LOG ]` toggle header. Inset `--bg-inset`, monospace 12px, 200px fixed height, overflow-y scroll. Last 50 lines. `[ COPY LOG ]` secondary button in header.

---

### 12.5 Accessibility and Contrast

- All text/background combinations must meet WCAG AA (4.5:1 minimum contrast ratio)
- Primary button (`#ffffff` on `#111111` and vice versa) exceeds AAA
- `--danger` (#ff4444 on dark, #cc0000 on light) must be verified against surface colors before shipping
- Focus states: `outline: 2px solid --border-focus` on all interactive elements, no `outline: none`
- All form inputs must have visible labels (not placeholder-only)
- Light/dark toggle must work via keyboard

---

*Built to run on Mac. All paths should default to `~/doc-anonymizer/` and use Mac conventions.*
