import os, importlib.util
import yaml
import fitz
from config import RUNPOD_MODEL

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("pdf_to_markdown", os.path.join(_BASE_DIR, "pdf_to_markdown.py"))
_pdf_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pdf_mod)
_is_scanned_page = _pdf_mod._is_scanned_page
_ocr_page        = _pdf_mod._ocr_page

_PROMPTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "naming_prompts.yaml")
with open(_PROMPTS_PATH, "r", encoding="utf-8") as _f:
    _PROMPTS = yaml.safe_load(_f)

NAMING_PROMPT = _PROMPTS["naming"]

SINGLE_TYPES   = {"petition", "rfe", "rfe_response", "noid", "noid_response", "decision"}
EVIDENCE_TYPES = {"evidence_letter", "evidence_publication", "evidence_other"}
MAX_PAGES      = 10


def _extract_preview(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        pages = []
        for i in range(min(MAX_PAGES, doc.page_count)):
            page = doc.load_page(i)
            text = page.get_text("text").strip()
            if _is_scanned_page(page, text):
                ocr_lines = _ocr_page(page)
                text = " ".join(ocr_lines).strip()
            if text:
                pages.append(f"--- Page {i+1} ---\n{text}")
        doc.close()
        return "\n\n".join(pages) if pages else "(no text extracted)"
    except Exception as e:
        return f"(error reading PDF: {e})"


_FILENAME_OVERRIDES = {
    "AHUSSAIN_O1A_Petition_Full - CONFIDENTIAL.pdf": "evidence_other",
}

def _classify_one(filename, pdf_path, client):
    if filename in _FILENAME_OVERRIDES:
        return _FILENAME_OVERRIDES[filename]
    content  = _extract_preview(pdf_path)
    prompt   = NAMING_PROMPT.format(filename=filename, content=content)
    response = client.generate(
        model=RUNPOD_MODEL,
        prompt=prompt,
        stream=False,
        options={"temperature": 0, "num_predict": 20}
    )
    return response['response'].strip().lower().split()[0].strip('.,":\'') if response['response'].strip() else ""


def normalize_application(app_folder, client):
    app_name  = os.path.basename(app_folder)
    raw_files = [f for f in os.listdir(app_folder) if f.lower().endswith(".pdf")]
    if not raw_files:
        print(f"[{app_name}] No PDFs found.")
        return {}

    print(f"\n[{app_name}] Found {len(raw_files)} file(s), classifying:")

    seen_single   = {}
    ev_counters   = {}
    renamed_files = {}

    for original in raw_files:
        pdf_path = os.path.join(app_folder, original)
        standard = _classify_one(original, pdf_path, client)

        if not standard:
            print(f"  {original}  →  (no classification returned, skipping)")
            continue

        base = standard
        for et in EVIDENCE_TYPES:
            if standard.startswith(et):
                base = et
                break

        if base in EVIDENCE_TYPES:
            ev_counters[base] = ev_counters.get(base, 0) + 1
            standard = f"{base}_{ev_counters[base]}"
        elif base in SINGLE_TYPES:
            if base in seen_single:
                standard = f"{base}_2"
            seen_single[base] = True
        else:
            print(f"  {original}  →  unknown type '{standard}', skipping")
            continue

        dst = os.path.join(app_folder, f"{standard}.pdf")
        os.rename(pdf_path, dst)
        renamed_files[standard] = dst
        print(f"  {original}  →  {standard}.pdf")

    return renamed_files


def run_naming(applications_dir, client):
    all_applications = {}
    for app_name in sorted(os.listdir(applications_dir)):
        if app_name.startswith("."):
            continue
        app_folder = os.path.join(applications_dir, app_name)
        if os.path.isdir(app_folder):
            all_applications[app_name] = normalize_application(app_folder, client)
    print("\nNaming done:", {a: list(f.keys()) for a, f in all_applications.items()})
    return all_applications
