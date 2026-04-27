
import re, json, time, os, subprocess, importlib.util, yaml
from datetime import datetime, timezone
from openai import OpenAI
import ollama
from google.oauth2 import service_account
from google.cloud import storage as gcs, secretmanager, firestore
from config import RUNPOD_URL, RUNPOD_MODEL

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

COLAB_KEY_PATH   = os.path.join(BASE_DIR, "colab-key-new.json")
GCP_PROJECT      = "data-enclave-dev-488920"
FIREBASE_BUCKET  = "moonlit-balm-489206-i9.firebasestorage.app"
SECRET_NAME      = f"projects/{GCP_PROJECT}/secrets/enclave-aes-key/versions/latest"
FIRESTORE_DB     = "llm-output"

APPLICATIONS_DIR = os.path.join(BASE_DIR, "applications")
PDF_TO_MD_SCRIPT = os.path.join(BASE_DIR, "pdf_to_markdown.py")
LOG_DIR          = os.path.join(BASE_DIR, "logs")

# ── CLIENTS ───────────────────────────────────────────────────
with open(COLAB_KEY_PATH) as f:
    _key = json.load(f)

_creds           = service_account.Credentials.from_service_account_info(_key)
gcs_client       = gcs.Client(credentials=_creds, project=GCP_PROJECT)
sm_client        = secretmanager.SecretManagerServiceClient(credentials=_creds)
firestore_client = firestore.Client(credentials=_creds, project=GCP_PROJECT, database=FIRESTORE_DB)

llm    = OpenAI(api_key="any", base_url=RUNPOD_URL)
client = ollama.Client(host=RUNPOD_URL)

print("✓ All clients ready.")
print(f"  RunPod : {RUNPOD_URL}")
print(f"  Model  : {RUNPOD_MODEL}")
print(f"  GCP    : {GCP_PROJECT}")

# ── PROMPTS ───────────────────────────────────────────────────
_PROMPTS_PATH = os.path.join(BASE_DIR, "insight_prompts.yaml")
with open(_PROMPTS_PATH, "r", encoding="utf-8") as _f:
    _PROMPTS = yaml.safe_load(_f)

PROMPT_PETITION = _PROMPTS["petition"]
PROMPT_DECISION = _PROMPTS["decision"]
PROMPT_EVIDENCE = _PROMPTS["evidence"]
PROMPT_RFE      = _PROMPTS["rfe"]

# ── PARSER RUNNER ─────────────────────────────────────────────
def run_parser(pdf_path):
    md_path = pdf_path.replace(".pdf", ".md")
    subprocess.run(
        ["python", PDF_TO_MD_SCRIPT, pdf_path, md_path],
        check=True
    )
    with open(md_path, "r", encoding="utf-8") as f:
        return f.read()


def build_app_markdowns(app_name, app_folder, skip_doc_types=None):
    """Parse PDFs in app_folder → {doc_type: markdown_text}.
    skip_doc_types — set of doc types to skip (already in processed_applications)."""
    skip = set(skip_doc_types or [])
    print(f"\n[{app_name}] Parsing documents:")
    docs_md = {}
    for f in sorted(os.listdir(app_folder)):
        if not f.lower().endswith(".pdf"):
            continue
        doc_type = f.replace(".pdf", "")
        if doc_type in skip:
            print(f"  {doc_type:20s} → skipped (already processed)")
            continue
        pdf_path = os.path.join(app_folder, f)
        try:
            md_text = run_parser(pdf_path)
            docs_md[doc_type] = md_text
            print(f"  {doc_type:20s} → {md_text.count('## Page')} pages, {len(md_text):,} chars")
        except Exception as e:
            print(f"  {doc_type:20s} → ERROR: {e}")
            docs_md[doc_type] = None
    return docs_md


def build_markdowns(applications_dir):
    all_markdowns = {}
    for app_name in sorted(os.listdir(applications_dir)):
        if app_name.startswith("."):
            continue
        app_folder = os.path.join(applications_dir, app_name)
        if not os.path.isdir(app_folder):
            continue
        all_markdowns[app_name] = build_app_markdowns(app_name, app_folder)
    print("\nMarkdowns ready:")
    for app, docs in all_markdowns.items():
        print(f"\n  {app}:")
        for doc_type, md in docs.items():
            status = f"{md.count('## Page')} pages" if md else "FAILED"
            print(f"    {doc_type:20s} → {status}")
    return all_markdowns


