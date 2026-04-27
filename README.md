# TMF Pipeline

An automated pipeline for extracting structured data from USCIS immigration case files (O-1A, EB-1A, EB-2 NIW, etc.) using an LLM, with results saved to Google Firestore and surfaced in a React analytics dashboard.

---

## What It Does

For each immigration application folder, the pipeline:

1. **Classifies and renames** raw PDFs into standard document types (petition, decision, RFE, evidence, etc.)
2. **Converts PDFs to markdown** using a GCS-hosted parser
3. **Extracts structured fields** from each document via LLM inference (petition metadata, decision outcome, evidence signals)
4. **Deduplicates across runs** — previously processed documents are reused from `processed_applications`, skipping LLM inference for anything already extracted
5. **Saves per-run results** to `insights` and `inference_logs` (scoped to this run only)
6. **Aggregates the full history** — on every run, all apps ever processed (from `processed_applications`) are written to `aggregated_cases` and aggregated into `aggregated_insights`, so the dashboard always reflects the complete picture

---

## Project Structure

```
PipelineScriptsTMF-main/
├── config.py              # ← Edit this before each run: RUNPOD_URL and RUNPOD_MODEL
├── run_all.py             # ← Single entry point — run this to process all applications
├── setup.py               # Called automatically by run_all.py — installs deps, downloads parser
├── pipeline.py            # Core logic: client setup, PDF parsing, LLM extraction, Firestore save
├── naming.py              # LLM-based PDF classifier and renamer
├── aggregate.py           # Transforms and aggregates Firestore results
├── reaggregate.py         # Re-run aggregation only for a given run_id (no re-extraction)
├── requirements.txt       # Python dependencies
├── insight_prompts.yaml   # LLM prompts for field extraction (petition, decision, evidence, rfe)
├── naming_prompts.yaml    # LLM prompts for PDF classification and renaming
├── colab-key-new.json     # GCP service account key (YOU must add this — not in repo)
├── pdf_to_markdown.py     # Downloaded automatically by setup.py from GCS
├── App.jsx                # React analytics dashboard (frontend)
├── applications/          # Working directory — renamed PDFs + generated .md files land here
└── logs/                  # Per-run log files (auto-created)
```

---

## Setup & Running

There are only two things you need to do manually before running:

### 1. Add your GCP service account key

Place `colab-key-new.json` in the project root. This key is used to access GCS, Secret Manager, and Firestore.

### 2. Upload application folders to GCS

Place one subfolder per case under `gs://<bucket>/applications/`. Folder names can be anything. Drop the raw PDFs inside — any filenames are fine, the pipeline will classify and rename them automatically.

```
gs://<bucket>/applications/
├── john-doe-o1a/
│   ├── AH Path O1A USCIS Forms.pdf
│   ├── I797A Approval Notice.pdf
│   └── Expert Letter - John Smith.pdf
└── smith-eb1/
    ├── NIW Filing.pdf
    └── Denial Notice.pdf
```

### 3. Run

```bash
python run_all.py
```

That's it. `run_all.py` automatically handles the rest:
- Installs all dependencies
- Creates required directories
- Downloads `pdf_to_markdown.py` from GCS
- Fetches all application folders from GCS into `applications/`
- Runs the full pipeline (naming → parsing → extraction → aggregation)

Applications are processed one at a time, in alphabetical order.

---

## Pipeline Steps (per run)

### Step 1 — Naming (`naming.py`)

The LLM reads the raw filenames and maps each PDF to a standard document type, then renames the files in place:

| Standard name | Description |
|---|---|
| `petition.pdf` | Main petition form (I-129, I-140, NIW filing) |
| `decision.pdf` | USCIS decision notice (approval or denial) |
| `rfe.pdf` | Request for Evidence |
| `rfe_response.pdf` | Applicant's RFE response |
| `noid.pdf` | Notice of Intent to Deny |
| `noid_response.pdf` | Applicant's NOID response |
| `evidence_letter_1.pdf` | Expert / recommendation letters (numbered) |
| `evidence_publication_1.pdf` | Journal articles, papers (numbered) |
| `evidence_other_1.pdf` | Awards, pay stubs, patents, etc. (numbered) |

### Step 2 — Parsing (`pipeline.py → build_app_markdowns`)

Each renamed PDF is converted to markdown page by page using `pdf_to_markdown.py`. Document types already recorded in `processed_applications` are skipped — their markdown is not re-generated.

### Step 3 — LLM Extraction (`pipeline.py → run_pipeline`)

Each **new** document is processed page by page with a targeted prompt:

- **Petition prompt** — petition type, filing date, service center, premium processing, beneficiary country, industry, criteria claimed, etc.
- **Decision prompt** — decision date, outcome, officer number, basis for decision, discretion applied
- **RFE prompt** — RFE basis, linkage to evidence issues
- **Evidence prompt** — publication signals, citation quality, Cell citation flag, basis for application

Fields are merged across pages (first non-null value wins). Previously processed documents are reused from `processed_applications` — their fields are merged in without re-running inference. After all documents are processed:

