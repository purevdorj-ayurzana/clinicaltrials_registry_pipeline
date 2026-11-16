from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .util import load_json, normalize_nct_id, read_csv


IDENTITY_RESOLUTION_COLUMNS = [
    "source_nct_id",
    "canonical_nct_id",
    "identity_status",
    "official_aliases",
    "history_storage_nct_id",
    "current_response_path",
    "current_sha256",
    "resolved_at_utc",
    "retrieval_status",
]

CANONICAL_TRIAL_COLUMNS = [
    "nct_id",
    "source_nct_ids",
    "cohort_alias_nct_ids",
    "official_aliases",
    "identity_status",
    "cohort_source",
    "classifier_run_id",
    "linked_publication_count",
    "earliest_linked_publication_date",
]


def resolve_current_identity(data: Any, source_nct_id: str) -> dict[str, Any]:
    """Resolve one requested NCT ID using only the official current-record payload."""
    if not isinstance(data, dict) or not isinstance(data.get("protocolSection"), dict):
        raise ValueError(f"Current study response schema changed for {source_nct_id}.")
    identification = data["protocolSection"].get("identificationModule")
    if not isinstance(identification, dict):
        raise ValueError(f"Current study identification module is missing for {source_nct_id}.")

    actual = identification.get("nctId")
    if not isinstance(actual, str) or normalize_nct_id(actual) != actual:
        raise ValueError(
            f"Current study returned an invalid canonical NCT ID {actual!r} "
            f"for {source_nct_id}."
        )
    aliases_value = identification.get("nctIdAliases", [])
    if aliases_value is None:
        aliases_value = []
    if not isinstance(aliases_value, list) or not all(
        isinstance(value, str) for value in aliases_value
    ):
        raise ValueError(f"Current study returned invalid NCT aliases for {source_nct_id}.")
    aliases: list[str] = []
    for value in aliases_value:
        normalized = normalize_nct_id(value)
        if normalized != value:
            raise ValueError(
                f"Current study returned a non-canonical alias {value!r} for {source_nct_id}."
            )
        aliases.append(normalized)
    aliases = sorted(set(aliases))

    if actual == source_nct_id:
        identity_status = "CANONICAL"
    elif source_nct_id in aliases:
        identity_status = "OFFICIAL_ALIAS"
    else:
        raise ValueError(
            f"Current study returned NCT ID {actual!r}, expected {source_nct_id}; "
            "the requested identifier is not listed in nctIdAliases."
        )
    return {
        "source_nct_id": source_nct_id,
        "canonical_nct_id": actual,
        "identity_status": identity_status,
        "official_aliases": aliases,
    }


def identity_rows_from_run(run_dir: str | Path) -> list[dict[str, Any]]:
    """Read identity metadata, with compatibility for pre-alias retrieval manifests."""
    run_dir = Path(run_dir)
    raw_root = run_dir / "raw" / "clinicaltrials_gov"
    rows: list[dict[str, Any]] = []
    for trial in read_csv(run_dir / "input" / "trials.csv"):
        source_nct_id = trial["nct_id"]
        trial_dir = raw_root / source_nct_id
        retrieval_path = trial_dir / "retrieval_manifest.json"
        retrieval = load_json(retrieval_path) if retrieval_path.is_file() else {}
        canonical_nct_id = retrieval.get("canonical_nct_id", source_nct_id)
        official_aliases = retrieval.get("official_aliases", [])
        if isinstance(official_aliases, str):
            try:
                official_aliases = json.loads(official_aliases)
            except json.JSONDecodeError:
                official_aliases = []
        if not isinstance(official_aliases, list):
            official_aliases = []
        identity_status = retrieval.get("identity_status")
        if not identity_status:
            identity_status = (
                "OFFICIAL_ALIAS" if canonical_nct_id != source_nct_id else "CANONICAL"
            )
        current_record = retrieval.get("files", {}).get("current", {})
        rows.append(
            {
                "source_nct_id": source_nct_id,
                "canonical_nct_id": canonical_nct_id,
                "identity_status": identity_status,
                "official_aliases": sorted(set(official_aliases)),
                "history_storage_nct_id": retrieval.get(
                    "history_storage_nct_id", canonical_nct_id
                ),
                "current_response_path": (
                    str((trial_dir / "current.json").relative_to(run_dir))
                    if (trial_dir / "current.json").is_file()
                    else ""
                ),
                "current_sha256": current_record.get("sha256", ""),
                "resolved_at_utc": retrieval.get("identity_resolved_at_utc", ""),
                "retrieval_status": retrieval.get("retrieval_status", "NOT_ATTEMPTED"),
            }
        )
    return rows


def canonical_trial_rows(
    run_dir: str | Path, identities: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    run_dir = Path(run_dir)
    identities = identities or identity_rows_from_run(run_dir)
    identity_by_source = {row["source_nct_id"]: row for row in identities}
    trials = read_csv(run_dir / "input" / "trials.csv")
    links = read_csv(run_dir / "input" / "publication_trial_links.csv")
    trials_by_source = {row["nct_id"]: row for row in trials}
    links_by_source: dict[str, list[dict[str, str]]] = defaultdict(list)
    for link in links:
        links_by_source[link["nct_id_normalized"]].append(link)

    sources_by_canonical: dict[str, list[str]] = defaultdict(list)
    for source_nct_id in sorted(trials_by_source):
        identity = identity_by_source.get(source_nct_id)
        canonical_nct_id = (
            identity["canonical_nct_id"] if identity else source_nct_id
        )
        sources_by_canonical[canonical_nct_id].append(source_nct_id)

    rows: list[dict[str, Any]] = []
    for canonical_nct_id in sorted(sources_by_canonical):
        source_nct_ids = sorted(sources_by_canonical[canonical_nct_id])
        source_trials = [trials_by_source[value] for value in source_nct_ids]
        cohort_sources = {row["cohort_source"] for row in source_trials}
        classifier_run_ids = {row["classifier_run_id"] for row in source_trials}
        if len(cohort_sources) != 1 or len(classifier_run_ids) != 1:
            raise ValueError(
                f"Canonical trial {canonical_nct_id} spans inconsistent cohort provenance."
            )
        aliases = sorted(
            {
                alias
                for source in source_nct_ids
                for alias in identity_by_source.get(source, {}).get("official_aliases", [])
            }
        )
        cohort_aliases = sorted(
            source
            for source in source_nct_ids
            if identity_by_source.get(source, {}).get("identity_status") == "OFFICIAL_ALIAS"
        )
        linked = [link for source in source_nct_ids for link in links_by_source[source]]
        publication_dates = sorted(
            {link["publication_date"] for link in linked if link["publication_date"]}
        )
        rows.append(
            {
                "nct_id": canonical_nct_id,
                "source_nct_ids": source_nct_ids,
                "cohort_alias_nct_ids": cohort_aliases,
                "official_aliases": aliases,
                "identity_status": (
                    "OFFICIAL_ALIAS_RESOLVED" if cohort_aliases else "CANONICAL"
                ),
                "cohort_source": next(iter(cohort_sources)),
                "classifier_run_id": next(iter(classifier_run_ids)),
                "linked_publication_count": len({link["pmid"] for link in linked}),
                "earliest_linked_publication_date": (
                    publication_dates[0] if publication_dates else ""
                ),
            }
        )
    return rows