# ── INFERENCE HELPERS ─────────────────────────────────────────
def split_by_page(md_text):
    pages = re.split(r'(?=## Page \d+)', md_text)
    return [p.strip() for p in pages if p.strip() and '## Page' in p]


def extract_first_json(s):
    start = s.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return s[start:i+1]
    return None


def run_page(prompt, page_text, page_idx, doc_type):
    full_prompt = prompt + "\n\n" + page_text
    print(f"[{doc_type}] Page {page_idx} → sending...")
    start = time.time()
    response = client.generate(
        model=RUNPOD_MODEL,
        prompt=full_prompt,
        stream=False,
        options={"temperature": 0, "num_predict": 2000}
    )
    elapsed = time.time() - start
    output  = response['response']
    input_tokens  = response.get('prompt_eval_count', 0)
    output_tokens = response.get('eval_count', 0)
    print(f"[{doc_type}] Page {page_idx} done ({elapsed:.1f}s)")
    return output, elapsed, input_tokens, output_tokens


def process_document(doc_type, md_text, docs):
    pages = split_by_page(md_text)
    if doc_type == "petition":
        prompt = PROMPT_PETITION
    elif doc_type == "decision":
        prompt = PROMPT_DECISION
    elif doc_type == "rfe":
        prompt = PROMPT_RFE
    else:
        prompt = PROMPT_EVIDENCE

    merged = {}
    stats  = {"pages": 0, "time": 0, "input_tokens": 0, "output_tokens": 0}

    for i, page in enumerate(pages, 1):
        result_text, elapsed, in_tok, out_tok = run_page(prompt, page, i, doc_type)
        stats["pages"]        += 1
        stats["time"]         += elapsed
        stats["input_tokens"] += in_tok
        stats["output_tokens"]+= out_tok

        raw = extract_first_json(result_text)
        if raw:
            try:
                parsed     = json.loads(raw)
                found_keys = []
                for k, v in parsed.items():
                    if v is not None:
                        found_keys.append(k)
                        if k not in merged:
                            merged[k] = v
                if found_keys:
                    print(f"[{doc_type}] Page {i} → Found: {found_keys}")
                else:
                    print(f"[{doc_type}] Page {i} → No useful fields")
            except Exception as e:
                print(f"[{doc_type}] Page {i} → Parse error")
        else:
            print(f"[{doc_type}] Page {i} → No JSON")

    return merged, stats


# ── GCS APPLICATION FETCHER ───────────────────────────────────
def list_gcs_applications():
    """Return sorted list of application folder names found in gs://{bucket}/applications/."""
    bucket = gcs_client.bucket(FIREBASE_BUCKET)
    blobs  = gcs_client.list_blobs(bucket, prefix="applications/", delimiter="/")
    # Consume the iterator so prefixes are populated
    list(blobs)
    app_names = []
    for prefix in blobs.prefixes:
        # prefix looks like "applications/application-001/"
        folder = prefix.rstrip("/").split("/")[-1]
        if folder:
            app_names.append(folder)
    return sorted(app_names)


def fetch_applications_from_gcs(app_names):
    """Download PDFs for the given app names from GCS into the local applications/ dir.
    Clears the local folder first so renamed/parsed files from previous runs don't interfere."""
    import shutil
    bucket = gcs_client.bucket(FIREBASE_BUCKET)
    for app_name in app_names:
        local_app_dir = os.path.join(APPLICATIONS_DIR, app_name)
        if os.path.exists(local_app_dir):
            shutil.rmtree(local_app_dir)
        os.makedirs(local_app_dir)
        prefix = f"applications/{app_name}/"
        blobs  = list(gcs_client.list_blobs(bucket, prefix=prefix))
        pdfs   = [b for b in blobs if b.name.lower().endswith(".pdf")]
        if not pdfs:
            print(f"  [{app_name}] No PDFs found in GCS at {prefix}")
            continue
        for blob in pdfs:
            filename  = blob.name.split("/")[-1]
            dest_path = os.path.join(local_app_dir, filename)
            blob.download_to_filename(dest_path)
            print(f"  [{app_name}] Downloaded: {filename}")


