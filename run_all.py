"""
run_all.py — Single entry point that runs the full pipeline in order.

Before running, install dependencies once:
  python setup.py

Each application folder inside applications/ is processed fully before moving
to the next:
  1. naming.py      → classify & rename PDFs to standard names
  2. pipeline.py    → parse PDFs to markdown, run LLM extraction, save to Firestore
  3. aggregate.py   → transform + aggregate results (called automatically inside run_pipeline)
"""

import os

# ── Importing pipeline triggers client setup ──────────────────────────────────
from pipeline import client, APPLICATIONS_DIR, build_app_markdowns, run_pipeline
from naming import normalize_application

if __name__ == "__main__":

    app_names = sorted(
        n for n in os.listdir(APPLICATIONS_DIR)
        if os.path.isdir(os.path.join(APPLICATIONS_DIR, n)) and not n.startswith(".")
    )

    if not app_names:
        print(f"No application folders found in {APPLICATIONS_DIR}")
        exit(1)

    print(f"\nFound {len(app_names)} application(s): {', '.join(app_names)}")

    completed = []
    for app_name in app_names:
        app_folder = os.path.join(APPLICATIONS_DIR, app_name)

        print("\n" + "="*60)
        print(f"APPLICATION: {app_name}  ({app_names.index(app_name)+1}/{len(app_names)})")
        print("="*60)

        # ── STEP 1: Classify and rename PDFs ─────────────────────────────────
        print(f"\n[{app_name}] STEP 1: Naming")
        normalize_application(app_folder, client)

        # ── STEP 2: Convert renamed PDFs → markdown ───────────────────────────
        print(f"\n[{app_name}] STEP 2: Parsing")
        docs_md = build_app_markdowns(app_name, app_folder)

        # ── STEP 3: LLM extraction + Firestore save + aggregate ───────────────
        print(f"\n[{app_name}] STEP 3: Pipeline")
        run_pipeline({app_name: docs_md})

        completed.append(app_name)
        print(f"\n✓ {app_name} done ({len(completed)}/{len(app_names)})")

    print("\n" + "="*60)
    print(f"ALL DONE — {len(completed)} application(s) processed.")
    print("="*60)
