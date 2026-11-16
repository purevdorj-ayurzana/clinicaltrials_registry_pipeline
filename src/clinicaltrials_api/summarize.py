from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .download import _update_run_manifest, validate_run
from .identity import identity_rows_from_run
from .util import atomic_write_csv, load_json, read_csv


TRIAL_SUMMARY_COLUMNS = [
    "nct_id",
    "source_nct_ids",
    "cohort_alias_nct_ids",
    "official_aliases",
    "identity_status",
    "cohort_source",
    "linked_publication_count",
    "earliest_linked_publication_date",
    "validation_status",
    "retrieval_status",
    "version_count",
    "first_posting_date",
    "latest_posting_date",
    "latest_study_type",
    "field_change_count",
    "nested_change_count",
    "review_flag_count",
    "review_flag_codes",
    "review_change_subtype_codes",
    "error_count",
]

MANUAL_REVIEW_COLUMNS = [
    "nct_id",
    "linked_pmids",
    "linked_publication_titles",
    "flag_code",
    "change_subtype_code",
    "from_version",
    "to_version",
    "event_posting_date",
    "field_or_domain",
    "old_value",
    "new_value",
    "rule",
    "after_study_start",
    "after_primary_completion",
    "after_completion",
    "after_earliest_publication",
    "review_status",
    "review_notes",
]

ERROR_COLUMNS = [
    "stage",
    "nct_id",
    "version",
    "error_type",
    "message",
    "at_utc",
    "resolution_status",
]