# ── PROCESSED APPLICATIONS TABLE ──────────────────────────────
def get_processed_application(app_name):
    """
    Return {"processed_docs": [doc_type, ...], "fields": {merged fields}}
    for an application, or {"processed_docs": [], "fields": {}} if not found.
    processed_docs — which document types have been processed (for deduplication).
    fields         — running merged result of all processed documents.
    """
    doc = firestore_client.collection("processed_applications").document(app_name).get()
    if not doc.exists:
        return {"processed_docs": [], "fields": {}}
    data = doc.to_dict()
    return {
        "processed_docs": data.get("processed_docs", []),
        "fields":         data.get("fields", {}),
    }


def update_processed_application(app_name, doc_type, updated_merged_fields, run_id):
    """
    After processing a single document type:
    - Appends doc_type to processed_docs (via ArrayUnion — safe against concurrent writes).
    - Replaces fields with the latest merged result.
    Called immediately after each document is processed for crash-safe tracking.
    """
    firestore_client.collection("processed_applications").document(app_name).set({
        "processed_docs": firestore.ArrayUnion([doc_type]),
        "fields":         updated_merged_fields,
        "run_id":         run_id,
        "last_updated":   datetime.now(timezone.utc).isoformat(),
    }, merge=True)
    print(f"  → processed_applications/{app_name}: added '{doc_type}'")


