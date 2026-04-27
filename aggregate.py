
from datetime import datetime, timezone
from dateutil import parser as dateparser

# ── HELPERS ───────────────────────────────────────────────────

def parse_date_safe(s):
    if not s:
        return None
    try:
        return dateparser.parse(str(s))
    except:
        return None

def compute_processing_days(filing_str, decision_str):
    f = parse_date_safe(filing_str)
    d = parse_date_safe(decision_str)
    if f and d and d > f:
        return (d - f).days
    return None

def normalize_outcome(val):
    if not val:
        return None
    v = str(val).lower()
    if "approv"   in v: return "approved"
    if "den"      in v: return "denied"
    if "withdraw" in v: return "withdrawn"
    return val

def to_bool(val):
    if isinstance(val, bool): return val
    if isinstance(val, str):  return val.strip().lower() in ("true","yes","y","1")
    return None


# ── STEP 1: TRANSFORM insights → aggregated_cases ─────────────

def transform_and_save(firestore_client, run_id, all_results, model_name):
    # Build full snapshot: ALL apps from processed_applications + this run's results
    snap = firestore_client.collection("processed_applications").get()
    combined = {}
    for doc in snap:
        app_key = doc.id
        data = doc.to_dict()
        fields = dict(data.get("fields", {}))
        doc_types = data.get("processed_docs", [])
        # Derive flags from doc types (not stored back in processed_applications)
        fields["rfe_linked_to_evidence_issue"]  = any(k.startswith("rfe")  for k in doc_types)
        fields["noid_linked_to_evidence_issue"] = any(k.startswith("noid") for k in doc_types)
        fields["_doc_types"] = sorted(doc_types)
        combined[app_key] = fields

    # Override with current run's all_results — freshest data + accurate derived flags
    for app_key, raw in all_results.items():
        combined[app_key] = raw

    print(f"\n[aggregate] Transforming {len(combined)} app(s) → aggregated_cases "
          f"({len(all_results)} this run + {len(combined) - len(all_results)} historical)...")

    for app_key, raw in combined.items():

        # ── Doc types attached by pipeline cell ───────────────
        doc_types = raw.get("_doc_types", [])

        filing_date   = raw.get("filing_date")
        decision_date = raw.get("decision_date")
        final_outcome = normalize_outcome(raw.get("final_outcome"))
        rfe_issued    = to_bool(raw.get("rfe_linked_to_evidence_issue"))
        noid_issued   = to_bool(raw.get("noid_linked_to_evidence_issue"))

        filing_year = None
        fd = parse_date_safe(filing_date)
        if fd:
            filing_year = fd.year

        doc = {
            # ── Traceability ──────────────────────────────────
            "run_id":    run_id,
            "app_key":   app_key,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model":     model_name,

            # ── Case identity ─────────────────────────────────
            "petition_type":      raw.get("petition_type"),
            "filing_date":        filing_date,
            "filing_year":        filing_year,
            "decision_date":      decision_date,
            "service_center":     raw.get("service_center_or_field_office"),
            "premium_processing": raw.get("premium_processing"),
            "officer_number":     raw.get("officer_number"),

            # ── Outcome ───────────────────────────────────────
            "final_outcome":        final_outcome,
            "approved":             final_outcome == "approved",
            "rfe_issued":           rfe_issued  if rfe_issued  is not None else False,
            "noid_issued":          noid_issued if noid_issued is not None else False,
            "rfe_basis":            raw.get("rfe_basis"),
            "noid_basis":           raw.get("noid_basis"),
            "outcome_after_rfe":    raw.get("outcome_after_rfe"),
            "outcome_after_noid":   raw.get("outcome_after_noid"),
            "processing_time_days": compute_processing_days(filing_date, decision_date),

            # ── Beneficiary ───────────────────────────────────
            "employer_sponsored":                 to_bool(raw.get("employer_sponsored")),
            "self_petition":                      to_bool(raw.get("self_petition")),
            "beneficiary_country_of_birth":       raw.get("beneficiary_country_of_birth"),
            "beneficiary_country_of_citizenship": raw.get("beneficiary_country_of_citizenship"),
            "beneficiary_industry":               raw.get("beneficiary_industry"),
            "endeavor_field":                     raw.get("endeavor_field"),

            # ── Criteria ──────────────────────────────────────
            "criteria_claimed_count":      raw.get("criteria_claimed_count"),
            "major_award_claimed":         to_bool(raw.get("major_award_claimed")),
            "publications_included":       to_bool(raw.get("publications_included")),
            "judging_others_work_claimed": to_bool(raw.get("judging_others_work_claimed")),
            "high_salary_claimed":         to_bool(raw.get("high_salary_claimed")),

            # ── Decision ──────────────────────────────────────
            "primary_basis_for_decision": raw.get("primary_basis_for_decision"),
            "basis_for_application":      raw.get("basis_for_application"),
            "basis_for_approval":         raw.get("basis_for_approval"),
            "discretion_applied":         to_bool(raw.get("discretion_applied")),

            # ── Evidence (LLM extracted) ──────────────────────
            "included_publications": to_bool(raw.get("included_publications")),
            "cell_cited":            to_bool(raw.get("cell_cited")),
            "publication_type":      raw.get("publication_type"),

            # ── Evidence (derived from doc types present) ─────
            "has_rfe":                  any(k == "rfe"                         for k in doc_types),
            "has_noid":                 any(k == "noid"                        for k in doc_types),
            "has_evidence_letter":      any(k.startswith("evidence_letter")     for k in doc_types),
            "has_evidence_publication": any(k.startswith("evidence_publication") for k in doc_types),
            "has_evidence_other":       any(k.startswith("evidence_other")       for k in doc_types),
            "evidence_count":           len([k for k in doc_types if k.startswith("evidence_")]),
            "doc_types_present":        doc_types,
        }

        doc_id = f"{app_key}_{run_id}"
        firestore_client.collection("aggregated_cases").document(doc_id).set(doc)
        print(f"  ✓ aggregated_cases/{doc_id}")

    print("[aggregate] Transform done.")