- `processed_applications/{app_name}` is updated with the cumulative merged fields and doc type list
- `insights/{run_id}` is written with this run's extracted applications only
- `inference_logs/{run_id}` is written with token counts and timing for this run

### Step 4 — Aggregation (`aggregate.py`, auto-called)

Called automatically at the end of Step 3. Two sub-steps:

#### 4a. `transform_and_save`

Reads **all** documents from `processed_applications` (the full history of every app ever processed, not just this run). Overrides with `all_results` for apps processed in this run (freshest data). Writes one structured `aggregated_cases` document per app, all tagged with the current `run_id`.

Example — if `processed_applications` has app-001, app-002, app-003 from past runs and this run processed app-004 and app-005:

```
aggregated_cases/app-001_<run_id>  ← from processed_applications (historical)
aggregated_cases/app-002_<run_id>  ← from processed_applications (historical)
aggregated_cases/app-003_<run_id>  ← from processed_applications (historical)
aggregated_cases/app-004_<run_id>  ← from this run's all_results (fresh)
aggregated_cases/app-005_<run_id>  ← from this run's all_results (fresh)
```

#### 4b. `aggregate_and_save`

Reads all `aggregated_cases` for the current `run_id` (which is now all 5 apps above). Pre-computes statistics across every combination of petition type × service center × filing year × officer. Writes a single `aggregated_insights/{run_id}` document containing all pre-computed filter slices.

This means the dashboard always shows analytics over the **entire processed history**, not just the apps from the latest run.

---

## Firestore Collections

| Collection | Scope | Contents |
|---|---|---|
| `processed_applications` | All time | One document per app — cumulative merged fields and processed doc type list. The authoritative deduplication store. |
| `insights` | Per run | Raw LLM extraction results for apps processed in this run only |
| `inference_logs` | Per run | Token counts and timing stats for this run |
| `aggregated_cases` | Per run | One structured record per app — covers all historical apps, tagged with the current run_id |
| `aggregated_insights` | Per run | Pre-computed aggregate stats across all filter combinations, covering the full history as of this run |

---

## Dashboard (App.jsx)

A React dashboard that reads from the API backend (Firestore-backed) and displays two views:

### Analytics Dashboard tab

Reads from `aggregated_insights/{run_id}`. Covers all apps ever processed. Filterable by petition type, service center, filing year, and officer. Five pages:

| Page | What it shows |
|---|---|
| Overview | Approval rate, RFE/NOID rates, processing time, bases for application and approval, year-over-year trends |
| Evidence Risk | Most common RFE/NOID reasons, publication types that trigger RFEs, evidence type breakdown |
| Case Outcomes | Approval and denial rates by adjudication path (RFE → outcome, NOID → outcome, clean → outcome) |
| Officer Review | Per-officer approval rates, RFE/NOID counts, year-by-year performance vs. overall benchmark |
| Cell Citation | Stats for cases citing the journal *Cell* — approval rate, RFE/NOID rate vs. non-Cell cases |

### Case Record tab

Reads from `insights/{run_id}`. Shows only the applications processed in the selected run, with per-app inference stats (pages processed, inference time, token counts) and all extracted fields organized by section (Case Identity, Beneficiary Profile, O-1A Criteria, Decision, Evidence Analysis).

---

## Re-running Aggregation Only

If you change the aggregation logic and want to recompute `aggregated_insights` without re-running extraction:

```bash
python reaggregate.py <run_id>
```

Example:
```bash
python reaggregate.py 20260423_143022
```

This reads `aggregated_cases` for that run and rewrites `aggregated_insights/{run_id}`.

---

## Configuration

### LLM endpoint (`config.py`)

`RUNPOD_URL` and `RUNPOD_MODEL` live in `config.py` — the only file you need to edit between runs when you spin up a new RunPod pod:

```python
# config.py
RUNPOD_URL   = "https://<your-pod-id>-11434.proxy.runpod.net/"
RUNPOD_MODEL = "qwen2.5:14b-instruct-q8_0"
```

Both `pipeline.py` and `naming.py` import from this file, so changing it here propagates everywhere automatically.

### Other constants

All remaining config is at the top of `pipeline.py`:

| Variable | Value |
|---|---|
| `GCP_PROJECT` | `data-enclave-dev-488920` |
| `FIREBASE_BUCKET` | `moonlit-balm-489206-i9.firebasestorage.app` |
| `FIRESTORE_DB` | `llm-output` |

---

## Dependencies

| Package | Purpose |
|---|---|
| `pymupdf` | PDF rendering |
| `pytesseract` | OCR for scanned PDFs |
| `pillow` | Image processing |
| `openai` | OpenAI-compatible LLM client (pointed at RunPod) |
| `ollama` | Ollama client for LLM inference |
| `google-cloud-storage` | Download parser and application PDFs from GCS |
| `google-cloud-secret-manager` | Access AES key from Secret Manager |
| `google-cloud-firestore` | Read and write all Firestore collections |
| `cryptography` | Encryption utilities |
| `python-dateutil` | Date parsing in aggregation |
| `pyyaml` | Load prompt YAML files |
