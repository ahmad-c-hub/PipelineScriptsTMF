
import re, json, time, os, subprocess, importlib.util
from datetime import datetime, timezone
from openai import OpenAI
import ollama
from google.oauth2 import service_account
from google.cloud import storage as gcs, secretmanager, firestore

# ── CONFIG ────────────────────────────────────────────────────
RUNPOD_URL   = "https://s70wgsir2yx37v-11434.proxy.runpod.net/"
RUNPOD_MODEL = "Qwen/Qwen2.5-14B-Instruct-1M-AWQ"

COLAB_KEY_PATH   = "/content/colab-key-new.json"
GCP_PROJECT      = "data-enclave-dev-488920"
FIREBASE_BUCKET  = "moonlit-balm-489206-i9.firebasestorage.app"
SECRET_NAME      = f"projects/{GCP_PROJECT}/secrets/enclave-aes-key/versions/latest"
FIRESTORE_DB     = "llm-output"

APPLICATIONS_DIR = "/content/pipeline/applications"
PDF_TO_MD_SCRIPT = "/content/pipeline/pdf_to_markdown.py"
LOG_DIR          = "/content/pipeline/logs"

os.makedirs(APPLICATIONS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

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

# ── DOWNLOAD PARSER ───────────────────────────────────────────
bucket = gcs_client.bucket(FIREBASE_BUCKET)
print("Downloading parser...")
bucket.blob("pipeline/pdf_to_markdown.py").download_to_filename(PDF_TO_MD_SCRIPT)
print(f"✓ Parser saved to {PDF_TO_MD_SCRIPT}")
result = subprocess.run(
    ["python", "-c", f"import ast; ast.parse(open('{PDF_TO_MD_SCRIPT}').read()); print('Syntax OK')"],
    capture_output=True, text=True
)
print(f"  {result.stdout.strip() or result.stderr.strip()}")

# ── PROMPTS ───────────────────────────────────────────────────
PROMPT_PETITION = """
You are an immigration document analyst. You will be given the text of an immigration petition (Form I-129 or I-140).

Extract the following fields and return ONLY a valid JSON object with snake_case keys. If a field is not found or not determinable, set its value to null.

Fields to extract:
{
  "petition_type": "string (e.g. O-1A, EB-1A, EB-2 NIW)",
  "filing_date": "YYYY-MM-DD",
  "service_center_or_field_office": "string",
  "premium_processing": "Y | N | Unknown",
  "employer_sponsored": true | false,
  "self_petition": true | false,
  "beneficiary_country_of_birth": "string",
  "beneficiary_country_of_citizenship": "string",
  "beneficiary_industry": "Tech | Pharmaceutical | Academic | Financial | Manufacturing | Other",
  "criteria_claimed_count": integer,
  "major_award_claimed": "Y | N",
  "publications_included": "Y | N",
  "judging_others_work_claimed": "Y | N",
  "high_salary_claimed": "Y | N",
  "eb2_basis": "Advanced Degree | Exceptional Ability"
  "endeavor_field": "Health | Energy | Tech | Public Policy | Other"
  }
Document:
"""

PROMPT_DECISION = """
You are analyzing a USCIS decision notice.

This is NOT a form — only narrative text.

INSTRUCTIONS:
- Scan entire text for key signals
- DO NOT infer missing values
- Extract ONLY explicit information
- Return ONLY JSON

KEY PATTERNS:

decision_date:
  Look for:
  - Notice Date
  - Date at top of letter

final_outcome:
  Approved → approval notice
  Denied → denial notice
  Withdrawn → explicitly stated

officer_number:
  Look for ANY of these patterns:
  - "Officer:" followed by a number or code
  - "Adjudicator:" followed by a number or code
  - "Officer ID:" or "ID:" in a stamp or signature block
  - A standalone alphanumeric code near a signature (e.g. "A12345678")
  - Initials + number combinations in the footer or header
  If found, return the exact code/number as a string.
  If not found, return null.

rfe_issued:
  true if "Request for Evidence" is explicitly mentioned
  false otherwise

noid_issued:
  true if "Notice of Intent to Deny" is explicitly mentioned
  false otherwise

rfe_basis:
  Only fill if rfe_issued is true.
  Extract the MAIN stated reason for the RFE. Choose ONE:
    "Lack of independent corroboration"
    "Evidence insufficient"
    "Lack of originality"
    "General claims"
    "Evidence does not meet legal standard"
    "Overreliance on expert letters"
    "Other"
  If rfe_issued is false, return null.

noid_basis:
  Only fill if noid_issued is true.
  Extract the MAIN stated reason for the NOID. Choose ONE:
    "Lack of independent corroboration"
    "Evidence insufficient"
    "Lack of originality"
    "General claims"
    "Evidence does not meet legal standard"
    "Inconsistencies or contradictions"
    "Other"
  If noid_issued is false, return null.

basis_for_approval:
  Extract key justification text if approved.

primary_basis_for_decision:
  Choose ONE:
    Major Award
    High Salary
    Publications
    Judging
    Critical Role
    Other

discretion_applied:
  true if language like "in our discretion" or "USCIS determines" appears.

outcome_after_rfe:
  If rfe_issued is true, what was the final outcome after the RFE response?
  "approved" | "denied" | null

outcome_after_noid:
  If noid_issued is true, what was the final outcome after the NOID response?
  "approved" | "denied" | null

Return EXACT JSON:
{
  "decision_date": null,
  "final_outcome": null,
  "officer_number": null,
  "rfe_issued": null,
  "noid_issued": null,
  "rfe_basis": null,
  "noid_basis": null,
  "basis_for_approval": null,
  "primary_basis_for_decision": null,
  "discretion_applied": null,
  "outcome_after_rfe": null,
  "outcome_after_noid": null
}

Document:
"""

PROMPT_EVIDENCE = """
You are analyzing a LARGE USCIS evidence package (hundreds of pages).

IMPORTANT:
- This is NOT a form
- This is NOT structured
- You MUST scan for SIGNALS, not summarize

VERY IMPORTANT LOGIC:

Two fields are NOT derived from the evidence text:

- rfe_linked_to_evidence_issue
- noid_linked_to_evidence_issue

These are determined externally:

- If an RFE document exists in the application → rfe_linked_to_evidence_issue = true
- If no RFE document exists → false

- If a NOID document exists in the application → noid_linked_to_evidence_issue = true
- If no NOID document exists → false

You will be given these flags explicitly:

RFE_PRESENT: __RFE_PRESENT__
NOID_PRESENT: __NOID_PRESENT__

You MUST:
- Use these values directly
- DO NOT infer them from text

────────────────────────────

STRATEGY:
1. Scan document for keywords and patterns
2. Detect relevant sections
3. Extract ONLY final signals

DO NOT:
- Summarize
- Copy text
- Infer anything not clearly stated

────────────────────────────

TARGET SIGNALS:

included_publications:
  true if you see:
    - journal articles
    - conference papers
    - arXiv / preprints
    - Google Scholar references

cell_cited:
  true ONLY if:
    - "Cell" journal is explicitly mentioned

publication_type:
  Look at what kind of publications are included.
  Choose the HIGHEST quality type present:
    "peer_reviewed_top_tier"     → Cell, Nature, Science, NEJM, Lancet
    "peer_reviewed_other"        → Any other peer-reviewed journal article
    "conference_paper"           → Conference proceedings, presented papers
    "preprint"                   → arXiv, bioRxiv, SSRN, preprints
    "trade_publication"          → Industry magazines, trade journals
    "blog_or_whitepaper"         → Blog posts, internal whitepapers, technical memos
  If no publications: null

basis_for_application:
  Identify dominant basis from repeated patterns:
    - Major Award
    - High Salary
    - Publications
    - Judging
    - Critical Role
    - Original Contributions

────────────────────────────

FINAL OUTPUT RULES:

- rfe_linked_to_evidence_issue = value of RFE_PRESENT
- noid_linked_to_evidence_issue = value of NOID_PRESENT

DO NOT change these values.

────────────────────────────

Return EXACT JSON:
{
  "included_publications": null,
  "cell_cited": null,
  "publication_type": null,
  "basis_for_application": null,
  "rfe_linked_to_evidence_issue": null,
  "noid_linked_to_evidence_issue": null
}

Document:
"""

# ── PARSER RUNNER ─────────────────────────────────────────────
def run_parser(pdf_path):
    md_path = pdf_path.replace(".pdf", ".md")
    subprocess.run(
        ["python", PDF_TO_MD_SCRIPT, pdf_path, md_path],
        check=True
    )
    with open(md_path, "r", encoding="utf-8") as f:
        return f.read()


def build_markdowns(applications_dir):
    all_markdowns = {}
    for app_name in sorted(os.listdir(applications_dir)):
        if app_name.startswith("."):
            continue
        app_folder = os.path.join(applications_dir, app_name)
        if not os.path.isdir(app_folder):
            continue
        print(f"\n[{app_name}] Parsing documents:")
        docs_md = {}
        for f in sorted(os.listdir(app_folder)):
            if not f.lower().endswith(".pdf"):
                continue
            doc_type = f.replace(".pdf", "")
            pdf_path = os.path.join(app_folder, f)
            try:
                md_text = run_parser(pdf_path)
                docs_md[doc_type] = md_text
                print(f"  {doc_type:20s} → {md_text.count('## Page')} pages, {len(md_text):,} chars")
            except Exception as e:
                print(f"  {doc_type:20s} → ERROR: {e}")
                docs_md[doc_type] = None
        all_markdowns[app_name] = docs_md
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
        model='org/qwen2.5-1m:14b',
        prompt=full_prompt,
        stream=False,
        options={"temperature": 0, "num_predict": 2000}
    )
    elapsed = time.time() - start
    output  = response['response']
    input_tokens  = len(full_prompt) // 4
    output_tokens = len(output) // 4
    print(f"[{doc_type}] Page {page_idx} done ({elapsed:.1f}s)")
    return output, elapsed, input_tokens, output_tokens