# ── STEP 2: AGGREGATE aggregated_cases → aggregated_insights ──

def aggregate_and_save(firestore_client, run_id, model_name):
    print(f"\n[aggregate] Reading aggregated_cases for run {run_id}...")

    snap     = firestore_client.collection("aggregated_cases") \
                   .where("run_id", "==", run_id).get()
    all_docs = [d.to_dict() for d in snap]

    print(f"  Found {len(all_docs)} doc(s)")
    if not all_docs:
        print("  No docs — skipping.")
        return

    # ── Core aggregation function ──────────────────────────────
    def aggregate(docs):
        total = len(docs)
        if total == 0:
            return {"sample_size": 0, "no_data": True}

        def pct(n, of=None):
            of = of if of is not None else total
            return round((n / of) * 100) if of else 0

        def freq(field):
            m = {}
            for d in docs:
                v = d.get(field)
                if v is None:
                    continue
                items = v if isinstance(v, list) else [v]
                for item in items:
                    if item is not None:
                        m[item] = m.get(item, 0) + 1
            return [
                {"label": k, "count": v, "pct": pct(v)}
                for k, v in sorted(m.items(), key=lambda x: -x[1])
            ]

        # ── Outcome groups ─────────────────────────────────────
        approved_docs = [d for d in docs if d.get("approved") is True]
        rfe_docs      = [d for d in docs if d.get("rfe_issued")  is True]
        noid_docs     = [d for d in docs if d.get("noid_issued") is True]
        clean_docs    = [d for d in docs if not d.get("rfe_issued") and not d.get("noid_issued")]

        n_rfe   = len(rfe_docs)
        n_noid  = len(noid_docs)
        n_clean = len(clean_docs)

        rfe_approved   = sum(1 for d in rfe_docs   if d.get("approved"))
        noid_approved  = sum(1 for d in noid_docs  if d.get("approved"))
        clean_approved = sum(1 for d in clean_docs if d.get("approved"))

        # ── Processing time ────────────────────────────────────
        time_docs  = [d for d in docs if d.get("processing_time_days") and d["processing_time_days"] > 0]
        avg_days   = sum(d["processing_time_days"] for d in time_docs) / len(time_docs) if time_docs else None
        avg_months = round(avg_days / 30.44, 1) if avg_days else None

        # ── YoY ───────────────────────────────────────────────
        by_year = {}
        for d in docs:
            y = d.get("filing_year")
            if not y: continue
            if y not in by_year:
                by_year[y] = {"total": 0, "rfe": 0, "noid": 0, "approved": 0}
            by_year[y]["total"]    += 1
            if d.get("rfe_issued"):  by_year[y]["rfe"]      += 1
            if d.get("noid_issued"): by_year[y]["noid"]     += 1
            if d.get("approved"):    by_year[y]["approved"] += 1

        yoy = [
            {
                "year":          str(y),
                "total":         v["total"],
                "rfe_rate":      pct(v["rfe"],      v["total"]),
                "noid_rate":     pct(v["noid"],     v["total"]),
                "approval_rate": pct(v["approved"], v["total"]),
            }
            for y, v in sorted(by_year.items())
        ]

        # ── Evidence risk ──────────────────────────────────────
        rfe_basis_freq   = freq("rfe_basis")
        noid_basis_freq  = freq("noid_basis")
        most_common_risk = rfe_basis_freq[0]["label"] if rfe_basis_freq else None
        pub_type_freq    = freq("publication_type")

        # ── Evidence type breakdown ────────────────────────────
        has_letter_docs = [d for d in docs if d.get("has_evidence_letter")]
        has_pub_docs    = [d for d in docs if d.get("has_evidence_publication")]
        has_other_docs  = [d for d in docs if d.get("has_evidence_other")]

        evidence_breakdown = {
            "with_letters":       pct(len(has_letter_docs)),
            "with_publications":  pct(len(has_pub_docs)),
            "with_other":         pct(len(has_other_docs)),
            "avg_evidence_count": round(
                sum(d.get("evidence_count", 0) for d in docs) / total, 1
            ),
        }

        # ── RFE rate by evidence type ──────────────────────────
        def rfe_rate_for(subset):
            n = len(subset)
            if not n: return None
            return pct(sum(1 for d in subset if d.get("rfe_issued")), n)

        rfe_by_evidence_type = {
            "letter_rfe_rate":      rfe_rate_for(has_letter_docs),
            "publication_rfe_rate": rfe_rate_for(has_pub_docs),
            "other_rfe_rate":       rfe_rate_for(has_other_docs),
        }

        # ── Cell citation ──────────────────────────────────────
        cell_docs     = [d for d in docs if d.get("cell_cited") is True]
        non_cell_docs = [d for d in docs if not d.get("cell_cited")]
        c_total  = len(cell_docs)
        nc_total = len(non_cell_docs)

        cell_rfe_docs   = [d for d in cell_docs if d.get("rfe_issued")]
        cell_noid_docs  = [d for d in cell_docs if d.get("noid_issued")]
        cell_clean_docs = [d for d in cell_docs if not d.get("rfe_issued") and not d.get("noid_issued")]

        def _outcome(grp):
            n = len(grp)
            if n == 0: return {"approved": None, "denied": None}
            a = sum(1 for d in grp if d.get("approved"))
            return {"approved": pct(a, n), "denied": pct(n - a, n)}

        pub_type_approvals = {}
        for d in docs:
            pt = d.get("publication_type")
            if not pt: continue
            if pt not in pub_type_approvals:
                pub_type_approvals[pt] = {"total": 0, "approved": 0}
            pub_type_approvals[pt]["total"] += 1
            if d.get("approved"):
                pub_type_approvals[pt]["approved"] += 1

        pub_type_approval_rates = [
            {
                "label":         pt,
                "total":         v["total"],
                "approval_rate": pct(v["approved"], v["total"]),
            }
            for pt, v in sorted(pub_type_approvals.items(), key=lambda x: -x[1]["total"])
        ]

        cell_block = {
            "total":                  c_total,
            "approval_rate":          pct(sum(1 for d in cell_docs     if d.get("approved")), c_total)  if c_total  else None,
            "rfe_rate":               pct(len(cell_rfe_docs),  c_total) if c_total  else None,
            "noid_rate":              pct(len(cell_noid_docs), c_total) if c_total  else None,
            "non_cell_approval_rate": pct(sum(1 for d in non_cell_docs if d.get("approved")), nc_total) if nc_total else None,
            "outcomes": [
                {"label": "No RFE / NOID", **_outcome(cell_clean_docs)},
                {"label": "RFE Issued",    **_outcome(cell_rfe_docs)},
                {"label": "NOID Issued",   **_outcome(cell_noid_docs)},
            ],
            "pub_type_approval_rates": pub_type_approval_rates,
        }

        # ── Officers ───────────────────────────────────────────
        officer_ids = list({d.get("officer_number") for d in docs if d.get("officer_number")})
        officers = []
        for oid in officer_ids:
            od      = [d for d in docs if d.get("officer_number") == oid]
            o_rfe   = [d for d in od   if d.get("rfe_issued")]
            o_noid  = [d for d in od   if d.get("noid_issued")]
            o_clean = [d for d in od   if not d.get("rfe_issued") and not d.get("noid_issued")]
            o_total = len(od)
            by_yr   = {}
            for d in od:
                y = d.get("filing_year")
                if not y: continue
                if y not in by_yr: by_yr[y] = {"total": 0, "approved": 0}
                by_yr[y]["total"] += 1
                if d.get("approved"): by_yr[y]["approved"] += 1
            officers.append({
                "id":                  oid,
                "total":               o_total,
                "approval_rate":       pct(sum(1 for d in od      if d.get("approved")), o_total),
                "rfe_count":           len(o_rfe),
                "noid_count":          len(o_noid),
                "rfe_approval_rate":   pct(sum(1 for d in o_rfe   if d.get("approved")), len(o_rfe))   if o_rfe   else None,
                "noid_approval_rate":  pct(sum(1 for d in o_noid  if d.get("approved")), len(o_noid))  if o_noid  else None,
                "clean_approval_rate": pct(sum(1 for d in o_clean if d.get("approved")), len(o_clean)) if o_clean else None,
                "by_year": [
                    {"year": str(y), "approval_rate": pct(v["approved"], v["total"])}
                    for y, v in sorted(by_yr.items())
                ],
            })

        return {
            # ── Page 1: Overview ──────────────────────────────
            "sample_size":           total,
            "approval_rate":         pct(len(approved_docs)),
            "rfe_rate":              pct(n_rfe),
            "noid_rate":             pct(n_noid),
            "avg_processing_months": avg_months,
            "bases_application":     freq("basis_for_application"),
            "bases_approval":        freq("primary_basis_for_decision"),
            "petition_types":        freq("petition_type"),
            "industries":            freq("beneficiary_industry"),
            "countries":             freq("beneficiary_country_of_birth"),
            "service_centers":       freq("service_center"),
            "endeavor_fields":       freq("endeavor_field"),
            "yoy":                   yoy,

            # ── Page 2: Evidence Risk ─────────────────────────
            "rfe_linked_pct":        pct(n_rfe,  n_rfe)  if n_rfe  else None,
            "noid_linked_pct":       pct(n_noid, n_noid) if n_noid else None,
            "most_common_risk":      most_common_risk,
            "rfe_basis_freq":        rfe_basis_freq,
            "noid_basis_freq":       noid_basis_freq,
            "pub_type_freq":         pub_type_freq,
            "evidence_breakdown":    evidence_breakdown,
            "rfe_by_evidence_type":  rfe_by_evidence_type,

            # ── Page 3: Case Outcomes ─────────────────────────
            "adjudication_paths": {
                "rfe_approval_rate":   pct(rfe_approved,             n_rfe)   if n_rfe   else None,
                "rfe_denial_rate":     pct(n_rfe  - rfe_approved,   n_rfe)   if n_rfe   else None,
                "noid_approval_rate":  pct(noid_approved,            n_noid)  if n_noid  else None,
                "noid_denial_rate":    pct(n_noid - noid_approved,   n_noid)  if n_noid  else None,
                "clean_approval_rate": pct(clean_approved,           n_clean) if n_clean else None,
                "clean_denial_rate":   pct(n_clean - clean_approved, n_clean) if n_clean else None,
            },

            # ── Page 4: Officer Review ────────────────────────
            "officers": officers,

            # ── Page 5: Cell Citation ─────────────────────────
            "cell": cell_block,
        }

    # ── Distinct filter values ─────────────────────────────────
    def uniq(field):
        return sorted({d.get(field) for d in all_docs if d.get(field) is not None})

    filter_values = {
        "petition_types":  uniq("petition_type"),
        "service_centers": uniq("service_center"),
        "filing_years":    [str(y) for y in uniq("filing_year")],
        "officers":        uniq("officer_number"),
    }

    # ── Pre-compute ALL filter combinations ────────────────────
    SENTINEL        = "__all__"
    petition_types  = [SENTINEL] + filter_values["petition_types"]
    service_centers = [SENTINEL] + filter_values["service_centers"]
    filing_years    = [SENTINEL] + [int(y) for y in filter_values["filing_years"]]
    officers        = [SENTINEL] + filter_values["officers"]

    total_combos = (
        len(petition_types) *
        len(service_centers) *
        len(filing_years) *
        len(officers)
    )
    print(f"  Pre-computing {total_combos} filter combination(s)...")

    slices = {}
    for pt in petition_types:
        for sc in service_centers:
            for fy in filing_years:
                for of in officers:
                    filtered = all_docs
                    if pt != SENTINEL: filtered = [d for d in filtered if d.get("petition_type")  == pt]
                    if sc != SENTINEL: filtered = [d for d in filtered if d.get("service_center") == sc]
                    if fy != SENTINEL: filtered = [d for d in filtered if d.get("filing_year")    == fy]
                    if of != SENTINEL: filtered = [d for d in filtered if d.get("officer_number") == of]

                    key = f"pt={pt}|sc={sc}|fy={fy}|of={of}"
                    slices[key] = aggregate(filtered)

    print(f"  ✓ {len(slices)} slices computed")

    # ── Write to aggregated_insights ──────────────────────────
    snapshot = {
        "run_id":        run_id,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "model":         model_name,
        "app_count":     len(all_docs),
        "filter_values": filter_values,
        "slices":        slices,
    }

    firestore_client.collection("aggregated_insights").document(run_id).set(snapshot)
    print(f"  ✓ aggregated_insights/{run_id}")
    print("[aggregate] Done.")
