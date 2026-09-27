# Business Spec: Doc Anonymizer

**Last updated:** 2026-09-27  
**Status:** v1.4 - unique 12-character identifiers and the AI round trip (anonymize -> analyze in any AI tool -> restore the answer) are built and tested

---

## The Problem

Public LLMs are powerful analytical tools. But the most valuable documents - donor lists, personnel records, financial reports, legal contracts - contain PII that cannot legally or ethically be sent to a third-party server. This creates a hard wall: the data that most needs analysis is the data that can't be shared.

Existing anonymization tools are either cloud-based (defeating the purpose) or require technical setup that non-developers can't manage. There is no simple, local, reversible document anonymizer built for a non-technical operator.

---

## The Solution

A locally-hosted web app that runs entirely on the user's machine. It accepts any major document format, uses a locally-running LLM to detect PII, and replaces every instance with a consistent labeled placeholder. The original document never leaves the machine.

The core flow the owner uses it for:

1. Add a spreadsheet (or any document).
2. Names, emails, phone numbers, addresses (and the other PII categories) become unique 12-character hex identifiers like `[PERSON_3A4F9C2B1D0E]`.
3. See the anonymized output on screen, verified clean.
4. Paste it into any AI tool for analysis.
5. Bring the AI's answer back. Every identifier becomes the real value again - with no chance of mixing up two people, two addresses, or two phone numbers.

The key design decisions that make this useful rather than just safe:

**Unique identifiers, one per value, never reused.** (Owner decision 2026-09-27, replacing the v1.3 shared-suffix design.) Every distinct value gets its own 12-character hex identifier: Jane Smith is `[PERSON_3A4F9C2B1D0E]`, her email is `[EMAIL_7C1B0A94E2D3]`. The same value repeated anywhere keeps its identifier, so counts, rankings, and joins still work for analysis. No identifier is ever issued twice - not within a file and not across files - because every new ID is checked against all saved keys and a permanent ledger of issued IDs. Two spreadsheets pasted into the same AI chat can never collide.

Why the change: under v1.3, Jane's name, email, and phone shared one suffix (`_3A4F`). When an AI answer referred to just the ID, or relabeled it, there was no way to know whether it meant her name or her email - and v1.3 could even give two different name variants the same placeholder. One ID per value makes every identifier a one-to-one pointer back to exactly one original. The relational structure an analyst needs is still there: in a spreadsheet the row carries it, and the key file records which values the model believed belong together (`linked_to`).

**Comprehensive PII coverage.** 17 PII categories covering the full range of sensitive data types: names, email, phone, address, SSN/tax IDs, organizations, financial account data, dates of birth, student/employee IDs, IP addresses, usernames, grades/GPA, medical and health information, immigration status, race/ethnicity, religion, and gender/pronouns. A SANITIZE ALL mode covers everything by default. Sensitive categories are visually flagged so the operator knows what they are enabling.

**Mandatory human preview.** Before any file is written, the operator reviews every detected PII span highlighted inline with its proposed replacement. False positives can be deselected. Nothing is scrubbed until the operator explicitly confirms. This step cannot be skipped.

**Deep scrub, not just find-and-replace.** Replacing visible text is not enough. DOCX and XLSX files contain PII in hidden layers: tracked changes, revision history, comments, and author metadata embedded in XML. The app strips all of these, walks every XML file in the document archive, and constructs the output as a freshly built file - never a modified copy of the original binary. There is no path by which the original content can survive in the output.

**Verified clean before release.** After scrubbing, the app re-extracts all text from the output file and scans it against both the replacement map and a set of regex patterns for common PII formats. The download button stays disabled until this scan returns zero matches. The operator sees a visible VERIFIED confirmation before the file is available.

