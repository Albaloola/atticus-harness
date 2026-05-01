# Pull Request: Fix OCR quality, coverage, and evidence extraction reliability

## Summary

The harness must stop treating poor OCR/extraction as if it were reliable evidence.

Recent Napier/Anfal work exposed a serious product problem: the harness had the relevant tenancy and guarantor documents, but extraction quality was too weak to support proper downstream analysis. In particular, PDF text extraction can miss handwritten/signature content and the source-led generator can select thin sentence fragments instead of the legally relevant clauses.

This PR should make OCR/extraction a first-class, quality-gated subsystem rather than a best-effort text dump.

## Problem

Current extraction behaviour is not good enough for legal evidence work.

Observed examples:

- `NAP-SRC-0071` — Guarantor Form 2025-26:
  - `pdftotext` extracted typed text;
  - signature lines appeared as blank underscores;
  - handwriting/signature presence was not reliably captured;
  - the harness had no strong visual/OCR quality warning for this.
- `NAP-SRC-0072` — Student Licence / Tenancy Agreement:
  - the tenancy agreement was present and extracted;
  - the harness still carried a stale “obtain tenancy agreement” blocker;
  - source-led packet generation selected a thin NTQ fragment instead of a full review of the agreement’s key terms.
- Downstream candidates were too thin and required manual reducer review because extraction/chunk selection did not provide a good evidence foundation.

The harness should not require Omer to debug OCR quality or explain that a document is present when it is already registered.

## Product goal

When a source is registered, the harness should be able to say clearly:

- is the document text-extractable?
- does it need OCR?
- does it need visual OCR even though embedded PDF text exists?
- are handwritten fields/signatures present or missing?
- is extraction coverage good enough for legal analysis?
- should downstream tasks be rebuilt because new/better OCR changed the evidence base?

A normal user should see a plain-language status, not a pile of stale blockers.

## Key changes required

### 1. Add OCR quality assessment

Add a quality assessment layer after extraction.

For each extracted source, compute and store metrics such as:

```json
{
  "text_bytes": 87408,
  "page_count": 16,
  "method": "pdf_text",
  "ocr_engine": null,
  "confidence": 0.95,
  "quality_status": "complete|partial|low_confidence|needs_visual_ocr|failed",
  "warnings": [
    "signature_lines_detected_without_handwriting_capture",
    "low_text_density_page_2",
    "possible_scanned_or_image_only_page"
  ]
}
```

Quality should not be based only on whether text exists.

### 2. Add PDF visual OCR fallback

For PDFs, do not rely on `pdftotext` alone.

If a PDF contains any of the following, run or queue visual OCR for affected pages:

- signature fields;
- handwritten forms;
- scanned pages;
- very low extracted text density;
- images embedded in otherwise text-based PDFs;
- blank-looking form fields where handwriting may exist;
- pages with important visual evidence.

Implementation can start local-only:

```bash
pdftoppm -> page images -> tesseract -> page OCR text
```

Store page-level OCR output under matter working files, for example:

```text
03-working/ocr/NAP-SRC-0071/page-001.txt
03-working/ocr/NAP-SRC-0071/page-002.txt
```

Then create a combined OCR artifact with page provenance.

### 3. Add image preprocessing before Tesseract

Before OCRing images or PDF-rendered pages, add local preprocessing options:

- grayscale;
- deskew if available;
- thresholding;
- contrast/sharpening;
- higher DPI render for PDFs, e.g. 300 DPI;
- preserve original image hash and preprocessing metadata.

This can use local tools if installed:

- ImageMagick `magick` / `convert`;
- `pdftoppm`;
- `tesseract`.

No provider calls are required for the first version.

### 4. Add `--force` / `--reocr` extraction controls

`extract-sources` currently skips sources with existing complete coverage.

Add controls such as:

```bash
atticus extract-sources --db DB --matter MATTER --workspace WORKSPACE --source-id NAP-SRC-0071 --reocr --write
atticus extract-sources --db DB --matter MATTER --workspace WORKSPACE --source-id NAP-SRC-0071 --force --write
atticus extract-sources --db DB --matter MATTER --workspace WORKSPACE --quality-only --json
```

