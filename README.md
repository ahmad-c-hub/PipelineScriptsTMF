# TMF Pipeline

An automated pipeline for extracting structured data from USCIS immigration case files (O-1A, EB-1A, EB-2 NIW, etc.) using an LLM, with results saved to Google Firestore.

---

## What It Does

For each immigration application folder, the pipeline:

1. **Classifies and renames** raw PDFs into standard document types (petition, decision, RFE, evidence, etc.)
2. **Converts PDFs to markdown** using a GCS-hosted parser
3. **Extracts structured fields** from each document via LLM inference (petition metadata, decision outcome, evidence signals)
4. **Saves results to Firestore** under `insights` and `inference_logs` collections
5. **Aggregates** all results into `aggregated_cases` and `aggregated_insights` collections, pre-computed across all filter combinations (petition type, service center, filing year, officer)

---

## Project Structure

```
PipelineScriptsTMF-main/
├── config.py              # ← Edit this before each run: RUNPOD_URL and RUNPOD_MODEL
├── setup.py               # Run once — installs deps, creates dirs, downloads parser
├── run_all.py             # Run each time — processes all applications end to end
├── pipeline.py            # Core logic: client setup, PDF parsing, LLM extraction, Firestore save
├── naming.py              # LLM-based PDF classifier and renamer
├── aggregate.py           # Transforms and aggregates Firestore results
├── requirements.txt       # Python dependencies
├── colab-key-new.json     # GCP service account key (YOU must add this — not in repo)
├── pdf_to_markdown.py     # Downloaded automatically by setup.py from GCS
├── applications/          # Created by setup.py — put your case folders here
│   ├── john-doe-o1a/      # One subfolder per application (any name)
│   │   ├── some_petition.pdf
│   │   ├── approval_notice.pdf
│   │   └── expert_letter.pdf
│   └── smith-eb1/
│       └── ...
└── logs/                  # Created by setup.py
```

---

## First-Time Setup

### 1. Add your GCP service account key

Place `colab-key-new.json` in the project root. This key is used to:
- Download `pdf_to_markdown.py` from GCS
- Read/write to Firestore

### 2. Run setup

```bash
python setup.py
```

This will:
- Install all dependencies from `requirements.txt`
- Create the `applications/` and `logs/` directories
- Download `pdf_to_markdown.py` from GCS and verify it

Only needs to be run once.

---

## Running the Pipeline

### 1. Add your application folders

Place one subfolder per case inside `applications/`. Folder names can be anything. Drop the raw PDFs inside — any filenames are fine, the pipeline will classify and rename them automatically.

```
applications/
├── john-doe-o1a/
│   ├── AH Path O1A USCIS Forms.pdf
│   ├── I797A Approval Notice.pdf
│   └── Expert Letter - John Smith.pdf
└── smith-eb1/
    ├── NIW Filing.pdf
    └── Denial Notice.pdf
```

### 2. Run

```bash
python run_all.py
```

Applications are processed **one at a time**, in alphabetical order. Each application goes through all three steps before the next one starts.

---

## Pipeline Steps (per application)

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

Each renamed PDF is converted to markdown (page by page) using `pdf_to_markdown.py`.

### Step 3 — LLM Extraction (`pipeline.py → run_pipeline`)

Each document is processed page by page with a targeted prompt:

- **Petition prompt** — extracts petition type, filing date, service center, premium processing, beneficiary country, industry, criteria claimed, etc.
- **Decision prompt** — extracts decision date, outcome, officer number, RFE/NOID flags and basis, discretion applied, etc.
- **Evidence prompt** — extracts publication signals, citation quality, basis for application, RFE/NOID linkage

Fields are merged across pages (first non-null value wins). Results are saved to Firestore `insights` collection under a timestamped `run_id`.

### Step 4 — Aggregation (`aggregate.py`, auto-called)

Called automatically at the end of Step 3:

- `transform_and_save` → writes one structured document per application to `aggregated_cases`
- `aggregate_and_save` → reads all cases for the run, computes stats across every combination of petition type / service center / filing year / officer, writes to `aggregated_insights`

---

## Firestore Collections

| Collection | Contents |
|---|---|
| `insights` | Raw LLM extraction results, one document per run |
| `inference_logs` | Token and timing stats per run |
| `aggregated_cases` | One structured record per application per run |
| `aggregated_insights` | Pre-computed aggregate stats and filter slices per run |

---

## Configuration

### LLM endpoint (`config.py`)

`RUNPOD_URL` and `RUNPOD_MODEL` live in **`config.py`** — the only file you need to edit between runs when you spin up a new RunPod pod:

```python
# config.py
RUNPOD_URL   = "https://<your-pod-id>-11434.proxy.runpod.net/"
RUNPOD_MODEL = "org/qwen2.5-1m:14b"
```

Both `pipeline.py` and `naming.py` import from this file, so changing it here propagates everywhere automatically.

### Other constants

All remaining config is at the top of `pipeline.py` and `setup.py`:

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
| `google-cloud-storage` | Download parser from GCS |
| `google-cloud-secret-manager` | Access AES key from Secret Manager |
| `google-cloud-firestore` | Save results to Firestore |
| `cryptography` | Encryption utilities |
| `python-dateutil` | Date parsing in aggregation |
