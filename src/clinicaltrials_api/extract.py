from __future__ import annotations

from pathlib import Path
from typing import Any

from .download import _update_run_manifest, validate_run
from .identity import (
    CANONICAL_TRIAL_COLUMNS,
    canonical_trial_rows,
    identity_rows_from_run,
)
from .util import (
    atomic_write_csv,
    canonical_json,
    canonical_json_hash,
    date_precision,
    load_json,
    path_get,
    sha256_file,
    utc_now_iso,
)


VERSION_INDEX_COLUMNS = [
    "nct_id",
    "version",
    "history_date",
    "last_update_submit_qc_date",
    "history_status",
    "history_study_type",
    "module_labels",
]

SNAPSHOT_BASE_COLUMNS = [
    "nct_id",
    "version",
    "history_date",
    "submission_date",
    "posting_date",
    "posting_date_type",
    "posting_date_precision",
    "has_results",
]

SNAPSHOT_TRAILER_COLUMNS = ["raw_file_path", "raw_sha256", "extracted_at_utc"]

SNAPSHOT_FIELD_COLUMNS = [
    "nct_id",
    "version",
    "field",
    "field_path",
    "value_state",
    "value_json",
]

OUTCOME_COLUMNS = [
    "nct_id",
    "version",
    "outcome_type",
    "position",
    "measure",
    "description",
    "time_frame",
    "item_hash",
    "source_json",
]

ARM_COLUMNS = [
    "nct_id",
    "version",
    "position",
    "arm_label",
    "arm_type",
    "description",
    "intervention_names",
    "item_hash",
    "source_json",
]

INTERVENTION_COLUMNS = [
    "nct_id",
    "version",
    "position",
    "intervention_type",
    "intervention_name",
    "description",
    "other_names",
    "arm_labels",
    "item_hash",
    "source_json",
]

ELIGIBILITY_COLUMNS = [
    "nct_id",
    "version",
    "eligibility_criteria",
    "minimum_age",
    "maximum_age",
    "sex",
    "healthy_volunteers",
    "criteria_hash",
]

EXTRACTION_ERROR_COLUMNS = ["nct_id", "version", "error_type", "message"]


def _study_from_version(payload: Any, nct_id: str, version: int) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("studyVersion") != version:
        raise ValueError(f"Unexpected version payload for {nct_id} version {version}.")
    study = payload.get("study")
    if not isinstance(study, dict) or not isinstance(study.get("protocolSection"), dict):
        raise ValueError(f"Historical study is missing for {nct_id} version {version}.")
    return study


