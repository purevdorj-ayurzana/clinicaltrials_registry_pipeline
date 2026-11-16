from __future__ import annotations

import shutil
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT, RUN_FORMAT_VERSION, config_hash
from .util import (
    atomic_write_csv,
    atomic_write_json,
    canonical_json_hash,
    csv_columns,
    load_json,
    normalize_nct_id,
    read_csv,
    sha256_file,
    source_provenance,
    utc_now_iso,
)


EXPORT_TYPE = "clinicaltrials_gov_history_cohort"
EXPORT_CONTRACTS = {
    1: {
        "classifier_arm": "terra",
        "selection_rule": "terra.final_label == PRIMARY_RCT_REPORT",
    },
    2: {
        "classifier_arm": "sol",
        "selection_rule": "sol.final_label == PRIMARY_RCT_REPORT",
    },
    3: {
        "classifier_arm": "sol",
        "selection_rule": "sol.final_label == PRIMARY_RCT_REPORT",
    },
}

ABSTRACT_REGEX_METHOD = "ABSTRACT_REGEX"
MANUAL_CORRECTION_METHOD = "MANUAL_VERIFIED_CORRECTION"
CURATED_CANDIDATE_RULE = "abstract_regex OR manual_verified_correction"

PUBLICATION_COLUMNS = [
    "source_row_number",
    "pmid",
    "title",
    "abstract",
    "doi",
    "journal",
    "publication_year",
    "publication_date",
    "publication_types",
    "classifier_run_id",
    "classifier_arm",
    "classifier_model",
    "classifier_reasoning_effort",
    "classifier_label",
    "underlying_trial_randomized",
    "publication_role",
    "trial_name_or_acronym",
    "registration_ids",
    "brief_rationale",
    "nct_abstract_regex_found",
    "nct_abstract_registry_numbers",
    "classifier_prompt_hash",
    "classifier_schema_hash",
    "classifier_input_hash",
    "classifier_request_hash",
]

NCT_ASSOCIATION_COLUMNS = [
    "pmid",
    "nct_id_raw",
    "nct_id_normalized",
    "classifier_run_id",
    "classifier_arm",
    "classifier_label",
    "nct_extraction_method",
    "nct_source_field",
]

CORRECTION_COLUMNS = [
    "pmid",
    "nct_id_source_text",
    "nct_id_normalized",
    "nct_source_field",
    "correction_type",
    "correction_reason",
    "publication_evidence_url",
    "registry_evidence_url",
    "verified_date",
]

CORRECTION_TYPES = {
    "MISSING_TOKEN_BOUNDARY",
    "TRUNCATED_NCT_IDENTIFIER",
}

TRIAL_COLUMNS = [
    "nct_id",
    "cohort_source",
    "classifier_run_id",
    "linked_publication_count",
    "earliest_linked_publication_date",
]

LINK_COLUMNS = NCT_ASSOCIATION_COLUMNS + [
    "title",
    "doi",
    "journal",
    "publication_year",
    "publication_date",
    "publication_role",
]


def _expected_manual_associations(
    publications: list[dict[str, str]],
    corrections: list[dict[str, str]],
) -> list[dict[str, str]]:
    publications_by_pmid = {row["pmid"]: row for row in publications}
    seen: set[tuple[str, str]] = set()
    expected: list[dict[str, str]] = []
    for correction in corrections:
        pmid = correction["pmid"]
        publication = publications_by_pmid.get(pmid)
        if publication is None:
            raise ValueError(f"Curated NCT correction references unknown PMID {pmid}.")
        normalized = normalize_nct_id(correction["nct_id_normalized"])
        if normalized != correction["nct_id_normalized"]:
            raise ValueError(
                f"Curated NCT correction for PMID {pmid} has a non-canonical target."
            )
        pair = (pmid, normalized)
        if pair in seen:
            raise ValueError("Curated NCT correction ledger contains duplicate associations.")
        seen.add(pair)
        source_field = correction["nct_source_field"]
        if source_field != "abstract":
            raise ValueError("Curated NCT corrections currently require abstract source text.")
        source_text = correction["nct_id_source_text"]
        if not source_text or source_text.casefold() not in publication[source_field].casefold():
            raise ValueError(
                f"Curated NCT correction source text is absent from PMID {pmid}."
            )
        if publication["nct_abstract_regex_found"] != "FALSE":
            raise ValueError(
                f"Curated NCT correction for PMID {pmid} duplicates a regex-positive publication."
            )
        if correction["correction_type"] not in CORRECTION_TYPES:
            raise ValueError(
                f"Curated NCT correction for PMID {pmid} has an unsupported correction type."
            )
        if not correction["correction_reason"]:
            raise ValueError(
                f"Curated NCT correction for PMID {pmid} has no correction reason."
            )
        if correction["publication_evidence_url"] != (
            f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        ):
            raise ValueError(
                f"Curated NCT correction for PMID {pmid} has an unexpected publication URL."
            )
        if correction["registry_evidence_url"] != (
            f"https://clinicaltrials.gov/study/{normalized}"
        ):
            raise ValueError(
                f"Curated NCT correction for PMID {pmid} has an unexpected registry URL."
            )
        try:
            date.fromisoformat(correction["verified_date"])
        except ValueError as exc:
            raise ValueError(
                f"Curated NCT correction for PMID {pmid} has an invalid verification date."
            ) from exc
        expected.append(
            {
                "pmid": pmid,
                "nct_id_raw": source_text,
                "nct_id_normalized": normalized,
                "classifier_run_id": publication["classifier_run_id"],
                "classifier_arm": publication["classifier_arm"],
                "classifier_label": publication["classifier_label"],
                "nct_extraction_method": MANUAL_CORRECTION_METHOD,
                "nct_source_field": source_field,
            }
        )
    return expected