# ── MAIN RUN LOOP ─────────────────────────────────────────────
def run_pipeline(all_markdowns):
    """
    all_markdowns — {app_name: {doc_type: md_text}}
    For each app, checks processed_applications in Firestore per document type:
      - Already processed → reuse stored fields, skip inference
      - New document      → run inference, save to processed_applications
    All results (new + reused) are saved together under a new run_id in insights.
    """
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    all_results   = {}
    all_app_stats = {}
    global_stats  = {
        "total_pages_processed":    0,
        "total_inference_time_sec": 0,
        "total_input_tokens":       0,
        "total_output_tokens":      0,
    }

    def _accumulate(target, stats):
        target["pages_processed"]    += stats["pages"]
        target["inference_time_sec"] += stats["time"]
        target["input_tokens"]       += stats["input_tokens"]
        target["output_tokens"]      += stats["output_tokens"]

    print(f"\nRUN ID: {run_id}")
    print("="*60)

    for app, docs in all_markdowns.items():
        print(f"\nAPPLICATION: {app}")
        print("-"*60)

        # Load what has already been processed for this app
        processed_record = get_processed_application(app)
        processed_docs   = set(processed_record["processed_docs"])

        if processed_docs:
            already = [dt for dt in docs if dt in processed_docs]
            new     = [dt for dt in docs if dt not in processed_docs]
            print(f"  Already processed ({len(already)}): {already}")
            print(f"  New documents     ({len(new)}):     {new}")
        else:
            print("  No prior record — processing all documents.")

        # Seed final with the stored merged fields from previous runs
        final     = dict(processed_record["fields"])
        app_stats = {"pages_processed": 0, "inference_time_sec": 0, "input_tokens": 0, "output_tokens": 0}

        def _process_or_reuse(doc_type, md_text, prompt_key, label):
            """Returns extracted fields if newly processed, None if reused."""
            if doc_type in processed_docs:
                print(f"\n=== REUSING {label} (already in processed_applications) ===")
                return None
            print(f"\n=== PROCESSING {label} ===")
            data, stats = process_document(prompt_key, md_text, docs)
            print(f"\n{label} RESULT:")
            print(json.dumps(data, indent=2))
            _accumulate(app_stats, stats)
            return data

        # ── Process in priority order; petition overwrites, others fill gaps ──
        if "petition" in docs and docs["petition"] is not None:
            data = _process_or_reuse("petition", docs["petition"], "petition", "PETITION")
            if data is not None:
                final.update(data)
                update_processed_application(app, "petition", final, run_id)

        if "petition_2" in docs and docs["petition_2"] is not None:
            data = _process_or_reuse("petition_2", docs["petition_2"], "petition", "PETITION_2")
            if data is not None:
                final.update({k: v for k, v in data.items() if k not in final})
                update_processed_application(app, "petition_2", final, run_id)

        if "rfe" in docs and docs["rfe"] is not None:
            data = _process_or_reuse("rfe", docs["rfe"], "rfe", "RFE")
            if data is not None:
                final.update({k: v for k, v in data.items() if k not in final})
                update_processed_application(app, "rfe", final, run_id)

        if "decision" in docs and docs["decision"] is not None:
            data = _process_or_reuse("decision", docs["decision"], "decision", "DECISION")
            if data is not None:
                final.update({k: v for k, v in data.items() if k not in final})
                update_processed_application(app, "decision", final, run_id)

        for doc_type, md_text in docs.items():
            if doc_type.startswith("evidence") and md_text is not None:
                data = _process_or_reuse(doc_type, md_text, "evidence", f"EVIDENCE ({doc_type})")
                if data is not None:
                    final.update({k: v for k, v in data.items() if k not in final})
                    update_processed_application(app, doc_type, final, run_id)

        # All doc types = newly parsed + previously processed (for flags and _doc_types)
        all_known_doc_types = processed_docs | set(docs.keys())

        # Derived flags — always from file presence, not LLM output
        final["rfe_linked_to_evidence_issue"]  = any(k.startswith("rfe")  for k in all_known_doc_types)
        final["noid_linked_to_evidence_issue"] = any(k.startswith("noid") for k in all_known_doc_types)

        all_results[app]   = final
        all_app_stats[app] = app_stats

        print(f"\n=== [{app}] STATS ===")
        print(f"  Pages processed  : {app_stats['pages_processed']}")
        print(f"  Inference time   : {app_stats['inference_time_sec']:.1f}s")
        print(f"  Input tokens     : {app_stats['input_tokens']:,}")
        print(f"  Output tokens    : {app_stats['output_tokens']:,}")
        newly_processed = [dt for dt in docs if dt not in processed_docs]
        print(f"  New docs saved   : {newly_processed or 'none (all reused)'}")

        print("\n=== FINAL MERGED RESULT ===")
        print(json.dumps(final, indent=2))

        for key in ("pages_processed", "inference_time_sec", "input_tokens", "output_tokens"):
            global_stats[f"total_{key}"] += app_stats[key]

    # ── Attach doc types (new + previously processed) ─────────
    for app, docs in all_markdowns.items():
        if app in all_results:
            prev = set(get_processed_application(app)["processed_docs"])
            all_results[app]["_doc_types"] = sorted(prev | set(docs.keys()))

    # ── Only apps with new inference this run ────────────────
    new_results   = {app: r for app, r in all_results.items()   if all_app_stats[app]["pages_processed"] > 0}
    new_app_stats = {app: s for app, s in all_app_stats.items() if s["pages_processed"] > 0}

    # ── Save to Firestore ─────────────────────────────────────
    print("\nSaving to Firestore...")
    print(f"  New apps this run: {list(new_results.keys()) or 'none'}")
    firestore_client.collection("insights").document(run_id).set({
        "run_id":                run_id,
        "timestamp":             datetime.now(timezone.utc).isoformat(),
        "model":                 RUNPOD_MODEL,
        "applications":          new_results,
        "total_fields_extracted": sum(len(v) for v in new_results.values()),
    })
    firestore_client.collection("inference_logs").document(run_id).set({
        "run_id": run_id,
        **global_stats,
        "applications": new_app_stats,
    })
    print(f"Saved → Firestore (run_id={run_id})")

    # ── Transform + Aggregate ─────────────────────────────────
    spec = importlib.util.spec_from_file_location("aggregate", os.path.join(BASE_DIR, "aggregate.py"))
    agg  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agg)

    agg.transform_and_save(firestore_client, run_id, all_results, RUNPOD_MODEL)
    agg.aggregate_and_save(firestore_client, run_id, RUNPOD_MODEL)

    return all_results