def _get_dict(document: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = document
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _get_list(document: dict[str, Any], *keys: str) -> list[Any]:
    current: Any = document
    for key in keys:
        if not isinstance(current, dict):
            return []
        current = current.get(key)
    return current if isinstance(current, list) else []


def extract_run(
    *, run_dir: str | Path, config: dict[str, Any], replace: bool = False
) -> dict[str, int]:
    run_dir, run_manifest = validate_run(run_dir, config)
    if run_manifest.get("stage_status", {}).get("download") not in {
        "COMPLETED",
        "COMPLETED_WITH_ERRORS",
    }:
        raise ValueError("Download stage must complete before extraction.")
    output_dir = run_dir / "processed"
    outputs = {
        "canonical_trials": output_dir / "canonical_trials.csv",
        "version_index": output_dir / "version_index.csv",
        "version_snapshots": output_dir / "version_snapshots.csv",
        "snapshot_fields": output_dir / "snapshot_fields.csv",
        "outcomes": output_dir / "outcomes.csv",
        "arms": output_dir / "arms.csv",
        "interventions": output_dir / "interventions.csv",
        "eligibility": output_dir / "eligibility_versions.csv",
        "errors": output_dir / "extraction_errors.csv",
    }
    existing = [str(path) for path in outputs.values() if path.exists()]
    if existing and not replace:
        raise FileExistsError(
            "Derived extraction files already exist; use --replace deliberately: "
            + ", ".join(existing)
        )

    index_rows: list[dict[str, Any]] = []
    snapshot_rows: list[dict[str, Any]] = []
    field_rows: list[dict[str, Any]] = []
    outcome_rows: list[dict[str, Any]] = []
    arm_rows: list[dict[str, Any]] = []
    intervention_rows: list[dict[str, Any]] = []
    eligibility_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    extracted_at = utc_now_iso()
    identities = identity_rows_from_run(run_dir)
    canonical_trials = canonical_trial_rows(run_dir, identities)

    for trial in canonical_trials:
        nct_id = trial["nct_id"]
        raw_dir = run_dir / "raw" / "clinicaltrials_gov" / nct_id
        retrieval_path = raw_dir / "retrieval_manifest.json"
        if not retrieval_path.is_file():
            error_rows.append(
                {"nct_id": nct_id, "version": "", "error_type": "MISSING_RETRIEVAL", "message": "No retrieval manifest."}
            )
            continue
        retrieval = load_json(retrieval_path)
        if retrieval.get("retrieval_status") != "COMPLETED":
            error_rows.append(
                {
                    "nct_id": nct_id,
                    "version": "",
                    "error_type": "INCOMPLETE_RETRIEVAL",
                    "message": retrieval.get("validation_status", "") + ": " + retrieval.get("retrieval_status", ""),
                }
            )
            continue
        index_payload = load_json(raw_dir / "history_index.json")
        changes = index_payload.get("history", {}).get("changes", [])
        history_by_version = {change["version"]: change for change in changes}
        expected_versions = retrieval.get("expected_versions", [])
        if sorted(history_by_version) != expected_versions:
            raise ValueError(f"History index no longer reconciles for {nct_id}.")

        for version in expected_versions:
            try:
                change = history_by_version[version]
                index_rows.append(
                    {
                        "nct_id": nct_id,
                        "version": version,
                        "history_date": change.get("date", ""),
                        "last_update_submit_qc_date": change.get("lastUpdateSubmitQcDate", ""),
                        "history_status": change.get("status", ""),
                        "history_study_type": change.get("studyType", ""),
                        "module_labels": change.get("moduleLabels", []),
                    }
                )
                raw_path = raw_dir / f"version_{version:04d}.json"
                payload = load_json(raw_path)
                study = _study_from_version(payload, nct_id, version)
                status = _get_dict(study, "protocolSection", "statusModule")
                post = status.get("lastUpdatePostDateStruct") or {}
                if not isinstance(post, dict):
                    post = {}
                snapshot: dict[str, Any] = {
                    "nct_id": nct_id,
                    "version": version,
                    "history_date": change.get("date", ""),
                    "submission_date": status.get("lastUpdateSubmitDate", ""),
                    "posting_date": post.get("date", ""),
                    "posting_date_type": post.get("type", ""),
                    "posting_date_precision": date_precision(str(post.get("date", ""))),
                    "has_results": bool(study.get("hasResults", False)),
                }
                for field in config["scalar_fields"]:
                    state, value = path_get(study, field["path"])
                    snapshot[field["label"]] = value
                    field_rows.append(
                        {
                            "nct_id": nct_id,
                            "version": version,
                            "field": field["label"],
                            "field_path": field["path"],
                            "value_state": state,
                            "value_json": canonical_json(value),
                        }
                    )
                snapshot.update(
                    {
                        "raw_file_path": str(raw_path.relative_to(run_dir)),
                        "raw_sha256": sha256_file(raw_path),
                        "extracted_at_utc": extracted_at,
                    }
                )
                snapshot_rows.append(snapshot)

                outcomes = _get_dict(study, "protocolSection", "outcomesModule")
                for outcome_type, key in (
                    ("PRIMARY", "primaryOutcomes"),
                    ("SECONDARY", "secondaryOutcomes"),
                    ("OTHER", "otherOutcomes"),
                ):
                    items = outcomes.get(key, []) if isinstance(outcomes.get(key, []), list) else []
                    for position, item in enumerate(items, start=1):
                        if not isinstance(item, dict):
                            continue
                        outcome_rows.append(
                            {
                                "nct_id": nct_id,
                                "version": version,
                                "outcome_type": outcome_type,
                                "position": position,
                                "measure": item.get("measure", ""),
                                "description": item.get("description", ""),
                                "time_frame": item.get("timeFrame", ""),
                                "item_hash": canonical_json_hash(
                                    {"outcome_type": outcome_type, "item": item}
                                ),
                                "source_json": item,
                            }
                        )

                arms_module = _get_dict(study, "protocolSection", "armsInterventionsModule")
                arm_items = arms_module.get("armGroups", [])
                for position, item in enumerate(arm_items if isinstance(arm_items, list) else [], start=1):
                    if not isinstance(item, dict):
                        continue
                    arm_rows.append(
                        {
                            "nct_id": nct_id,
                            "version": version,
                            "position": position,
                            "arm_label": item.get("label", ""),
                            "arm_type": item.get("type", ""),
                            "description": item.get("description", ""),
                            "intervention_names": item.get("interventionNames", []),
                            "item_hash": canonical_json_hash(item),
                            "source_json": item,
                        }
                    )
                intervention_items = arms_module.get("interventions", [])
                for position, item in enumerate(
                    intervention_items if isinstance(intervention_items, list) else [], start=1
                ):
                    if not isinstance(item, dict):
                        continue
                    intervention_rows.append(
                        {
                            "nct_id": nct_id,
                            "version": version,
                            "position": position,
                            "intervention_type": item.get("type", ""),
                            "intervention_name": item.get("name", ""),
                            "description": item.get("description", ""),
                            "other_names": item.get("otherNames", []),
                            "arm_labels": item.get("armGroupLabels", []),
                            "item_hash": canonical_json_hash(item),
                            "source_json": item,
                        }
                    )

                eligibility = _get_dict(study, "protocolSection", "eligibilityModule")
                criteria = eligibility.get("eligibilityCriteria", "")
                eligibility_rows.append(
                    {
                        "nct_id": nct_id,
                        "version": version,
                        "eligibility_criteria": criteria,
                        "minimum_age": eligibility.get("minimumAge", ""),
                        "maximum_age": eligibility.get("maximumAge", ""),
                        "sex": eligibility.get("sex", ""),
                        "healthy_volunteers": eligibility.get("healthyVolunteers", ""),
                        "criteria_hash": canonical_json_hash(criteria),
                    }
                )
            except (KeyError, TypeError, ValueError, OSError) as exc:
                error_rows.append(
                    {
                        "nct_id": nct_id,
                        "version": version,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )

    scalar_columns = [field["label"] for field in config["scalar_fields"]]
    atomic_write_csv(
        outputs["canonical_trials"],
        canonical_trials,
        CANONICAL_TRIAL_COLUMNS,
        overwrite=replace,
    )
    atomic_write_csv(outputs["version_index"], index_rows, VERSION_INDEX_COLUMNS, overwrite=replace)
    atomic_write_csv(
        outputs["version_snapshots"],
        snapshot_rows,
        SNAPSHOT_BASE_COLUMNS + scalar_columns + SNAPSHOT_TRAILER_COLUMNS,
        overwrite=replace,
    )
    atomic_write_csv(outputs["snapshot_fields"], field_rows, SNAPSHOT_FIELD_COLUMNS, overwrite=replace)
    atomic_write_csv(outputs["outcomes"], outcome_rows, OUTCOME_COLUMNS, overwrite=replace)
    atomic_write_csv(outputs["arms"], arm_rows, ARM_COLUMNS, overwrite=replace)
    atomic_write_csv(
        outputs["interventions"], intervention_rows, INTERVENTION_COLUMNS, overwrite=replace
    )
    atomic_write_csv(outputs["eligibility"], eligibility_rows, ELIGIBILITY_COLUMNS, overwrite=replace)
    atomic_write_csv(outputs["errors"], error_rows, EXTRACTION_ERROR_COLUMNS, overwrite=replace)
    _update_run_manifest(
        run_dir, stage="extract", status="COMPLETED" if not error_rows else "COMPLETED_WITH_ERRORS"
    )
    return {
        "trials": len(canonical_trials),
        "versions": len(snapshot_rows),
        "fields": len(field_rows),
        "outcomes": len(outcome_rows),
        "arms": len(arm_rows),
        "interventions": len(intervention_rows),
        "eligibility_versions": len(eligibility_rows),
        "errors": len(error_rows),
    }