def verify_classifier_export(export_dir: str | Path) -> dict[str, Any]:
    export_dir = Path(export_dir).resolve()
    manifest_path = export_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Classifier export manifest not found: {manifest_path}")
    manifest = load_json(manifest_path)
    export_format_version = manifest.get("export_format_version")
    contract = (
        EXPORT_CONTRACTS.get(export_format_version)
        if isinstance(export_format_version, int)
        and not isinstance(export_format_version, bool)
        else None
    )
    if contract is None:
        raise ValueError(
            f"Unsupported classifier export format version: {export_format_version!r}."
        )
    if manifest.get("export_type") != EXPORT_TYPE:
        raise ValueError("Unexpected classifier export type.")
    expected_arm = contract["classifier_arm"]
    expected_rule = contract["selection_rule"]
    if manifest.get("classifier_arm") != expected_arm:
        raise ValueError(
            f"Classifier export format version {export_format_version} requires "
            f"classifier_arm {expected_arm!r}."
        )
    if manifest.get("selection_rule") != expected_rule:
        raise ValueError(
            f"Classifier export format version {export_format_version} requires "
            f"selection_rule {expected_rule!r}."
        )

    expected = {
        "selected_publications.csv": PUBLICATION_COLUMNS,
        "nct_candidates.csv": NCT_ASSOCIATION_COLUMNS,
    }
    if export_format_version == 3:
        expected["nct_candidate_corrections.csv"] = CORRECTION_COLUMNS
    loaded: dict[str, list[dict[str, str]]] = {}
    for filename, columns in expected.items():
        path = export_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"Classifier export file is missing: {path}")
        record = (manifest.get("files") or {}).get(filename)
        if not isinstance(record, dict):
            raise ValueError(f"Manifest is missing the file record for {filename}.")
        if sha256_file(path) != record.get("sha256"):
            raise ValueError(f"Classifier export hash mismatch for {filename}.")
        if csv_columns(path) != columns or record.get("columns") != columns:
            raise ValueError(f"Classifier export schema mismatch for {filename}.")
        rows = read_csv(path)
        if len(rows) != int(record.get("rows", -1)):
            raise ValueError(f"Classifier export row-count mismatch for {filename}.")
        loaded[filename] = rows

    publications = loaded["selected_publications.csv"]
    associations = loaded["nct_candidates.csv"]
    pmids = [row["pmid"] for row in publications]
    if not pmids or any(not pmid for pmid in pmids) or len(pmids) != len(set(pmids)):
        raise ValueError("Classifier export has invalid PMID identity.")
    if {row["classifier_arm"] for row in publications} != {expected_arm}:
        raise ValueError(
            f"Selected publications must all use classifier_arm {expected_arm!r} "
            f"for export format version {export_format_version}."
        )
    if {row["classifier_label"] for row in publications} != {"PRIMARY_RCT_REPORT"}:
        raise ValueError("Selected publications must all be PRIMARY_RCT_REPORT.")
    if associations and {row["classifier_arm"] for row in associations} != {expected_arm}:
        raise ValueError(
            f"NCT associations must all use classifier_arm {expected_arm!r} "
            f"for export format version {export_format_version}."
        )
    if associations and {
        row["classifier_label"] for row in associations
    } != {"PRIMARY_RCT_REPORT"}:
        raise ValueError("NCT associations must all be PRIMARY_RCT_REPORT.")

    publication_pmids = set(pmids)
    pairs: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    for row in associations:
        if row["pmid"] not in publication_pmids:
            raise ValueError("Classifier export has an NCT association for an unknown PMID.")
        normalized = normalize_nct_id(row["nct_id_normalized"])
        if normalized != row["nct_id_normalized"]:
            raise ValueError("Classifier export contains a non-canonical normalized NCT ID.")
        pair = (row["pmid"], normalized)
        if pair in seen_pairs:
            raise ValueError("Classifier export contains duplicate PMID-NCT associations.")
        seen_pairs.add(pair)
        pairs.append(f"{pair[0]}|{pair[1]}")

    flagged = {
        row["pmid"]
        for row in publications
        if row["nct_abstract_regex_found"] == "TRUE"
    }
    if export_format_version == 3:
        methods = {row["nct_extraction_method"] for row in associations}
        if not methods.issubset({ABSTRACT_REGEX_METHOD, MANUAL_CORRECTION_METHOD}):
            raise ValueError("Curated classifier export contains an unsupported NCT method.")
        regex_associations = [
            row
            for row in associations
            if row["nct_extraction_method"] == ABSTRACT_REGEX_METHOD
        ]
        manual_associations = [
            row
            for row in associations
            if row["nct_extraction_method"] == MANUAL_CORRECTION_METHOD
        ]
        if {row["pmid"] for row in regex_associations} != flagged:
            raise ValueError(
                "Curated classifier export regex associations do not match publication NCT flags."
            )
        corrections = loaded["nct_candidate_corrections.csv"]
        expected_manual = _expected_manual_associations(publications, corrections)
        if manual_associations != expected_manual:
            raise ValueError(
                "Curated classifier export manual associations do not match its correction ledger."
            )
        if manifest.get("candidate_selection_rule") != CURATED_CANDIDATE_RULE:
            raise ValueError("Curated classifier export candidate-selection rule mismatch.")
        if manifest.get("correction_set_hash") != canonical_json_hash(corrections):
            raise ValueError("Curated classifier export correction-set hash mismatch.")
        base_export = manifest.get("base_export")
        if not isinstance(base_export, dict):
            raise ValueError("Curated classifier export has no base-export lineage.")
        if base_export.get("export_format_version") != 2:
            raise ValueError("Curated classifier export must derive from format version 2.")
        if base_export.get("classifier_run_id") != manifest.get("classifier_run_id"):
            raise ValueError("Curated classifier export classifier lineage mismatch.")
        if base_export.get("selected_publications_sha256") != sha256_file(
            export_dir / "selected_publications.csv"
        ):
            raise ValueError("Curated classifier export changed its base publication file.")
        regex_pairs = sorted(
            f"{row['pmid']}|{row['nct_id_normalized']}" for row in regex_associations
        )
        if base_export.get("association_hash") != canonical_json_hash(regex_pairs):
            raise ValueError("Curated classifier export regex lineage hash mismatch.")
        regex_nct_ids = sorted(
            {row["nct_id_normalized"] for row in regex_associations}
        )
        if base_export.get("unique_nct_id_hash") != canonical_json_hash(regex_nct_ids):
            raise ValueError("Curated classifier export base NCT lineage hash mismatch.")
    else:
        corrections = []
        manual_associations = []
        if flagged != {row["pmid"] for row in associations}:
            raise ValueError("NCT candidates do not match publication NCT flags.")

    associated = {row["pmid"] for row in associations}
    unique_nct_ids = sorted({row["nct_id_normalized"] for row in associations})
    checks = {
        "selected_publications": len(publications),
        "selected_publications_with_nct": len(associated),
        "selected_publications_without_nct": len(publications) - len(associated),
        "pmid_nct_associations": len(associations),
        "unique_candidate_nct_ids": len(unique_nct_ids),
    }
    if export_format_version == 3:
        checks.update(
            {
                "selected_publications_with_abstract_regex_nct": len(flagged),
                "selected_publications_with_manual_verified_correction": len(
                    {row["pmid"] for row in manual_associations}
                ),
                "manual_verified_corrections": len(manual_associations),
            }
        )
    for name, count in checks.items():
        if int(manifest.get(name, -1)) != count:
            raise ValueError(f"Classifier export manifest count mismatch for {name}.")
    if manifest.get("selected_pmid_hash") != canonical_json_hash(sorted(pmids)):
        raise ValueError("Classifier export selected PMID hash mismatch.")
    if manifest.get("association_hash") != canonical_json_hash(sorted(pairs)):
        raise ValueError("Classifier export association hash mismatch.")
    if manifest.get("unique_nct_id_hash") != canonical_json_hash(unique_nct_ids):
        raise ValueError("Classifier export unique NCT ID hash mismatch.")
    return {
        "manifest": manifest,
        "publications": publications,
        "associations": associations,
        "corrections": corrections,
        "export_files": ["manifest.json", *expected],
    }


