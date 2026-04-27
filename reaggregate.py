"""
reaggregate.py — Re-run only the aggregation step on an existing Firestore run.

Usage:
    python reaggregate.py 20260420_130948
"""

import sys, json, importlib.util, os
from google.oauth2 import service_account
from google.cloud import firestore

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
KEY_PATH   = os.path.join(BASE_DIR, "colab-key-new.json")
GCP_PROJECT = "data-enclave-dev-488920"
FIRESTORE_DB = "llm-output"

from config import RUNPOD_MODEL

if len(sys.argv) < 2:
    print("Usage: python reaggregate.py <run_id>")
    print("Example: python reaggregate.py 20260420_130948")
    sys.exit(1)

run_id = sys.argv[1]

with open(KEY_PATH) as f:
    _key = json.load(f)

_creds = service_account.Credentials.from_service_account_info(_key)
firestore_client = firestore.Client(credentials=_creds, project=GCP_PROJECT, database=FIRESTORE_DB)

spec = importlib.util.spec_from_file_location("aggregate", os.path.join(BASE_DIR, "aggregate.py"))
agg  = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agg)

print(f"Re-running aggregation for run_id={run_id} ...")
agg.aggregate_and_save(firestore_client, run_id, RUNPOD_MODEL)
print("Done.")
