"""
setup.py — Run this ONCE before using the pipeline.
***Before running, you must manually add the gcs secret key file (colab-key-new.json) 
to the project root, which is used for both downloading the parser and saving results to Firestore.***
***After setup, and before running, you should add your PDFs 
to subfolders inside applications/ (one subfolder per application).***


What it does:
  1. Installs all dependencies from requirements.txt
  2. Creates the required directories
  3. Downloads pdf_to_markdown.py from GCS

Usage:
  python setup.py
"""

import subprocess, sys, os, json

# ── STEP 1: Install dependencies ─────────────────────────────────────────────
print("="*60)
print("STEP 1: Installing dependencies")
print("="*60)
req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_path, "-q"])
print("✓ Dependencies installed.\n")

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

COLAB_KEY_PATH   = os.path.join(BASE_DIR, "colab-key-new.json")
GCP_PROJECT      = "data-enclave-dev-488920"
FIREBASE_BUCKET  = "moonlit-balm-489206-i9.firebasestorage.app"

APPLICATIONS_DIR = os.path.join(BASE_DIR, "applications")
PDF_TO_MD_SCRIPT = os.path.join(BASE_DIR, "pdf_to_markdown.py")
LOG_DIR          = os.path.join(BASE_DIR, "logs")

# ── STEP 2: Create directories ────────────────────────────────────────────────
print("="*60)
print("STEP 2: Creating directories")
print("="*60)
for d in [APPLICATIONS_DIR, LOG_DIR, os.path.dirname(PDF_TO_MD_SCRIPT)]:
    os.makedirs(d, exist_ok=True)
    print(f"✓ {d}")
print()

# ── STEP 3: Download parser from GCS ─────────────────────────────────────────
print("="*60)
print("STEP 3: Downloading pdf_to_markdown.py from GCS")
print("="*60)

from google.oauth2 import service_account
from google.cloud import storage as gcs

with open(COLAB_KEY_PATH) as f:
    _key = json.load(f)

_creds     = service_account.Credentials.from_service_account_info(_key)
gcs_client = gcs.Client(credentials=_creds, project=GCP_PROJECT)

bucket = gcs_client.bucket(FIREBASE_BUCKET)
bucket.blob("pipeline/pdf_to_markdown.py").download_to_filename(PDF_TO_MD_SCRIPT)
print(f"✓ Parser saved to {PDF_TO_MD_SCRIPT}")

result = subprocess.run(
    ["python", "-c", f"import ast; ast.parse(open('{PDF_TO_MD_SCRIPT}').read()); print('Syntax OK')"],
    capture_output=True, text=True
)
print(f"  {result.stdout.strip() or result.stderr.strip()}")

print("\n" + "="*60)
print("Setup complete. You can now run: python run_all.py")
print("="*60)