Expected semantics:

- `--force`: regenerate extraction even if coverage exists;
- `--reocr`: specifically regenerate OCR/visual OCR artifacts;
- `--quality-only`: assess extraction/OCR quality without rewriting artifacts unless `--write` is supplied.

### 5. Add page-level provenance

Every extracted/OCR text chunk should be traceable to:

- source ID;
- source SHA-256;
- page number;
- extraction method;
- OCR engine;
- rendered image hash if OCR came from a PDF page image;
- confidence/quality status.

This matters because legal claims must not cite vague OCR blobs.

### 6. Add form/signature detection heuristics

For forms like `NAP-SRC-0071`, detect field labels such as:

- `Student Handwritten Signature`;
- `Guarantor’s Handwritten Signature`;
- `Witness’s Handwritten Signature`;
- `ALL THREE SIGNATURES ABOVE MUST BE HANDWRITTEN`;
- `Date`;
- `Full Name`.

If these appear, the extractor should flag that visual inspection/OCR is needed even if `pdftotext` returns text.

The harness should distinguish:

```text
text extracted from typed PDF
```

from:

```text
signature/handwriting presence verified or not verified
```

### 7. Prevent stale “missing document” blockers when the document is present

If a task says “obtain tenancy agreement” but the matter already contains sources matching tenancy/licence agreement terms, the harness should route the task to review/reanalysis, not keep asking the operator for the document.

For the Napier matter, once `NAP-SRC-0010` or `NAP-SRC-0072` exists and has extraction coverage, the task:

```text
napier-accommodation-arrears-obtain-tenancy-agreement
```

should become:

```text
review existing tenancy/licence agreement sources
```

not a missing-document blocker.

### 8. Improve source-led packet generation after OCR repair

Source-led packets must not select one thin lexical fragment where a task requires document review.

For tasks like “review tenancy agreement”, the packet should gather the legally/materially relevant clauses:

- parties;
- premises;
- start/end dates;
- rent amount and payment schedule;
- guarantor/arrears clauses;
- Notice to Quit / termination clauses;
- jurisdiction / Scots law clauses;
- internal complaints/dispute clauses if present;
- student accommodation exclusion/private residential tenancy wording.

A single fragment like:

```text
Agreement (“a Notice to Quit / NTQ”) requiring the Tenant to remove...
```

is not a sufficient candidate for reducer acceptance.

### 9. Add OCR status command

Add a command for human/agent monitoring:

```bash
atticus ocr status --db DB --matter MATTER --json
atticus ocr status --db DB --matter MATTER
```

It should show:

- sources needing OCR;
- sources with low-confidence OCR;
- sources needing visual OCR despite PDF text;
- sources whose OCR is stale against current SHA;
- sources whose downstream tasks/candidates should be rebuilt.

Optional repair command:

```bash
atticus ocr repair --db DB --matter MATTER --source-id NAP-SRC-0071 --write
```

### 10. Trigger downstream rebuilds when OCR improves

When extraction/OCR changes for a source, mark dependent tasks/candidates stale or queue repair work.

Example:

- improved OCR for `NAP-SRC-0072` should stale/rebuild tasks that previously concluded “tenancy agreement missing”;
- improved OCR for `NAP-SRC-0071` should allow guarantor analysis and signature caveat assessment;
- final gate should not pass on candidates built from stale/low-quality OCR.

## Safety requirements

- Local OCR/extraction must not call external providers by default.
- Do not upload documents to third-party OCR unless explicitly configured and approved.
- Do not treat OCR text as source proof without provenance.
- Do not certify legal conclusions from low-confidence OCR without warning.
- Do not silently overwrite old extraction artifacts; preserve provenance and mark superseded/stale where needed.
- Handwriting/signature detection can flag “needs visual verification”; it should not pretend to authenticate signatures.

## Suggested files likely touched