def summarize_run(
    *, run_dir: str | Path, config: dict[str, Any], replace: bool = False
) -> dict[str, int]:
    run_dir, manifest = validate_run(run_dir, config)
    if manifest.get("stage_status", {}).get("compare") != "COMPLETED":
        raise ValueError("Comparison stage must complete before summarization.")
    report_dir = run_dir / "reports"
    summary_path = report_dir / "trial_summary.csv"
    review_path = report_dir / "manual_review.csv"
    errors_path = report_dir / "errors.csv"
    existing = [str(path) for path in (summary_path, review_path, errors_path) if path.exists()]
    if existing and not replace:
        raise FileExistsError(
            "Summary files already exist; use --replace deliberately: " + ", ".join(existing)
        )

    trials = read_csv(run_dir / "processed" / "canonical_trials.csv")
    links = read_csv(run_dir / "input" / "publication_trial_links.csv")
    identities = identity_rows_from_run(run_dir)
    source_to_canonical = {
        row["source_nct_id"]: row["canonical_nct_id"] for row in identities
    }
    snapshots = read_csv(run_dir / "processed" / "version_snapshots.csv")
    field_changes = read_csv(run_dir / "processed" / "field_changes.csv")
    nested_changes = read_csv(run_dir / "processed" / "nested_changes.csv")
    flags = read_csv(report_dir / "review_flags.csv")
    extraction_errors = read_csv(run_dir / "processed" / "extraction_errors.csv")

    snapshots_by_nct: dict[str, list[dict[str, str]]] = defaultdict(list)
    links_by_nct: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in snapshots:
        snapshots_by_nct[row["nct_id"]].append(row)
    for row in links:
        canonical_nct_id = source_to_canonical.get(
            row["nct_id_normalized"], row["nct_id_normalized"]
        )
        links_by_nct[canonical_nct_id].append(row)
    field_counts = Counter(row["nct_id"] for row in field_changes)
    nested_counts = Counter(row["nct_id"] for row in nested_changes)
    flags_by_nct: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in flags:
        flags_by_nct[row["nct_id"]].append(row)

    error_rows: list[dict[str, Any]] = []
    error_counts: Counter[str] = Counter()
    raw_root = run_dir / "raw" / "clinicaltrials_gov"
    for identity in identities:
        source_nct_id = identity["source_nct_id"]
        canonical_nct_id = identity["canonical_nct_id"]
        retrieval_path = raw_root / source_nct_id / "retrieval_manifest.json"
        if retrieval_path.is_file():
            retrieval = load_json(retrieval_path)
            resolved = retrieval.get("retrieval_status") == "COMPLETED"
            for error in retrieval.get("errors", []):
                error_rows.append(
                    {
                        "stage": "download",
                        "nct_id": source_nct_id,
                        "version": "",
                        "error_type": error.get("error_type", ""),
                        "message": error.get("message", ""),
                        "at_utc": error.get("at_utc", ""),
                        "resolution_status": "RESOLVED" if resolved else "UNRESOLVED",
                    }
                )
                if not resolved:
                    error_counts[canonical_nct_id] += 1
        else:
            error_rows.append(
                {
                    "stage": "download",
                    "nct_id": source_nct_id,
                    "version": "",
                    "error_type": "MISSING_RETRIEVAL_MANIFEST",
                    "message": "No retrieval manifest exists.",
                    "at_utc": "",
                    "resolution_status": "UNRESOLVED",
                }
            )
            error_counts[canonical_nct_id] += 1
    for error in extraction_errors:
        error_rows.append(
            {
                "stage": "extract",
                "nct_id": error["nct_id"],
                "version": error["version"],
                "error_type": error["error_type"],
                "message": error["message"],
                "at_utc": "",
                "resolution_status": "UNRESOLVED",
            }
        )
        error_counts[error["nct_id"]] += 1

    summary_rows: list[dict[str, Any]] = []
    for trial in trials:
        nct_id = trial["nct_id"]
        rows = sorted(snapshots_by_nct.get(nct_id, []), key=lambda row: int(row["version"]))
        retrieval_path = raw_root / nct_id / "retrieval_manifest.json"
        retrieval = load_json(retrieval_path) if retrieval_path.is_file() else {}
        dates = [row["posting_date"] for row in rows if row["posting_date"]]
        trial_flags = flags_by_nct.get(nct_id, [])
        summary_rows.append(
            {
                "nct_id": nct_id,
                "source_nct_ids": trial["source_nct_ids"],
                "cohort_alias_nct_ids": trial["cohort_alias_nct_ids"],
                "official_aliases": trial["official_aliases"],
                "identity_status": trial["identity_status"],
                "cohort_source": trial["cohort_source"],
                "linked_publication_count": trial["linked_publication_count"],
                "earliest_linked_publication_date": trial["earliest_linked_publication_date"],
                "validation_status": retrieval.get("validation_status", "NOT_ATTEMPTED"),
                "retrieval_status": retrieval.get("retrieval_status", "NOT_ATTEMPTED"),
                "version_count": len(rows),
                "first_posting_date": min(dates) if dates else "",
                "latest_posting_date": max(dates) if dates else "",
                "latest_study_type": rows[-1]["study_type"] if rows else "",
                "field_change_count": field_counts[nct_id],
                "nested_change_count": nested_counts[nct_id],
                "review_flag_count": len(trial_flags),
                "review_flag_codes": sorted({row["flag_code"] for row in trial_flags}),
                "review_change_subtype_codes": sorted(
                    {
                        row["change_subtype_code"]
                        for row in trial_flags
                        if row.get("change_subtype_code")
                    }
                ),
                "error_count": error_counts[nct_id],
            }
        )

    manual_rows: list[dict[str, Any]] = []
    for flag in flags:
        linked = links_by_nct.get(flag["nct_id"], [])
        manual_rows.append(
            {
                "nct_id": flag["nct_id"],
                "linked_pmids": sorted({row["pmid"] for row in linked}),
                "linked_publication_titles": sorted(
                    {row["title"] for row in linked if row["title"]}
                ),
                **flag,
                "review_status": "PENDING",
                "review_notes": "",
            }
        )

    atomic_write_csv(summary_path, summary_rows, TRIAL_SUMMARY_COLUMNS, overwrite=replace)
    atomic_write_csv(review_path, manual_rows, MANUAL_REVIEW_COLUMNS, overwrite=replace)
    atomic_write_csv(errors_path, error_rows, ERROR_COLUMNS, overwrite=replace)
    _update_run_manifest(run_dir, stage="summarize", status="COMPLETED")
    return {
        "trials": len(summary_rows),
        "manual_review_rows": len(manual_rows),
        "error_events": len(error_rows),
        "unresolved_errors": sum(
            row["resolution_status"] == "UNRESOLVED" for row in error_rows
        ),
    }


def run_status(run_dir: str | Path) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    manifest = load_json(run_dir / "run_manifest.json")
    result: dict[str, Any] = {
        "run_id": manifest.get("run_id"),
        "trial_count": manifest.get("trial_count"),
        "canonical_trial_count": manifest.get("identity_resolution", {}).get(
            "canonical_trial_count", manifest.get("trial_count")
        ),
        "official_alias_count": manifest.get("identity_resolution", {}).get(
            "official_alias_count", 0
        ),
        "stage_status": manifest.get("stage_status", {}),
    }
    summary_path = run_dir / "reports" / "trial_summary.csv"
    if summary_path.is_file():
        rows = read_csv(summary_path)
        result["retrieval_status_counts"] = dict(Counter(row["retrieval_status"] for row in rows))
        result["total_versions"] = sum(int(row["version_count"] or 0) for row in rows)
        result["total_review_flags"] = sum(int(row["review_flag_count"] or 0) for row in rows)
        result["total_errors"] = sum(int(row["error_count"] or 0) for row in rows)
    return result
