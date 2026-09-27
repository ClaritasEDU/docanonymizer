# Business Spec: Doc Anonymizer

**Last updated:** 2026-07-17  
**Status:** v1 implementation complete; web edition added; code review findings in CODE_REVIEW.md pending fixes

---

## The Problem

Public LLMs are powerful analytical tools. But the most valuable documents - donor lists, personnel records, financial reports, legal contracts - contain PII that cannot legally or ethically be sent to a third-party server. This creates a hard wall: the data that most needs analysis is the data that can't be shared.

Existing anonymization tools are either cloud-based (defeating the purpose) or require technical setup that non-developers can't manage. There is no simple, local, reversible document anonymizer built for a non-technical operator.

---

## The Solution

A locally-hosted web app that runs entirely on the user's machine. It accepts any major document format, uses a locally-running LLM to detect PII, and replaces every instance with a consistent labeled placeholder. The original document never leaves the machine.

The key design decisions that make this useful rather than just safe:

**Consistent, linked identifiers.** Every PII type for the same real-world person shares a hex suffix. Jane Smith's name, email, and phone all become `_3A4F`. An analyst looking at the anonymized file can still observe that Person 3A4F emailed Address 3A4F from Phone 3A4F - the relational structure is intact even though the identities are hidden.

**Comprehensive PII coverage.** 17 PII categories covering the full range of sensitive data types: names, email, phone, address, SSN/tax IDs, organizations, financial account data, dates of birth, student/employee IDs, IP addresses, usernames, grades/GPA, medical and health information, immigration status, race/ethnicity, religion, and gender/pronouns. A SANITIZE ALL mode covers everything by default. Sensitive categories are visually flagged so the operator knows what they are enabling.

**Mandatory human preview.** Before any file is written, the operator reviews every detected PII span highlighted inline with its proposed replacement. False positives can be deselected. Nothing is scrubbed until the operator explicitly confirms. This step cannot be skipped.

**Deep scrub, not just find-and-replace.** Replacing visible text is not enough. DOCX and XLSX files contain PII in hidden layers: tracked changes, revision history, comments, and author metadata embedded in XML. The app strips all of these, walks every XML file in the document archive, and constructs the output as a freshly built file - never a modified copy of the original binary. There is no path by which the original content can survive in the output.

**Verified clean before release.** After scrubbing, the app re-extracts all text from the output file and scans it against both the replacement map and a set of regex patterns for common PII formats. The download button stays disabled until this scan returns zero matches. The operator sees a visible VERIFIED confirmation before the file is available.

**Full reversibility.** A local key file maps every placeholder back to the original value. The operator can unanonymize at any time, on any machine that has the key file, without any network access.

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

**Deferred to v2:**

7. Optional AES-encrypted key files at rest.
8. In-place highlight-and-tag in the preview panel (custom terms cover the need for now).