def _new_run_id(nct_ids: list[str]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{canonical_json_hash(nct_ids)[:8]}"


def ingest_classifier_export(
    *,
    export_dir: str | Path,
    config: dict[str, Any],
    output_root: str | Path | None = None,
) -> Path:
    verified = verify_classifier_export(export_dir)
    manifest = verified["manifest"]
    publications = verified["publications"]
    associations = verified["associations"]
    publications_by_pmid = {row["pmid"]: row for row in publications}
    links: list[dict[str, str]] = []
    by_nct: dict[str, list[dict[str, str]]] = defaultdict(list)
    for association in associations:
        publication = publications_by_pmid[association["pmid"]]
        link = dict(association)
        for field in LINK_COLUMNS[len(NCT_ASSOCIATION_COLUMNS) :]:
            link[field] = publication.get(field, "")
        links.append(link)
        by_nct[association["nct_id_normalized"]].append(link)

    all_nct_ids = sorted(by_nct)
    trial_rows: list[dict[str, Any]] = []
    for nct_id in all_nct_ids:
        linked = by_nct[nct_id]
        dates = sorted(row["publication_date"] for row in linked if row["publication_date"])
        trial_rows.append(
            {
                "nct_id": nct_id,
                "cohort_source": "CLASSIFIER_EXPORT",
                "classifier_run_id": manifest["classifier_run_id"],
                "linked_publication_count": len({row["pmid"] for row in linked}),
                "earliest_linked_publication_date": dates[0] if dates else "",
            }
        )

    if not all_nct_ids:
        raise ValueError("The cohort would contain no NCT IDs.")
    run_id = _new_run_id(all_nct_ids)
    root = Path(output_root or config["run_output_root"])
    if not root.is_absolute():
        root = ROOT / root
    run_dir = root / run_id
    if run_dir.exists():
        raise FileExistsError(f"Refusing to overwrite existing ClinicalTrials API run: {run_dir}")

    upstream_dir = run_dir / "input" / "upstream_classifier"
    upstream_dir.mkdir(parents=True)
    export_dir = Path(export_dir).resolve()
    export_files = verified["export_files"]
    for filename in export_files:
        shutil.copy2(export_dir / filename, upstream_dir / filename)
    atomic_write_csv(run_dir / "input" / "trials.csv", trial_rows, TRIAL_COLUMNS)
    atomic_write_csv(run_dir / "input" / "publication_trial_links.csv", links, LINK_COLUMNS)
    atomic_write_json(run_dir / "config_snapshot.json", config)

    run_manifest = {
        "run_format_version": RUN_FORMAT_VERSION,
        "run_type": "CLASSIFIER_COHORT",
        "run_id": run_id,
        "project_name": config["project_name"],
        "created_at_utc": utc_now_iso(),
        "workflow": "explicit_stages",
        "config_hash": config_hash(config),
        "classifier_export": {
            "export_format_version": manifest["export_format_version"],
            "classifier_run_id": manifest["classifier_run_id"],
            "classifier_arm": manifest["classifier_arm"],
            "selection_rule": manifest["selection_rule"],
            "source_manifest_sha256": sha256_file(export_dir / "manifest.json"),
            "copied_files": {
                filename: sha256_file(upstream_dir / filename)
                for filename in export_files
            },
        },
        "trial_count": len(trial_rows),
        "classifier_candidate_count": len(by_nct),
        "cohort_nct_ids": all_nct_ids,
        "cohort_hash": canonical_json_hash(all_nct_ids),
        "input_files": {
            "config_snapshot.json": sha256_file(run_dir / "config_snapshot.json"),
            "trials.csv": sha256_file(run_dir / "input" / "trials.csv"),
            "publication_trial_links.csv": sha256_file(
                run_dir / "input" / "publication_trial_links.csv"
            ),
        },
        "stage_status": {
            "ingest": "COMPLETED",
            "download": "PENDING",
            "extract": "PENDING",
            "compare": "PENDING",
            "summarize": "PENDING",
        },
        "stage_provenance": {"ingest": {"recorded_at_utc": utc_now_iso(), **source_provenance(ROOT)}},
    }
    atomic_write_json(run_dir / "run_manifest.json", run_manifest)
    return run_dir
