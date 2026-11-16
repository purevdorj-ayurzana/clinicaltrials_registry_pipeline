"""Rebuild the archived registry workflow and verify its frozen outputs."""
from __future__ import annotations
import csv
import json
import shutil
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from clinicaltrials_api.cohort import ingest_classifier_export
from clinicaltrials_api.config import load_config
from clinicaltrials_api.extract import extract_run
from clinicaltrials_api.compare import compare_run
from clinicaltrials_api.summarize import summarize_run
from . import covariates, exposures, publication_cohort, registration, registration_detail, start_boundary, timing, trial_dates
from .release import ROOT, cell, compare_csv, read_csv, read_json, restore_archives, sha256, verify_data, write_csv

def restore_run(work):
    config = load_config(ROOT / "config.json")
    initialized = ingest_classifier_export(export_dir=ROOT / "data/classifier", config=config,
                                          output_root=work / "runs")
    run = work / "registry"
    initialized.rename(run)
    manifest = read_json(run / "run_manifest.json")
    manifest["run_id"] = run.name
    manifest["stage_status"]["download"] = "COMPLETED"
    manifest["registry_source"] = "BUNDLED_ARCHIVED_RESPONSES"
    restore_archives(run / "raw/clinicaltrials_gov")
    identity = run / "raw/clinicaltrials_gov/identity_resolutions.csv"
    manifest["identity_resolution"] = {
        "source_nct_id_count": 1759, "resolved_source_nct_id_count": 1759,
        "canonical_trial_count": 1758, "official_alias_count": 3,
        "file": "raw/clinicaltrials_gov/identity_resolutions.csv", "sha256": sha256(identity),
    }
    (run / "run_manifest.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    for stage, function in [("extract", extract_run), ("compare", compare_run), ("summarize", summarize_run)]:
        print(f"Registry {stage}", flush=True)
        print(json.dumps(function(run_dir=run, config=config)), flush=True)
    return run

def boundary_details(flags):
    details, seen = [], set()
    for row in flags:
        if row["flag_code"] != "START_DATE_CROSSED_FIRST_SUBMISSION_BOUNDARY":
            continue
        key = (row["nct_id"], row["from_version"], row["to_version"], row["event_posting_date"])
        if key in seen:
            raise ValueError("Duplicate boundary flag")
        seen.add(key)
        old, new = json.loads(row["old_value"]), json.loads(row["new_value"])
        direction = registration_detail._boundary_direction(old, new)
        submission = old["study_first_submit_date"]
        if submission != new["study_first_submit_date"]:
            raise ValueError("Boundary submission date changed")
        output = dict(zip(["nct_id", "from_version", "to_version", "event_posting_date"], key))
        output.update(direction=direction, first_submission_date=submission)
        for prefix, value in [("old", old), ("new", new)]:
            desc = registration_detail.describe_relative_date(value["study_start_date"], submission)
            for column, field in [("reported_start_date", "reported_date"), ("start_precision", "precision"),
                                  ("start_possible_first_date", "possible_first_date"), ("start_possible_last_date", "possible_last_date"),
                                  ("possible_lag_min_days", "possible_lag_min_days"), ("possible_lag_max_days", "possible_lag_max_days"),
                                  ("exact_lag_days", "exact_lag_days"), ("exact_lag_bin", "exact_lag_bin"),
                                  ("relation_to_first_submission", "relation")]:
                output[f"{prefix}_{column}"] = desc[field]
        details.append(output)
    return sorted(details, key=lambda r: (r["nct_id"],r["from_version"],r["to_version"],r["event_posting_date"]))

def semantic_overlap(flags, result):
    assessments=read_csv(ROOT/"data/semantic_assessment/assessments.csv")
    canonical={row["nct_id"] for row in assessments}
    deterministic={row["nct_id"] for row in flags if row["flag_code"]=="PRIMARY_OUTCOME_CHANGED"}
    semantic={row["nct_id"] for row in assessments if row["meaningful_change"].lower()=="true"}
    overlap=[]
    for det in [True,False]:
        for llm in [True,False]:
            overlap.append({"deterministic_primary_outcome_flag":"Yes" if det else "No",
                            "llm_meaningful_change":"Yes" if llm else "No",
                            "trials":sum((nct in deterministic)==det and (nct in semantic)==llm for nct in canonical)})
    write_csv(result/"semantic_rule_overlap.csv",overlap)
    subtype={"outcome added":"PRIMARY_OUTCOME_ADDED","outcome removed":"PRIMARY_OUTCOME_REMOVED",
             "outcome promotion":"OUTCOME_PROMOTED_TO_PRIMARY","outcome demotion":"PRIMARY_OUTCOME_DEMOTED",
             "outcome modified":"PRIMARY_OUTCOME_TIME_FRAME_CHANGED"}
    definitions={"outcome added":"change_type contains addition","outcome removed":"change_type contains removal",
        "outcome promotion":"outcome_reclassifications contains outcome promotion",
        "outcome demotion":"outcome_reclassifications contains outcome demotion",
        "outcome modified":"change_type contains any of definition_or_components, measurement_method, timeframe, threshold",
        "outcome replaced":"change_type contains outcome_replacement","other":"change_type contains other"}
    categories=defaultdict(set)
    for row in assessments:
        types=set(json.loads(row["change_type"])); reclass=set(json.loads(row["outcome_reclassifications"]))
        names=[]
        for code,name in [("addition","outcome added"),("removal","outcome removed"),("outcome_replacement","outcome replaced"),("other","other")]:
            if code in types:names.append(name)
        if types & {"definition_or_components","measurement_method","timeframe","threshold"}:names.append("outcome modified")
        names.extend(reclass)
        for name in names:categories[name].add(row["nct_id"])
    comparisons=[]
    for category,definition in definitions.items():
        sem=categories[category]
        row={"harmonized_category":category,"deterministic_definition":subtype.get(category,"No direct deterministic counterpart"),
             "llm_definition":definition,"deterministic_yes":"","llm_yes":len(sem),"both_yes":"",
             "deterministic_only":"","llm_only":"","neither":"","union":"","jaccard_percent":""}
        if category in subtype:
            det={r["nct_id"] for r in flags if r["change_subtype_code"]==subtype[category] and r["flag_code"]=="PRIMARY_OUTCOME_CHANGED"}
            union=len(det|sem);both=len(det&sem)
            row.update(deterministic_yes=len(det),both_yes=both,deterministic_only=len(det-sem),llm_only=len(sem-det),
                       neither=1758-union,union=union,jaccard_percent=f"{100*both/union:.1f}" if union else "")
        comparisons.append(row)
    write_csv(result/"semantic_rule_categories.csv",comparisons)

def prepare_tables(run, work):
    frames, result = work / "dataframes", work / "results"
    frames.mkdir(parents=True, exist_ok=True)
    result.mkdir(parents=True, exist_ok=True)
    endpoint_meta = read_json(ROOT / "data/classifier/endpoint_metadata.json")
    settings = endpoint_meta["arm_settings"]["sol"]
    endpoint, _ = publication_cohort.validate_endpoint_rows(
        read_csv(ROOT / "data/classifier/endpoint_assessments.csv"),
        endpoint_run_id=endpoint_meta["run_id"], upstream_rct_run_id=endpoint_meta["upstream_rct_run_id"],
        endpoint_arm="sol", prompt_hash=endpoint_meta["prompt_hash"], schema_hash=endpoint_meta["schema_hash"],
        model=settings["model"], reasoning_effort=settings["reasoning_effort"], expected_pmid_hash=endpoint_meta["source_pmid_hash"],
    )
    mapping = publication_cohort.derive_mapping(read_csv(run / "input/publication_trial_links.csv"), endpoint)
    if mapping["counts"] != publication_cohort.PRODUCTION_EXPECTED_COUNTS:
        raise ValueError("Publication mapping does not reproduce the frozen cohort")
    for name, key in [("cohort_flow.csv","flow_rows"),("cohort_exclusions.csv","ledger_rows"),("publication_trial_mapping.csv","mapping_rows")]:
        write_csv(result / name, mapping[key])
    trials, _, _ = trial_dates._build_trial_cohort(mapping["mapping_rows"], run)
    snapshots = read_csv(run / "processed/version_snapshots.csv")
    first = {}
    for row in snapshots:
        if row["nct_id"] not in first or int(row["version"]) < int(first[row["nct_id"]]["version"]):
            first[row["nct_id"]] = row
    model_rows = []
    for row in sorted(trials.values(), key=lambda item: item["nct_id"]):
        submitted = first[row["nct_id"]]["study_first_submit_date"]
        year = datetime.strptime(submitted, "%Y-%m-%d").year
        model_rows.append({"nct_id":row["nct_id"],"endpoint_label":row["endpoint_label"],
                           "endpoint_positive":row["endpoint_label"]=="POSITIVE", "first_submission_date":submitted,
                           "first_submission_year":year,"submission_year_5y_centered":(year-2015)/5})
    cov, _, counts = covariates._build_covariates(model_rows, run)
    write_csv(frames / "covariates.csv", cov, covariates.COVARIATE_COLUMNS)
    shutil.copyfile(frames / "covariates.csv", result / "trial_covariates.csv")
    compare_csv(result / "trial_covariates.csv", ROOT / "results/trial_covariates.csv", tolerance=1e-11)
    cov_by = {r["nct_id"]:r for r in cov}
    mapping_by = {r["nct_id"]:r for r in mapping["mapping_rows"]}
    for trial in trials.values():
        publication = mapping_by[trial["source_nct_id"]]["index_primary_publication_date"]
        trial["index_primary_publication_date"] = publication
        primary = trial["primary_completion_anchor"]
        trial["two_window_population_eligible"] = primary is not None
        trial["four_window_population_eligible"] = bool(trial["anchor_evaluable"] and publication and primary["latest"] <= date.fromisoformat(publication))
    flags = read_csv(run / "reports/review_flags.csv")
    semantic_overlap(flags,result)
    cohort_flags = [r for r in flags if r["nct_id"] in trials]
    terms = exposures.build_terms(cohort_flags)
    events, assignments, term_rows = exposures.aggregate_events(trials, cohort_flags, terms)
    write_csv(frames / "exposure_terms.csv", term_rows, exposures.TERM_COLUMNS)
    write_csv(frames / "trial_level_term_exposures.csv", assignments, exposures.EXPOSURE_COLUMNS)
    write_csv(frames / "term_events.csv", events, exposures.EVENT_COLUMNS)
    details = boundary_details(flags)
    write_csv(frames / "boundary_events.csv", details)
    directions = start_boundary.build_trial_exposure(
        [{k:str(cell(v)) for k,v in r.items()} for r in cov], details)
    write_csv(frames / "directional.csv", directions)
    by_event = defaultdict(list)
    for event in events:
        by_event[(event["nct_id"],event["exposure_level"],event["flag_code"],event["subtype_code"])].append(date.fromisoformat(event["event_posting_date"]))
    ledger = []
    for level, code, label, event_level, flag, subtype in timing.EXPOSURES:
        for nct in sorted(n for n,c in cov_by.items() if c["complete_case_eligible"]):
            trial, c = trials[nct], cov_by[nct]
            dates = sorted(by_event[(nct,event_level,flag,subtype)])
            start, pc = trial["study_start_anchor"], trial["primary_completion_anchor"]
            row = {"nct_id":nct,"endpoint_label":c["endpoint_label"],"exposure_level":level,
                   "exposure_code":code,"exposure_label":label,"event_level":event_level,"flag_code":flag,"subtype_code":subtype,
                   "event_count":len(dates),"event_posting_dates_json":json.dumps([d.isoformat() for d in dates],separators=(',',':')),
                   "submission_year_5y_centered":c["submission_year_5y_centered"],"phase_group":c["phase_group"],
                   "industry_lead_sponsor":c["industry_lead_sponsor"],"log2_initial_enrollment_centered_619":c["log2_initial_enrollment_centered_619"]}
            for prefix, anchor in [("study_start",start),("primary_completion",pc)]:
                classified = timing.classify(dates, anchor["earliest"], anchor["latest"]) if anchor else ("ANCHOR_UNAVAILABLE","ANCHOR_UNAVAILABLE",0,0,0)
                row[prefix+"_four_category"], row[prefix+"_collapsed_category"] = classified[:2]
                for suffix,value in zip(["early_event_count","late_event_count","ambiguous_event_count"],classified[2:]):row[prefix+"_"+suffix]=value
                date_prefix = "actual_primary_completion" if prefix=="primary_completion" else prefix
                for suffix,field in [("date","value"),("precision","precision"),("earliest","earliest"),("latest","latest")]:
                    value=anchor[field] if anchor else ""
                    row[date_prefix+"_"+suffix]=value.isoformat() if isinstance(value,date) else value
            ledger.append(row)
    for landmark, expected in timing.EXPECTED.items():
        for code, target in expected.items():
            observed=Counter(r[landmark+"_four_category"] for r in ledger if r["exposure_code"]==code)
            if tuple(observed[c] for c in timing.CATEGORY_ORDER) != target:
                raise ValueError(f"Timing counts differ: {landmark} {code}")
    write_csv(frames / "timing.csv", ledger)
    canonical={r["nct_id"] for r in read_csv(run / "processed/canonical_trials.csv")}
    lag=registration._build_trial_rows(canonical,snapshots)
    write_csv(result / "registration_lags.csv",lag)
    first_post_ids={r["nct_id"] for r in flags if r["flag_code"]=="FIRST_POST_AFTER_STUDY_START"}
    first_details=registration_detail._first_post_detail([{k:str(cell(v)) for k,v in r.items()} for r in lag],first_post_ids)
    write_csv(result / "first_post_after_start.csv",registration_detail._first_post_summary(first_details, [{k:str(cell(v)) for k,v in r.items()} for r in lag]))
    write_csv(result / "start_boundary_crossings.csv",registration_detail._boundary_summary(details))
    all_terms=exposures.build_terms(flags)
    prevalence=[]
    for term in all_terms:
        selected=[r for r in flags if r["flag_code"]==term["flag_code"] and (not term["subtype_code"] or r["change_subtype_code"]==term["subtype_code"])]
        prevalence.append({"flag_code":term["flag_code"],"subtype_code":term["subtype_code"],"trials":len({r["nct_id"] for r in selected}),"canonical_trials":1758,"flag_rows":len(selected)})
    write_csv(result / "registry_change_prevalence.csv",prevalence)
    shutil.copyfile(ROOT / "data/semantic_assessment/assessments.csv",frames / "semantic_assessments.csv")
    print(json.dumps(counts),flush=True)

REBUILT_TABLES = (
    "cohort_flow.csv",
    "cohort_exclusions.csv",
    "publication_trial_mapping.csv",
    "trial_covariates.csv",
    "semantic_rule_overlap.csv",
    "semantic_rule_categories.csv",
    "registration_lags.csv",
    "first_post_after_start.csv",
    "start_boundary_crossings.csv",
    "registry_change_prevalence.csv",
)

def compare_results(work):
    for name in REBUILT_TABLES:
        reference = ROOT/"results"/name
        ignored=("exposure_label",) if reference.name.startswith("semantic_") else ()
        compare_csv(work/"results"/reference.name,reference,ignored=ignored)
    print("Registry, cohort, and descriptive tables match the frozen study outputs.",flush=True)

def reproduce(output_dir):
    work=Path(output_dir).resolve()
    if work == ROOT or work.is_relative_to(ROOT/"data") or work.is_relative_to(ROOT/"results"):
        raise ValueError("Reproduction output must be separate from immutable study inputs and results")
    if work.exists() and any(work.iterdir()):
        raise FileExistsError("Output directory is not empty; choose a new output directory")
    print(json.dumps(verify_data()),flush=True)
    work.mkdir(parents=True,exist_ok=True)
    run=restore_run(work)
    prepare_tables(run,work)
    from .semantic import prepare_assessments
    prepare_assessments(run,work/"semantic_inputs")
    compare_results(work)
