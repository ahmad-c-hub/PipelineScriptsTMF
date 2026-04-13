
import os, json

NAMING_PROMPT = """You are a document classifier for US immigration case files.

You will be given a list of filenames belonging to a single immigration application.
Your job is to map each filename to one of the following standard document types:

ALWAYS PRESENT:
- "petition"            → The main petition form (I-129, I-140, I-485, NIW filing forms,
                          USCIS forms packet). Look for: forms, USCIS, I-129, I-140,
                          NIW Filing, petition form
- "decision"            → The final USCIS decision notice (approval or denial).
                          Look for: I-797, approval notice, denial notice, I-797A,
                          Denial Notice, Approval Notice

CONDITIONAL (may or may not be present):
- "rfe"                 → Request for Evidence issued by USCIS.
- "rfe_response"        → Applicant's response to an RFE.
- "noid"                → Notice of Intent to Deny issued by USCIS.
- "noid_response"       → Applicant's response to a NOID.

EVIDENCE:
- "evidence_letter"     → Expert letters, support letters, recommendation letters
- "evidence_publication"→ Journal articles, conference papers, preprints, publications
- "evidence_other"      → Awards, certificates, pay stubs, memberships, patents, contracts

RULES:
- Classify based ONLY on the filename.
- At most ONE of each: petition, rfe, rfe_response, noid, noid_response, decision.
- Multiple evidence files allowed — append counter: evidence_letter_1, evidence_letter_2, etc.
- Only process PDF files — ignore jpg, png, and other non-PDF files.
- Return ONLY valid JSON. No explanation, no markdown, no extra text.

Input filenames:
{filenames_list}

Expected output format:
{{
  "Expert Letter - John Smith.pdf": "evidence_letter_1",
  "Nature Paper 2023.pdf": "evidence_publication_1",
  "AH Path O1A USCIS Forms.pdf": "petition",
  "AHussain_O-1_I797A_Approval_Notice.pdf": "decision",
  "RFE.pdf": "rfe",
  "FULL O-1A Filing - Confidential.pdf": "evidence_other_1"
}}"""

SINGLE_TYPES  = {"petition", "rfe", "rfe_response", "noid", "noid_response", "decision"}
EVIDENCE_TYPES = {"evidence_letter", "evidence_publication", "evidence_other"}


def normalize_application(app_folder, client):
    app_name  = os.path.basename(app_folder)
    raw_files = [f for f in os.listdir(app_folder) if f.lower().endswith(".pdf")]
    if not raw_files:
        print(f"[{app_name}] No PDFs found.")
        return {}

    print(f"\n[{app_name}] Found {len(raw_files)} file(s):")
    for f in raw_files:
        print(f"  {f}")

    prompt   = NAMING_PROMPT.format(filenames_list="\n".join(f"- {f}" for f in raw_files))
    response = client.generate(
        model='org/qwen2.5-1m:14b',
        prompt=prompt,
        stream=False,
        options={"temperature": 0, "num_predict": 1000}
    )
    raw = response['response'].strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        naming_map = json.loads(raw.strip())
    except json.JSONDecodeError as e:
        print(f"  Parse error: {e} | raw: {raw[:200]}")
        return {}

    seen_single  = {}
    ev_counters  = {}
    renamed_files = {}

    for original, standard in naming_map.items():
        src = os.path.join(app_folder, original)
        if not os.path.exists(src):
            print(f"  Warning: not found — {original}")
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

        dst = os.path.join(app_folder, f"{standard}.pdf")
        os.rename(src, dst)
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