```text
atticus/extraction/local.py
atticus/cli.py
atticus/commands/registry.py
atticus/db/schema.py
atticus/db/repo.py
atticus/validation/gates.py
atticus/retrieval/source_chunks.py
atticus/agents/repair_planner.py
atticus/status/completion.py
docs/ocr-quality.md
tests/test_local_extraction.py
tests/test_ocr_quality.py
tests/test_human_interface.py
```

Exact file set may differ depending on implementation.

## Tests

Add tests for:

- PDF text extraction with embedded text still flags signature/form pages for visual OCR;
- image OCR records include confidence and source SHA provenance;
- `--force` regenerates extraction coverage;
- `--reocr` regenerates OCR coverage;
- low-confidence OCR blocks final gate or creates repair routing;
- stale OCR after source hash change is detected;
- tenancy agreement source present prevents stale “obtain tenancy agreement” human blocker;
- source-led packet for tenancy review includes key clauses, not one thin fragment;
- no external provider call happens during local OCR repair.

Run:

```bash
python -m pytest tests/test_local_extraction.py tests/test_ocr_quality.py tests/test_human_interface.py -q
python -m pytest -q
python -m compileall -q atticus tests
git diff --check
```

## Acceptance criteria

The PR is accepted when:

- `extract-sources` can force/re-OCR a source;
- PDFs with signature/form fields can trigger visual OCR or at least a clear `needs_visual_ocr` quality warning;
- OCR quality metadata is persisted and visible;
- low-quality OCR is not treated as clean evidence coverage;
- `ocr status` shows exactly what needs repair;
- downstream tasks are marked stale or rebuilt after OCR improves;
- `napier-accommodation-arrears-obtain-tenancy-agreement` no longer remains a missing-document blocker once `NAP-SRC-0010`/`NAP-SRC-0072` is present;
- source-led packets for legal document review include all material clauses;
- tests pass.

## Napier regression scenario

Use the Napier/Anfal matter as a regression case:

```text
Matter: napier-accommodation-arrears
Sources:
- NAP-SRC-0071 — Guarantor Form 2025-26 provided by Omer 2026-05-01
- NAP-SRC-0072 — Student Licence / Tenancy Agreement provided by Omer 2026-05-01
Task:
- napier-accommodation-arrears-obtain-tenancy-agreement
```

Expected result:

- the harness recognises the tenancy/licence agreement is present;
- OCR/extraction quality is assessed;
- guarantor form signature/handwriting areas are flagged for visual verification if not captured;
- downstream analysis is rebuilt from the improved evidence base;
- final gate is blocked only by real reducer/legal review issues, not by false missing-document/OCR confusion.

## Required autonomous/live-provider behaviour

This OCR/extraction repair must also fix the live-provider/autonomy regression.

Omer's instruction to continue harness work should mean continue the harness work. The harness must not perform one tiny local step, stop, and then ask again merely because the next useful step uses the normal live provider route.

Live provider capability should be treated as operational readiness metadata, not a repeated human approval blocker.

Required behaviour:

- If the operator has instructed the harness/agent to continue the matter, scheduler/orchestrator/provider-backed continuation should proceed through the normal configured route.
- The control panel and monitor may display `requires_live_provider=true`, provider, model, estimated cost, and risk metadata.
- They must not convert normal provider-backed continuation into a fresh human blocker.
- The harness should only interrupt Omer for genuine human questions, high-risk reducer/legal review, or unsafe external action.
- OCR repair/reanalysis should be able to continue through provider-backed rebuild tasks when needed, rather than stopping after a deterministic source-led fragment.

The rejected UX is:

```text
I did one local tick. To actually continue, say another approval phrase.
```

The desired UX is:

```text
Omer said continue. Continue the safe harness work until complete, genuinely blocked, or needing legal/reducer review.
```

For Napier, once `NAP-SRC-0071` and `NAP-SRC-0072` are present, the harness should autonomously rebuild the tenancy/guarantor analysis using those sources and only stop for:

- reducer review of substantive legal conclusions;
- final-gate certification decision;
- a genuine missing fact/document not already present;
- external action approval.

Do not reintroduce a `needs_live_approval` state or equivalent repeated prompt.