def process_document(doc_type, md_text, docs):
    pages = split_by_page(md_text)
    if doc_type == "petition":
        prompt = PROMPT_PETITION
    elif doc_type == "decision":
        prompt = PROMPT_DECISION
    else:
        rfe_present  = "rfe" in docs
        noid_present = "noid" in docs
        prompt = PROMPT_EVIDENCE.replace(
            "__RFE_PRESENT__",  str(rfe_present).lower()
        ).replace(
            "__NOID_PRESENT__", str(noid_present).lower()
        )

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


# ── MAIN RUN LOOP ─────────────────────────────────────────────
def run_pipeline(all_markdowns):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    all_results  = {}
    global_stats = {
        "total_pages_processed":    0,
        "total_inference_time_sec": 0,
        "total_input_tokens":       0,
        "total_output_tokens":      0
    }

    print(f"\nRUN ID: {run_id}")
    print("="*60)

    for app, docs in all_markdowns.items():
        print(f"\nAPPLICATION: {app}")
        print("-"*60)
        final = {}

        if "petition" in docs:
            print("\n=== PROCESSING PETITION ===")
            data, stats = process_document("petition", docs["petition"], docs)
            print("\nPETITION RESULT:")
            print(json.dumps(data, indent=2))
            final.update(data)
            for k in ("pages","time","input_tokens","output_tokens"):
                global_stats[f"total_{'pages_processed' if k=='pages' else ('inference_time_sec' if k=='time' else k)}"] += stats[k]

        if "decision" in docs:
            print("\n=== PROCESSING DECISION ===")
            data, stats = process_document("decision", docs["decision"], docs)
            print("\nDECISION RESULT:")
            print(json.dumps(data, indent=2))
            final.update({k: v for k, v in data.items() if k not in final})
            for k in ("pages","time","input_tokens","output_tokens"):
                global_stats[f"total_{'pages_processed' if k=='pages' else ('inference_time_sec' if k=='time' else k)}"] += stats[k]

        for k, v in docs.items():
            if k.startswith("evidence"):
                print("\n=== PROCESSING EVIDENCE ===")
                data, stats = process_document("evidence", v, docs)
                print("\nEVIDENCE RESULT:")
                print(json.dumps(data, indent=2))
                final.update({k: v for k, v in data.items() if k not in final})
                for k in ("pages","time","input_tokens","output_tokens"):
                    global_stats[f"total_{'pages_processed' if k=='pages' else ('inference_time_sec' if k=='time' else k)}"] += stats[k]

        all_results[app] = final
        print("\n=== FINAL MERGED RESULT ===")
        print(json.dumps(final, indent=2))

    # ── Attach doc types ──────────────────────────────────────
    for app, docs in all_markdowns.items():
        if app in all_results:
            all_results[app]["_doc_types"] = list(docs.keys())

    # ── Save to Firestore ─────────────────────────────────────
    print("\nSaving to Firestore...")
    firestore_client.collection("insights").document(run_id).set({
        "run_id":    run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model":     "Qwen/Qwen2.5-14B-Instruct-AWQ",
        "applications": all_results,
        "total_fields_extracted": sum(len(v) for v in all_results.values())
    })
    firestore_client.collection("inference_logs").document(run_id).set({
        "run_id": run_id,
        **global_stats
    })
    print(f"Saved → Firestore (run_id={run_id})")

    # ── Transform + Aggregate ─────────────────────────────────
    spec = importlib.util.spec_from_file_location("aggregate", "/content/pipeline/aggregate.py")
    agg  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agg)

    agg.transform_and_save(firestore_client, run_id, all_results, "Qwen/Qwen2.5-14B-Instruct-AWQ")
    agg.aggregate_and_save(firestore_client, run_id, "Qwen/Qwen2.5-14B-Instruct-AWQ")

    return all_results
