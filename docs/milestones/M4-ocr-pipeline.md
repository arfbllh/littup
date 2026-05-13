# M4 — OCR Pipeline

**Estimated time:** 3 hours (the longest single milestone)
**Dependencies:** M2, M3
**Rubric impact:** Document Processing — the single biggest concrete deliverable for the 25-point category

## Goal

A working OCR pipeline that classifies pages, routes them through the right engine, escalates to VLM only when justified, preserves coordinates throughout, and respects the per-document and global VLM budget caps. After this milestone, uploading a mixed pack of synthetic documents results in extracted text per page with confidence scores.

This is the milestone where the reviewer's "handling of messy inputs" rubric line is decided.

## Context Claude Code must read

1. `docs/architecture/10-fixes-and-non-negotiables.md` — **NN-6**
2. `docs/architecture/03-components/ingestion-ocr.md` — full spec
3. `docs/architecture/02-architecture.md` (data flow happy path)
4. References from the architect skill (already incorporated in the spec — don't re-read)

## Non-Negotiables that apply

- **NN-6** — per-document and global VLM caps; `MAX_VLM_PAGES_PER_DOC` enforced before every VLM call

## Files to create / modify

### Configuration

- `config/ocr.yaml`:
  ```yaml
  max_vlm_pages_per_doc: 20
  paddleocr_confidence_threshold: 0.7
  classify:
    blurry_laplacian_variance_threshold: 100
    handwriting_stroke_ratio_threshold: 0.35
  preprocess:
    deskew_max_angle_deg: 15
    denoise: true
    binarize: true
  ```

### OCR engines

- `app/ingest/ocr/base.py` — `OCRProvider` protocol:
  - `async extract_page(image_or_pdf_path, page_num) -> PageExtraction`
  - `PageExtraction(spans: list[Span], full_text: str, mean_confidence: float, source: str)`
- `app/ingest/ocr/pdfplumber_ocr.py` — for native PDFs; extracts text + word-level bboxes; returns `mean_confidence=1.0`
- `app/ingest/ocr/paddle_ocr.py` — PaddleOCR; lazy-load model on first call; CPU-mode by default, GPU if `OCR_USE_GPU=1`; returns word-level spans with confidences
- `app/ingest/ocr/vlm_ocr.py` — sends page image to `LLMRouter.generate(task="vision", ...)`; prompt asks for structured JSON: `[{"text":...,"bbox":[x0,y0,x1,y1],"line":N}]`. Schema enforces.
- `app/ingest/ocr/preprocess.py` — OpenCV pipeline: deskew via `cv2.minAreaRect` on text contours; denoise; adaptive thresholding for binarization; resize to ~300 DPI equivalent
- `app/ingest/ocr/routing.py` — `route_page(classified_page, doc_state, config) -> OCRProvider`:
  - Native PDF → pdfplumber
  - Clean scan / form / multi-column → PaddleOCR (with/without preprocess)
  - Handwriting heavy → preprocess + PaddleOCR; if confidence < threshold AND doc.vlm_pages_used < cap → VLM; else fail this page with `OCR_VLM_BUDGET_EXCEEDED` (NN-6)

### Classifier

- `app/ingest/classifier.py` — `classify_page(image: np.ndarray) -> PageType`:
  - PDF has text layer → `NATIVE`
  - Laplacian variance < threshold → `BLURRY_SCAN`
  - Stroke direction ratio (vertical+horizontal vs. diagonal) → `HANDWRITING_LIKELY`
  - Default → `CLEAN_SCAN`
  - Returns confidence in the classification too — used for "when unsure, default to PaddleOCR with preprocess"

### Orchestration

- `app/ingest/service.py` — extend with `async ocr_document(document_id)`:
  - Loads document + its pages from storage (if PDF, rasterizes per page via `pdf2image`; caches per-page images)
  - For each page: classify → route → call provider; if PaddleOCR confidence < threshold → consider VLM escalation (subject to per-doc cap)
  - Persist `Page` and `Span` rows transactionally per page (don't lose work on partial failure)
  - Updates `documents.vlm_pages_used` after each VLM call
  - Updates `documents.status` through `ocr_running → ocr_done`
  - Bumps `last_event_seq` and writes a `document_events` row per status change

### Worker handler

- `app/jobs/kinds.py` — register the OCR handler that calls `IngestService.ocr_document(payload["document_id"])`. On success, enqueue a LAYOUT job. On failure, set `status='failed'` with `error_code` from the exception.

### Budget integration

- The VLM provider call in `vlm_ocr.py` goes through the `LLMRouter`, which already enforces the global budget from M2. The **per-document** cap is enforced in `routing.py` before the call — that's where the `documents.vlm_pages_used` counter is checked.

### Sample documents

- `tests/fixtures/docs/native_clean.pdf` — generate via `reportlab` script in `scripts/generate_fixtures.py`. Includes a fake "Pearson Specter Litt" case caption, parties, dates.
- `tests/fixtures/docs/scan_clean.pdf` — render the native PDF as 200dpi images, re-PDF-them (simulates a fresh scan)
- `tests/fixtures/docs/scan_skewed.pdf` — rotate 3° before re-PDF-ing
- `tests/fixtures/docs/handwriting_excerpt.pdf` — handwritten note image embedded in a PDF page (use a public-domain handwritten image from `tests/fixtures/raw/`; or synthesize with Pillow + a handwriting font for a deterministic build)
- `tests/fixtures/docs/multi_column.pdf` — two-column layout, otherwise clean
- `tests/fixtures/docs/corrupt.pdf` — a 100-byte garbage file
- `scripts/generate_fixtures.py` — the script that produces all of these from raw assets. Idempotent.

### Tests

- `tests/integration/test_ocr_native.py` — upload `native_clean.pdf`; assert pdfplumber path; spans have correct text; mean_confidence is 1.0
- `tests/integration/test_ocr_scan.py` — upload `scan_clean.pdf`; assert PaddleOCR path; mean_confidence > 0.85; key strings (parties, case caption) appear in extracted text
- `tests/integration/test_ocr_skewed.py` — upload `scan_skewed.pdf`; assert preprocess deskew runs; final confidence acceptable
- `tests/integration/test_ocr_vlm_budget.py` (NN-6) — set `MAX_VLM_PAGES_PER_DOC=2`; upload a document where every page is classified as handwriting; assert exactly 2 VLM calls happen; remaining pages have `status='vlm_budget_exceeded'`; document still reaches `ocr_done` with a warning
- `tests/integration/test_ocr_corrupt.py` — upload `corrupt.pdf`; assert document transitions to `failed` with a typed error; no spans persisted
- `tests/unit/test_classifier.py` — feed each fixture's first page through `classify_page` and assert expected category
- `tests/unit/test_preprocess.py` — feed a skewed image; assert output is deskewed within 0.5°

## Acceptance criteria

- [ ] All five OCR fixture documents reach `status='ocr_done'` (or `failed` for corrupt) after `make seed`
- [ ] `make bench-ocr` prints mean confidence per fixture
- [ ] VLM cap test green
- [ ] Spans carry `(page, bbox, confidence, source)` in every code path
- [ ] No VLM call happens for native PDFs (cost discipline)
- [ ] PaddleOCR model is lazy-loaded once per worker process (not per page)

## Out of scope

- Layout parsing (next milestone, M5)
- Chunking, embeddings
- DOCX path — leave the file `app/ingest/ocr/docx.py` empty with a TODO; legal documents are overwhelmingly PDFs and DOCX is a rare path

## Definition of done

`make seed && make bench-ocr` shows confidences across the fixtures; the VLM budget test passes; the corrupt-file path doesn't poison the queue. `M4-DONE.md` written with the exact CER on each fixture.

## Sub-agent delegation

**Yes, recommended.** After `app/ingest/ocr/base.py` and `routing.py` skeleton are merged:

- Sub-agent A: `pdfplumber_ocr.py` + native test fixture + test
- Sub-agent B: `paddle_ocr.py` + scan test fixture + test
- Sub-agent C: `preprocess.py` + skewed test fixture + test
- Sub-agent D: `vlm_ocr.py` + budget test (uses MockProvider via the router)
- Sub-agent E: `classifier.py` + classifier unit tests

The five providers are independent. The orchestration in `service.py` integrates them after all five are merged.
