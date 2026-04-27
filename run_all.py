"""
run_all.py — Single entry point. Just run:

  python run_all.py

Before running, you must manually:
  1. Place colab-key-new.json in the project root

Everything else (dependency install, directory creation, parser download,
application fetching) is handled automatically.

Steps:
  0.  setup.py    → install deps, create dirs, download parser
  0b. GCS         → list applications/, download all PDFs
  1.  naming.py   → classify & rename PDFs in applications/
  2.  pipeline.py → parse PDFs to markdown, run LLM extraction, save to Firestore
                    (per-document deduplication via processed_applications collection)
  3.  aggregate.py→ transform + aggregate results (called inside run_pipeline)
"""

import os, sys, time, subprocess
from datetime import datetime, timezone

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Pre-flight checks ─────────────────────────────────────────────────────────
_key_path = os.path.join(_BASE_DIR, "colab-key-new.json")
if not os.path.exists(_key_path):
    print("ERROR: colab-key-new.json not found in project root.")
    print("  Add your GCP service account key file and re-run.")
    sys.exit(1)

# ── STEP 0: Setup ─────────────────────────────────────────────────────────────
print("="*60)
print("STEP 0: Running setup")
print("="*60)
subprocess.run([sys.executable, os.path.join(_BASE_DIR, "setup.py")], check=True)

from config import RUNPOD_URL, RUNPOD_MODEL
print("="*60)
print("CONFIG")
print("="*60)
print(f"  RunPod URL : {RUNPOD_URL}")
print(f"  Model      : {RUNPOD_MODEL}")
print("="*60)
print()

# ── Tee stdout → console + per-run log file ───────────────────────────────────
class _Tee:
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            s.write(data)
    def flush(self):
        for s in self._streams:
            s.flush()

_LOG_DIR  = os.path.join(_BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

_run_ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
_log_path = os.path.join(_LOG_DIR, f"{_run_ts}.log")
_log_file = open(_log_path, "w", encoding="utf-8", buffering=1)
sys.stdout = _Tee(sys.__stdout__, _log_file)

# ── Imports deferred until after setup installs packages ─────────────────────
from pipeline import (
    client, APPLICATIONS_DIR,
    build_app_markdowns, run_pipeline,
    list_gcs_applications, fetch_applications_from_gcs,
    get_processed_application,
)
from naming import normalize_application

if __name__ == "__main__":

    # ── STEP 0b: Fetch all applications from GCS ──────────────────────────────
    print("="*60)
    print("STEP 0b: Fetching applications from GCS")
    print("="*60)

    gcs_apps = list_gcs_applications()
    if not gcs_apps:
        print("ERROR: No application folders found in gs://…/applications/")
        sys.exit(1)

    print(f"Applications in GCS: {', '.join(gcs_apps)}")
    print(f"Downloading {len(gcs_apps)} application(s)...")
    fetch_applications_from_gcs(gcs_apps)
    print()

    app_names = gcs_apps

    def _fmt(seconds):
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s:02d}s" if m else f"{s}s"

    timing    = []
    run_start = time.monotonic()
    all_docs_md = {}

    # ── STEPS 1 & 2: Naming + Parsing (all apps) ─────────────────────────────
    for app_name in app_names:
        app_folder = os.path.join(APPLICATIONS_DIR, app_name)

        print("\n" + "="*60)
        print(f"APPLICATION: {app_name}  ({app_names.index(app_name)+1}/{len(app_names)})")
        print("="*60)

        print(f"\n[{app_name}] STEP 1: Naming")
        t0 = time.monotonic()
        normalize_application(app_folder, client)
        step1_s = time.monotonic() - t0

        # After naming we know the standard doc type names — check what's already processed
        processed_record  = get_processed_application(app_name)
        already_processed = set(processed_record["processed_docs"])
        if already_processed:
            print(f"  Skipping parse for already-processed docs: {sorted(already_processed)}")

        print(f"\n[{app_name}] STEP 2: Parsing")
        t0 = time.monotonic()
        all_docs_md[app_name] = build_app_markdowns(app_name, app_folder, skip_doc_types=already_processed)
        step2_s = time.monotonic() - t0

        timing.append((app_name, step1_s, step2_s))
        print(f"\n✓ {app_name} prepared — naming {_fmt(step1_s)}, parsing {_fmt(step2_s)}")

    # ── STEP 3: Pipeline (per-document deduplication handled inside) ──────────
    print("\n" + "="*60)
    print(f"STEP 3: Pipeline — {len(app_names)} application(s)")
    print("="*60)
    t0 = time.monotonic()
    run_pipeline(all_docs_md)
    pipeline_s = time.monotonic() - t0

    run_total = time.monotonic() - run_start

    print("\n" + "="*60)
    print(f"ALL DONE — {len(app_names)} application(s).")
    print(f"Log saved → {_log_path}")
    print("="*60)

    # ── Timing summary ────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("TIMING SUMMARY")
    print("="*60)
    col = max(len(n) for n, *_ in timing)
    print(f"{'Application':<{col}}  {'Naming':>8}  {'Parsing':>8}")
    print("-" * (col + 20))
    for app_name, s1, s2 in timing:
        print(f"{app_name:<{col}}  {_fmt(s1):>8}  {_fmt(s2):>8}")
    print("-" * (col + 20))
    print(f"{'Pipeline (all apps)':<{col}}  {'':>8}  {_fmt(pipeline_s):>8}")
    print(f"{'TOTAL RUN':<{col}}  {'':>8}  {_fmt(run_total):>8}")
    print("="*60)