**Full reversibility, including AI output.** A local key file maps every identifier back to the original value. The operator can restore at any time, on any machine that has the key file, without any network access. Restoring works on whatever the AI hands back - pasted text, or a .txt / .md / .csv / .xlsx / .docx file - and tolerates how AI tools mangle identifiers: lowercased, brackets dropped, markdown-escaped (`\[PERSON\_...\]`), relabeled (`[DONOR_...]`), or reduced to the bare 12-character hex. Because each ID is unique, the hex alone is enough to restore safely. Anything that looks like an identifier but isn't in the selected keys (a typo, a truncated ID) is left untouched and listed, never guessed. All saved keys can stay selected at once, since IDs never overlap; keys from another machine can be imported.

**Copy for AI.** The anonymized output has a `[ COPY FOR AI ]` button that prepends one sentence asking the AI to keep identifiers exactly as written. That single instruction is the biggest factor in getting a cleanly restorable answer.

**Any local LLM.** The app connects to any locally-running LLM server - Ollama, LM Studio, llama.cpp, or any OpenAI-compatible endpoint. Multiple endpoints can be configured and switched between. This future-proofs the tool against any single model or runtime becoming unavailable.

**Direct repo delivery.** After a verified scrub, the operator can push the anonymized file directly to a GitHub repository branch. This closes the last gap in the workflow: the clean file goes exactly where the analyst needs it, without the operator manually downloading and uploading it.

**Copy / paste straight from the screen.** Many uses of this tool end with the operator pasting clean text into another app (a chat with a public LLM, a Slack thread, an email). After a verified scrub, the anonymized text is also available right in the page with a one-click `[ COPY ALL ]`. No file download, no opening another app, no risk of grabbing the wrong file. The downloadable file is still produced and verified - on-screen text is an additional output, not a replacement.

---

## Who Uses This

Primary user: a single operator (initially the product owner) who needs to prepare documents for AI-assisted analysis without exposing sensitive data to public services.

Secondary use: small teams where documents pass through a compliance review step before going to analysts who use AI tools.

**Role in the ClaritasEDU school suite (owner ruling 2026-08-06):** this
tool is the designated deep-scrub for school newsletters headed to any
frontier model outside ParentPoint's own guarded extraction pipeline —
e.g. an operator pasting a newsletter into a hosted AI chat for ad-hoc
analysis. ParentPoint's automated pipeline keeps its own roster-driven
name tokenizer; this tool covers what a roster structurally can't know
(non-roster names, addresses, phones — the full 17-category sweep). The
ruling, its Beacon counterpart (already-family-distributed newsletters may
reach Beacon PII-intact — that content is school-published), and the open
integration-shape decision (manual pre-step today; possibly a service on
the school's on-prem box later, using its local LLM — tracked as
parentpoint tracker B50) live in `parentpoint/PROTECTED_DATA_CLASSES.md`
Part 1 ("Newsletters — owner rulings 2026-08-06"). Nothing about this
tool's local-only privacy contract changes: the scrub still never runs in
the cloud, by construction.

---

## Why Local

- HIPAA, FERPA, and diocesan data governance rules prohibit sending certain records to third parties
- Donor confidentiality expectations in Catholic nonprofit fundraising
- Personnel records cannot leave HR systems without authorization
- Legal contracts under NDA cannot be sent externally
- Student records (grades, IEP status, medical accommodations) carry strict handling requirements under FERPA and IDEA

A local tool with no external dependencies satisfies all of these constraints by construction, not by policy. The only permitted external network call is the GitHub push, and only when the operator explicitly initiates it with a verified-clean file.

---

## Scope Boundaries

This is a document preparation tool, not an analysis tool. It anonymizes. It does not summarize, classify, or extract insights. Those jobs belong to the LLM the user runs the anonymized document through afterward.

v1 ships with text-preserving output for PDF and PPTX (formatting not rebuilt). Full format-preserving output for XLSX, DOCX, and CSV. All other major formats extracted to text.

v2 targets PDF rebuild with formatting preserved, and in-place PPTX scrubbing.

---

## Web Edition (added 2026-07-17)

A second delivery channel: `web/index.html`, a single static page deployable to
Netlify. It exists for the case where the operator is away from the machine
running the local LLM but still needs to anonymize before pasting into a
public LLM.

The privacy guarantee holds by construction: the page is static, all
processing happens inside the browser tab, and the deploy's security policy
(connect-src 'none') makes network calls from the page impossible. The
document never leaves the device.

Differences from the local app, accepted as scope:

- Detection is pattern-based (9 categories) plus an operator-supplied custom
  terms list - not LLM-based. Weaker on names; the custom terms box and the
  mandatory preview are the compensating controls.
- Formats: txt, md, csv, docx, xlsx. No PDF.
- Outputs three artifacts per run: the anonymized file, a human-readable
  decoder ring (.txt), and a key.json interchangeable with the local app's
  key files. Either the decoder ring or the key.json drives deanonymize.
- Verification hard-blocks release only on actual replacement-map residue.
  Generic pattern residue is a warning, not a dead end.

---

## Operational Decisions (resolved during implementation)

The PRD listed six open questions. Three are resolved as built; three are deferred to v2.

**Resolved:**

1. **Auto-cleanup of `/uploads` and `/output`.** `/uploads/` is purged immediately after every terminal state - success, failure, or cancel - along with any LibreOffice conversion temp directory and staged scrub files. An output that fails verification still contains PII, so it is deleted too (quarantined), never left in `/output/`. Verified outputs and `/keys/` are kept - they are work product.
2. **XLSX formulas containing PII.** The formula cell is not rewritten by the cell pass, but the deep XML pass replaces PII literals inside formula text, and the verifier scans raw sheet XML so formula content can never escape the map scan. `formula_warnings` still surface so the operator can confirm the formula still computes.
3. **LLM timeout mid-chunk.** Retry the chunk (3 attempts with backoff), then abort the whole run. A skipped chunk would ship PII with no safety net - names and addresses have no regex shape the verifier could catch. The earlier "log and continue" behavior was a privacy hole and is gone.
4. **False-positive regex matches in verification.** Verification now has two tiers. Any replacement-map original still present anywhere in the output is a hard fail that blocks release and deletes the output. Generic pattern hits (phone-shaped, IP-shaped strings) are warnings shown in the results panel - they cannot block, because any 10-digit invoice number or version string would otherwise dead-end a clean document.
5. **Operator-added PII terms.** A CUSTOM TERMS box on the anonymize panel takes one term per line (optional `ORG:`-style type prefix). Terms found in the document are guaranteed catches regardless of what the LLM finds. This replaces the deferred highlight-and-tag preview UI with something simpler.
6. **Endpoint locality.** Enforced server-side, not just in the browser. Saving a non-local endpoint requires an explicit override flag, and every LLM call to a non-local endpoint logs a loud warning.

7. **Identifier uniqueness (2026-09-27).** Enforced, not probabilistic: new IDs are checked against the session, every key in `/keys`, `keys/issued_ids.ledger` (append-only, random IDs only, no PII), and IDs issued by the running process.
8. **Legacy 4-character keys (2026-09-27).** Still restore, but only with their tag present (`[PERSON_3A4F]`), since a bare 4-character hex is too common in ordinary text. Old keys that gave two values the same placeholder restore to the longer value and say so. They are opt-in in the key picker; if two selected keys disagree about an identifier, restore refuses and names both keys.
9. **Short numbers (2026-09-27).** A value with no letters and fewer than 7 digits (a grade, room number, ZIP, 5-digit student ID) is replaced only where it stands as a whole number - a grade of 94 does not touch 1945, 94.5, or row 94 of a spreadsheet. Phones, SSNs, and account numbers keep plain substring matching.
10. **Spreadsheet realities (2026-09-27).** Phones, ZIPs, and birthdays stored as numbers or dates are scrubbed (the cell becomes text). Sheet names that contain PII get the bracket-free form (`ORG_B9442179E0EE`, since Excel forbids brackets in sheet names) and every formula, defined name, and pivot reference follows the rename.

**Deferred to v2:**

11. Optional AES-encrypted key files at rest.
12. In-place highlight-and-tag in the preview panel (custom terms cover the need for now).
