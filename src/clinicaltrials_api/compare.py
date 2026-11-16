from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .download import _update_run_manifest, validate_run
from .util import (
    atomic_write_csv,
    canonical_json,
    parse_partial_date,
    read_csv,
    stable_text,
)


FIELD_CHANGE_COLUMNS = [
    "nct_id",
    "from_version",
    "to_version",
    "from_submission_date",
    "to_submission_date",
    "from_posting_date",
    "to_posting_date",
    "field",
    "field_path",
    "change_type",
    "transition_context",
    "old_state",
    "new_state",
    "old_value",
    "new_value",
    "after_study_start",
    "after_primary_completion",
    "after_completion",
    "after_earliest_publication",
]

NESTED_CHANGE_COLUMNS = [
    "nct_id",
    "from_version",
    "to_version",
    "from_posting_date",
    "to_posting_date",
    "domain",
    "item_key",
    "match_method",
    "field",
    "change_type",
    "old_position",
    "new_position",
    "old_value",
    "new_value",
    "after_study_start",
    "after_primary_completion",
    "after_completion",
    "after_earliest_publication",
]

REVIEW_FLAG_COLUMNS = [
    "nct_id",
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
]

MAIN_REVIEW_FLAG_CODES = frozenset(
    {
        "STUDY_TYPE_CHANGED",
        "ALLOCATION_CHANGED",
        "INTERVENTION_MODEL_CHANGED",
        "PRIMARY_PURPOSE_CHANGED",
        "MASKING_CHANGED",
        "NUMBER_OF_ARMS_CHANGED",
        "PLANNED_ENROLLMENT_CHANGED",
        "PRIMARY_OUTCOME_CHANGED",
        "ARMS_INTERVENTIONS_CHANGED",
        "FIRST_POST_AFTER_STUDY_START",
        "START_DATE_CROSSED_FIRST_SUBMISSION_BOUNDARY",
    }
)

CHANGE_SUBTYPE_CODES = frozenset(
    {
        "PRIMARY_OUTCOME_ADDED",
        "PRIMARY_OUTCOME_REMOVED",
        "OUTCOME_PROMOTED_TO_PRIMARY",
        "PRIMARY_OUTCOME_DEMOTED",
        "PRIMARY_OUTCOME_TIME_FRAME_CHANGED",
        "ARM_ADDED",
        "ARM_REMOVED",
        "ARM_TYPE_CHANGED",
        "INTERVENTION_ADDED",
        "INTERVENTION_REMOVED",
        "INTERVENTION_ARM_ASSIGNMENT_CHANGED",
    }
)

NORMALIZATION_EQUIVALENT_FIELDS = frozenset(
    {
        ("OUTCOME", "measure"),
        ("OUTCOME", "description"),
        ("OUTCOME", "time_frame"),
        ("ARM", "arm_label"),
        ("ARM", "description"),
        ("INTERVENTION", "intervention_name"),
        ("INTERVENTION", "description"),
    }
)


def _parse_json(value: str) -> Any:
    return json.loads(value) if value else None


def _bool_csv(value: bool | None) -> str:
    if value is None:
        return ""
    return "TRUE" if value else "FALSE"


def _first_date(values: list[tuple[str, str]], *, require_actual: bool = False) -> str:
    candidates = [
        value
        for value, date_type in values
        if value and (not require_actual or date_type == "ACTUAL")
    ]
    return min(candidates) if candidates else ""


def _timing_context(
    event_date: str,
    *,
    study_start: str,
    primary_completion: str,
    completion: str,
    earliest_publication: str,
) -> dict[str, str]:
    event = parse_partial_date(event_date)

    def after(anchor: str) -> str:
        parsed = parse_partial_date(anchor)
        if event is None or parsed is None:
            return ""
        return _bool_csv(event > parsed)

    return {
        "after_study_start": after(study_start),
        "after_primary_completion": after(primary_completion),
        "after_completion": after(completion),
        "after_earliest_publication": after(earliest_publication),
    }


def _change_type(old_state: str, new_state: str, old_value: Any, new_value: Any) -> str:
    if old_state == "ABSENT" and new_state != "ABSENT":
        return "ADDED"
    if old_state != "ABSENT" and new_state == "ABSENT":
        return "REMOVED"
    if old_state == "PRESENT_EMPTY" and new_state == "PRESENT_VALUE":
        return "ADDED"
    if old_state == "PRESENT_VALUE" and new_state == "PRESENT_EMPTY":
        return "REMOVED"
    return "MODIFIED"


def _unordered_equal(old_value: Any, new_value: Any) -> bool:
    if not isinstance(old_value, list) or not isinstance(new_value, list):
        return False
    return sorted(canonical_json(value) for value in old_value) == sorted(
        canonical_json(value) for value in new_value
    )


def _csv_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _normalized_text_set(value: Any) -> set[str]:
    return {stable_text(str(item)) for item in _csv_list(value) if stable_text(str(item))}


def _date_relation_to_first_submission(start_date: str, first_submission_date: str) -> str:
    """Classify only date relations that remain certain at the recorded precision."""
    start_earliest = parse_partial_date(start_date, boundary="start")
    start_latest = parse_partial_date(start_date, boundary="end")
    submission_earliest = parse_partial_date(first_submission_date, boundary="start")
    submission_latest = parse_partial_date(first_submission_date, boundary="end")
    if None in {start_earliest, start_latest, submission_earliest, submission_latest}:
        return "UNCERTAIN"
    if start_latest < submission_earliest:
        return "BEFORE_FIRST_SUBMISSION"
    if start_earliest >= submission_latest:
        return "ON_OR_AFTER_FIRST_SUBMISSION"
    return "UNCERTAIN"


def _transition_context(
    field: str,
    old_values: dict[str, Any],
    new_values: dict[str, Any],
) -> str:
    companion = {
        "enrollment_count": "enrollment_type",
        "start_date": "start_date_type",
        "primary_completion_date": "primary_completion_date_type",
        "completion_date": "completion_date_type",
    }.get(field)
    if field.endswith("_type"):
        old_type = old_values.get(field)
        new_type = new_values.get(field)
    elif companion:
        old_type = old_values.get(companion)
        new_type = new_values.get(companion)
    else:
        return ""
    if old_type or new_type:
        return f"{old_type or 'MISSING'}_TO_{new_type or 'MISSING'}"
    return "TYPE_UNAVAILABLE"


def _group_rows(rows: list[dict[str, str]], keys: tuple[str, ...]) -> dict[tuple[str, ...], list[dict[str, str]]]:
    grouped: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row[key] for key in keys)].append(row)
    return grouped


def is_primary_outcome_change(change: dict[str, Any]) -> bool:
    """Return whether a nested event concerns a primary outcome."""
    return change.get("domain") == "OUTCOME" and change.get(
        "change_type"
    ) != "REORDERED" and (
        str(change.get("item_key", "")).startswith("PRIMARY|")
        or (
            change.get("field") == "outcome_type"
            and "PRIMARY" in {change.get("old_value"), change.get("new_value")}
        )
        or '"outcome_type":"PRIMARY"' in str(change.get("old_value", ""))
        or '"outcome_type":"PRIMARY"' in str(change.get("new_value", ""))
    )


def _is_normalization_equivalent_change(change: dict[str, Any]) -> bool:
    """Return whether exact text differs only by case or whitespace normalization."""
    if change.get("change_type") != "MODIFIED":
        return False
    if (
        change.get("domain"),
        change.get("field"),
    ) not in NORMALIZATION_EQUIVALENT_FIELDS:
        return False
    if "old_value" not in change or "new_value" not in change:
        return False
    return stable_text(str(change["old_value"])) == stable_text(
        str(change["new_value"])
    )


def is_primary_outcome_flaggable_change(change: dict[str, Any]) -> bool:
    """Return whether a primary-outcome event is eligible for its parent flag."""
    return (
        is_primary_outcome_change(change)
        and change.get("field") != "description"
        and not _is_normalization_equivalent_change(change)
    )


def _primary_outcome_subtype(change: dict[str, Any]) -> str:
    if not is_primary_outcome_flaggable_change(change):
        return ""
    if change.get("change_type") == "ADDED" and change.get("field") == "item":
        return "PRIMARY_OUTCOME_ADDED"
    if change.get("change_type") == "REMOVED" and change.get("field") == "item":
        return "PRIMARY_OUTCOME_REMOVED"
    if change.get("field") == "outcome_type" and change.get("change_type") == "RECLASSIFIED":
        if change.get("new_value") == "PRIMARY":
            return "OUTCOME_PROMOTED_TO_PRIMARY"
        if change.get("old_value") == "PRIMARY":
            return "PRIMARY_OUTCOME_DEMOTED"
    if change.get("field") == "time_frame" and change.get("change_type") == "MODIFIED":
        return "PRIMARY_OUTCOME_TIME_FRAME_CHANGED"
    return ""


def is_arms_interventions_change(change: dict[str, Any]) -> bool:
    return change.get("domain") in {"ARM", "INTERVENTION"} and change.get(
        "change_type"
    ) != "REORDERED"


def is_arms_interventions_flaggable_change(change: dict[str, Any]) -> bool:
    """Return whether an arm/intervention event is eligible for its parent flag."""
    return (
        is_arms_interventions_change(change)
        and change.get("field") != "description"
        and not _is_normalization_equivalent_change(change)
    )


def _arms_interventions_subtype(
    change: dict[str, Any],
    arm_groups: dict[tuple[str, ...], list[dict[str, str]]],
) -> str:
    if not is_arms_interventions_flaggable_change(change):
        return ""
    if change.get("domain") == "ARM" and change.get("field") == "item":
        if change.get("change_type") == "ADDED":
            return "ARM_ADDED"
        if change.get("change_type") == "REMOVED":
            return "ARM_REMOVED"
    if change.get("domain") == "INTERVENTION" and change.get("field") == "item":
        if change.get("change_type") == "ADDED":
            return "INTERVENTION_ADDED"
        if change.get("change_type") == "REMOVED":
            return "INTERVENTION_REMOVED"
    if (
        change.get("domain") == "ARM"
        and change.get("field") == "arm_type"
        and change.get("change_type") == "MODIFIED"
    ):
        return "ARM_TYPE_CHANGED"
    if not (
        change.get("domain") == "INTERVENTION"
        and change.get("field") == "arm_labels"
        and change.get("change_type") == "MODIFIED"
    ):
        return ""
    old_labels = _normalized_text_set(change.get("old_value", ""))
    new_labels = _normalized_text_set(change.get("new_value", ""))
    if not old_labels or not new_labels or old_labels == new_labels:
        return ""
    nct_id = str(change["nct_id"])
    from_version = str(change["from_version"])
    to_version = str(change["to_version"])
    old_arms = arm_groups.get((nct_id, from_version), [])
    new_arms = arm_groups.get((nct_id, to_version), [])
    stable_arm_labels = {
        stable_text(row.get("arm_label", "")) for row in old_arms
    } & {stable_text(row.get("arm_label", "")) for row in new_arms}
    if not (old_labels | new_labels) <= stable_arm_labels:
        return ""
    intervention_name = str(change.get("item_key", "")).split("|", 1)[-1]

    def arm_intervention_names(value: Any) -> set[str]:
        names: set[str] = set()
        for item in _csv_list(value):
            normalized = stable_text(str(item))
            if not normalized:
                continue
            names.add(normalized)
            if ":" in normalized:
                names.add(normalized.split(":", 1)[1].strip())
        return names

    def inverse_links_match(rows: list[dict[str, str]], labels: set[str]) -> bool:
        normalized: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            normalized[stable_text(row.get("arm_label", ""))].append(row)
        return all(
            len(normalized.get(label, [])) == 1
            and intervention_name
            in arm_intervention_names(normalized[label][0].get("intervention_names", ""))
            for label in labels
        )

    if not inverse_links_match(old_arms, old_labels) or not inverse_links_match(
        new_arms, new_labels
    ):
        return ""
    return "INTERVENTION_ARM_ASSIGNMENT_CHANGED"


def _nested_event(
    *,
    nct_id: str,
    from_version: int,
    to_version: int,
    snapshots: dict[tuple[str, int], dict[str, str]],
    timing: dict[str, str],
    domain: str,
    item_key: str,
    match_method: str,
    field: str,
    change_type: str,
    old_position: str = "",
    new_position: str = "",
    old_value: Any = "",
    new_value: Any = "",
) -> dict[str, Any]:
    old_snapshot = snapshots[(nct_id, from_version)]
    new_snapshot = snapshots[(nct_id, to_version)]
    return {
        "nct_id": nct_id,
        "from_version": from_version,
        "to_version": to_version,
        "from_posting_date": old_snapshot["posting_date"],
        "to_posting_date": new_snapshot["posting_date"],
        "domain": domain,
        "item_key": item_key,
        "match_method": match_method,
        "field": field,
        "change_type": change_type,
        "old_position": old_position,
        "new_position": new_position,
        "old_value": old_value,
        "new_value": new_value,
        **timing,
    }


def _compare_keyed_items(
    *,
    nct_id: str,
    from_version: int,
    to_version: int,
    old_rows: list[dict[str, str]],
    new_rows: list[dict[str, str]],
    snapshots: dict[tuple[str, int], dict[str, str]],
    timing: dict[str, str],
    domain: str,
    key_fn: Callable[[dict[str, str]], str],
    compare_fields: list[str],
    reclassify_key_fn: Callable[[dict[str, str]], str] | None = None,
    unordered_fields: set[str] | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    old_by_hash = _group_rows(old_rows, ("item_hash",))
    new_by_hash = _group_rows(new_rows, ("item_hash",))
    consumed_old: set[int] = set()
    consumed_new: set[int] = set()
    for item_hash in sorted(set(old_by_hash) & set(new_by_hash)):
        old_items = sorted(old_by_hash[item_hash], key=lambda row: int(row.get("position") or 0))
        new_items = sorted(new_by_hash[item_hash], key=lambda row: int(row.get("position") or 0))
        for old, new in zip(old_items, new_items):
            consumed_old.add(id(old))
            consumed_new.add(id(new))
            if old.get("position") != new.get("position"):
                events.append(
                    _nested_event(
                        nct_id=nct_id,
                        from_version=from_version,
                        to_version=to_version,
                        snapshots=snapshots,
                        timing=timing,
                        domain=domain,
                        item_key=key_fn(old),
                        match_method="EXACT_ITEM",
                        field="position",
                        change_type="REORDERED",
                        old_position=old.get("position", ""),
                        new_position=new.get("position", ""),
                        old_value=old.get("position", ""),
                        new_value=new.get("position", ""),
                    )
                )

    remaining_old = [row for row in old_rows if id(row) not in consumed_old]
    remaining_new = [row for row in new_rows if id(row) not in consumed_new]
    for row in remaining_old:
        row["__identity"] = key_fn(row)
    for row in remaining_new:
        row["__identity"] = key_fn(row)
    old_keyed = _group_rows(remaining_old, ("__identity",))
    new_keyed = _group_rows(remaining_new, ("__identity",))
    for key in sorted(set(old_keyed) & set(new_keyed)):
        if not key[0] or len(old_keyed[key]) != 1 or len(new_keyed[key]) != 1:
            continue
        old = old_keyed[key][0]
        new = new_keyed[key][0]
        consumed_old.add(id(old))
        consumed_new.add(id(new))
        for field in compare_fields:
            if old.get(field, "") != new.get(field, ""):
                change_type = "MODIFIED"
                if field in (unordered_fields or set()) and _normalized_text_set(
                    old.get(field, "")
                ) == _normalized_text_set(new.get(field, "")):
                    change_type = "REORDERED"
                events.append(
                    _nested_event(
                        nct_id=nct_id,
                        from_version=from_version,
                        to_version=to_version,
                        snapshots=snapshots,
                        timing=timing,
                        domain=domain,
                        item_key=key[0],
                        match_method="STABLE_KEY",
                        field=field,
                        change_type=change_type,
                        old_position=old.get("position", ""),
                        new_position=new.get("position", ""),
                        old_value=old.get(field, ""),
                        new_value=new.get(field, ""),
                    )
                )
        if old.get("position") != new.get("position"):
            events.append(
                _nested_event(
                    nct_id=nct_id,
                    from_version=from_version,
                    to_version=to_version,
                    snapshots=snapshots,
                    timing=timing,
                    domain=domain,
                    item_key=key[0],
                    match_method="STABLE_KEY",
                    field="position",
                    change_type="REORDERED",
                    old_position=old.get("position", ""),
                    new_position=new.get("position", ""),
                    old_value=old.get("position", ""),
                    new_value=new.get("position", ""),
                )
            )

    if reclassify_key_fn is not None:
        remaining_old = [row for row in old_rows if id(row) not in consumed_old]
        remaining_new = [row for row in new_rows if id(row) not in consumed_new]
        for row in remaining_old:
            row["__reclassify"] = reclassify_key_fn(row)
        for row in remaining_new:
            row["__reclassify"] = reclassify_key_fn(row)
        old_reclassified = _group_rows(remaining_old, ("__reclassify",))
        new_reclassified = _group_rows(remaining_new, ("__reclassify",))
        for key in sorted(set(old_reclassified) & set(new_reclassified)):
            if not key[0] or len(old_reclassified[key]) != 1 or len(new_reclassified[key]) != 1:
                continue
            old = old_reclassified[key][0]
            new = new_reclassified[key][0]
            if old.get("outcome_type") == new.get("outcome_type"):
                continue
            consumed_old.add(id(old))
            consumed_new.add(id(new))
            events.append(
                _nested_event(
                    nct_id=nct_id,
                    from_version=from_version,
                    to_version=to_version,
                    snapshots=snapshots,
                    timing=timing,
                    domain=domain,
                    item_key=key[0],
                    match_method="EXACT_CONTENT",
                    field="outcome_type",
                    change_type="RECLASSIFIED",
                    old_position=old.get("position", ""),
                    new_position=new.get("position", ""),
                    old_value=old.get("outcome_type", ""),
                    new_value=new.get("outcome_type", ""),
                )
            )

    for old in old_rows:
        if id(old) not in consumed_old:
            events.append(
                _nested_event(
                    nct_id=nct_id,
                    from_version=from_version,
                    to_version=to_version,
                    snapshots=snapshots,
                    timing=timing,
                    domain=domain,
                    item_key=key_fn(old),
                    match_method="UNMATCHED",
                    field="item",
                    change_type="REMOVED",
                    old_position=old.get("position", ""),
                    old_value=old.get("source_json", ""),
                )
            )
    for new in new_rows:
        if id(new) not in consumed_new:
            events.append(
                _nested_event(
                    nct_id=nct_id,
                    from_version=from_version,
                    to_version=to_version,
                    snapshots=snapshots,
                    timing=timing,
                    domain=domain,
                    item_key=key_fn(new),
                    match_method="UNMATCHED",
                    field="item",
                    change_type="ADDED",
                    new_position=new.get("position", ""),
                    new_value=new.get("source_json", ""),
                )
            )
    for row in old_rows + new_rows:
        row.pop("__identity", None)
        row.pop("__reclassify", None)
    return events


def compare_run(
    *, run_dir: str | Path, config: dict[str, Any], replace: bool = False
) -> dict[str, int]:
    run_dir, run_manifest = validate_run(run_dir, config)
    if run_manifest.get("stage_status", {}).get("extract") not in {
        "COMPLETED",
        "COMPLETED_WITH_ERRORS",
    }:
        raise ValueError("Extraction stage must complete before comparison.")
    processed = run_dir / "processed"
    reports = run_dir / "reports"
    outputs = {
        "fields": processed / "field_changes.csv",
        "nested": processed / "nested_changes.csv",
        "flags": reports / "review_flags.csv",
    }
    existing = [str(path) for path in outputs.values() if path.exists()]
    if existing and not replace:
        raise FileExistsError(
            "Comparison files already exist; use --replace deliberately: " + ", ".join(existing)
        )

    snapshot_rows = read_csv(processed / "version_snapshots.csv")
    snapshots = {(row["nct_id"], int(row["version"])): row for row in snapshot_rows}
    fields = read_csv(processed / "snapshot_fields.csv")
    fields_by_version: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in fields:
        fields_by_version[(row["nct_id"], int(row["version"]))][row["field"]] = {
            "path": row["field_path"],
            "state": row["value_state"],
            "value": _parse_json(row["value_json"]),
        }
    trials = {
        row["nct_id"]: row for row in read_csv(processed / "canonical_trials.csv")
    }
    versions_by_nct: dict[str, list[int]] = defaultdict(list)
    for nct_id, version in snapshots:
        versions_by_nct[nct_id].append(version)

    anchors: dict[str, dict[str, str]] = {}
    for nct_id, versions in versions_by_nct.items():
        rows = [snapshots[(nct_id, version)] for version in sorted(versions)]
        anchors[nct_id] = {
            "study_start": _first_date([(row["start_date"], row["start_date_type"]) for row in rows]),
            "primary_completion": _first_date(
                [(row["primary_completion_date"], row["primary_completion_date_type"]) for row in rows],
                require_actual=True,
            ),
            "completion": _first_date(
                [(row["completion_date"], row["completion_date_type"]) for row in rows],
                require_actual=True,
            ),
            "earliest_publication": trials[nct_id]["earliest_linked_publication_date"],
        }

    field_changes: list[dict[str, Any]] = []
    field_specs = {field["label"]: field for field in config["scalar_fields"]}
    for nct_id, versions in versions_by_nct.items():
        for from_version, to_version in zip(sorted(versions), sorted(versions)[1:]):
            old_snapshot = snapshots[(nct_id, from_version)]
            new_snapshot = snapshots[(nct_id, to_version)]
            timing = _timing_context(new_snapshot["posting_date"], **anchors[nct_id])
            old_fields = fields_by_version[(nct_id, from_version)]
            new_fields = fields_by_version[(nct_id, to_version)]
            old_values = {name: value["value"] for name, value in old_fields.items()}
            new_values = {name: value["value"] for name, value in new_fields.items()}
            for field_name in field_specs:
                old = old_fields[field_name]
                new = new_fields[field_name]
                if old["state"] == new["state"] and old["value"] == new["value"]:
                    continue
                change_type = _change_type(old["state"], new["state"], old["value"], new["value"])
                if field_specs[field_name].get("unordered") and _unordered_equal(old["value"], new["value"]):
                    change_type = "REORDERED"
                field_changes.append(
                    {
                        "nct_id": nct_id,
                        "from_version": from_version,
                        "to_version": to_version,
                        "from_submission_date": old_snapshot["submission_date"],
                        "to_submission_date": new_snapshot["submission_date"],
                        "from_posting_date": old_snapshot["posting_date"],
                        "to_posting_date": new_snapshot["posting_date"],
                        "field": field_name,
                        "field_path": old["path"],
                        "change_type": change_type,
                        "transition_context": _transition_context(field_name, old_values, new_values),
                        "old_state": old["state"],
                        "new_state": new["state"],
                        "old_value": old["value"],
                        "new_value": new["value"],
                        **timing,
                    }
                )

    outcomes = read_csv(processed / "outcomes.csv")
    arms = read_csv(processed / "arms.csv")
    interventions = read_csv(processed / "interventions.csv")
    eligibility = read_csv(processed / "eligibility_versions.csv")
    nested_changes: list[dict[str, Any]] = []
    for rows in (outcomes, arms, interventions):
        for row in rows:
            row["source_json"] = row.get("source_json", "")
    outcome_groups = _group_rows(outcomes, ("nct_id", "version"))
    arm_groups = _group_rows(arms, ("nct_id", "version"))
    intervention_groups = _group_rows(interventions, ("nct_id", "version"))
    eligibility_by_version = {(row["nct_id"], int(row["version"])): row for row in eligibility}

    for nct_id, versions in versions_by_nct.items():
        for from_version, to_version in zip(sorted(versions), sorted(versions)[1:]):
            timing = _timing_context(snapshots[(nct_id, to_version)]["posting_date"], **anchors[nct_id])
            pair = {"nct_id": nct_id, "from_version": from_version, "to_version": to_version, "snapshots": snapshots, "timing": timing}
            old_outcomes = outcome_groups.get((nct_id, str(from_version)), [])
            new_outcomes = outcome_groups.get((nct_id, str(to_version)), [])
            nested_changes.extend(
                _compare_keyed_items(
                    **pair,
                    old_rows=old_outcomes,
                    new_rows=new_outcomes,
                    domain="OUTCOME",
                    key_fn=lambda row: f"{row['outcome_type']}|{stable_text(row['measure'])}",
                    compare_fields=["outcome_type", "measure", "description", "time_frame"],
                    reclassify_key_fn=lambda row: "|".join(
                        [
                            stable_text(row["measure"]),
                            stable_text(row["description"]),
                            stable_text(row["time_frame"]),
                        ]
                    ),
                )
            )
            nested_changes.extend(
                _compare_keyed_items(
                    **pair,
                    old_rows=arm_groups.get((nct_id, str(from_version)), []),
                    new_rows=arm_groups.get((nct_id, str(to_version)), []),
                    domain="ARM",
                    key_fn=lambda row: stable_text(row["arm_label"]),
                    compare_fields=["arm_label", "arm_type", "description", "intervention_names"],
                    unordered_fields={"intervention_names"},
                )
            )
            nested_changes.extend(
                _compare_keyed_items(
                    **pair,
                    old_rows=intervention_groups.get((nct_id, str(from_version)), []),
                    new_rows=intervention_groups.get((nct_id, str(to_version)), []),
                    domain="INTERVENTION",
                    key_fn=lambda row: f"{row['intervention_type']}|{stable_text(row['intervention_name'])}",
                    compare_fields=[
                        "intervention_type",
                        "intervention_name",
                        "description",
                        "other_names",
                        "arm_labels",
                    ],
                    unordered_fields={"other_names", "arm_labels"},
                )
            )
            old_eligibility = eligibility_by_version.get((nct_id, from_version), {})
            new_eligibility = eligibility_by_version.get((nct_id, to_version), {})
            for field in (
                "eligibility_criteria",
                "minimum_age",
                "maximum_age",
                "sex",
                "healthy_volunteers",
            ):
                if old_eligibility.get(field, "") != new_eligibility.get(field, ""):
                    nested_changes.append(
                        _nested_event(
                            **pair,
                            domain="ELIGIBILITY",
                            item_key="eligibility",
                            match_method="FIELD",
                            field=field,
                            change_type="MODIFIED",
                            old_value=old_eligibility.get(field, ""),
                            new_value=new_eligibility.get(field, ""),
                        )
                    )

    flags: list[dict[str, Any]] = []

    def add_flag(
        change: dict[str, Any],
        code: str,
        rule: str,
        *,
        domain: str | None = None,
        subtype: str = "",
    ) -> None:
        if code not in MAIN_REVIEW_FLAG_CODES:
            raise ValueError(f"Unknown main review flag code: {code}")
        if subtype and subtype not in CHANGE_SUBTYPE_CODES:
            raise ValueError(f"Unknown review change subtype: {subtype}")
        flags.append(
            {
                "nct_id": change["nct_id"],
                "flag_code": code,
                "change_subtype_code": subtype,
                "from_version": change.get("from_version", ""),
                "to_version": change.get("to_version", ""),
                "event_posting_date": change.get("to_posting_date", ""),
                "field_or_domain": domain or change.get("field", ""),
                "old_value": change.get("old_value", ""),
                "new_value": change.get("new_value", ""),
                "rule": rule,
                "after_study_start": change.get("after_study_start", ""),
                "after_primary_completion": change.get("after_primary_completion", ""),
                "after_completion": change.get("after_completion", ""),
                "after_earliest_publication": change.get("after_earliest_publication", ""),
            }
        )

    direct_rules = {
        "study_type": ("STUDY_TYPE_CHANGED", "Study type changed between consecutive public versions."),
        "allocation": ("ALLOCATION_CHANGED", "Allocation field was added, removed, or modified."),
        "intervention_model": ("INTERVENTION_MODEL_CHANGED", "Intervention model changed."),
        "primary_purpose": ("PRIMARY_PURPOSE_CHANGED", "Primary purpose changed."),
        "masking": ("MASKING_CHANGED", "Masking changed."),
        "number_of_arms": ("NUMBER_OF_ARMS_CHANGED", "Number of arms changed."),
    }
    for change in field_changes:
        if change["field"] in direct_rules:
            code, rule = direct_rules[change["field"]]
            add_flag(change, code, rule)
        elif change["field"] == "enrollment_count" and change["transition_context"] == "ESTIMATED_TO_ESTIMATED":
            add_flag(
                change,
                "PLANNED_ENROLLMENT_CHANGED",
                "Enrollment count changed while both versions described it as ESTIMATED.",
            )

    for change in nested_changes:
        if is_primary_outcome_flaggable_change(change):
            add_flag(
                change,
                "PRIMARY_OUTCOME_CHANGED",
                "A primary-outcome item was added, removed, modified, or reclassified.",
                domain="OUTCOME",
                subtype=_primary_outcome_subtype(change),
            )

    for change in nested_changes:
        if not is_arms_interventions_flaggable_change(change):
            continue
        add_flag(
            change,
            "ARMS_INTERVENTIONS_CHANGED",
            "An arm or intervention item was added, removed, or modified.",
            domain="ARMS_INTERVENTIONS",
            subtype=_arms_interventions_subtype(change, arm_groups),
        )

    for change in field_changes:
        if change.get("field") != "start_date":
            continue
        old_snapshot = snapshots[(change["nct_id"], int(change["from_version"]))]
        new_snapshot = snapshots[(change["nct_id"], int(change["to_version"]))]
        old_submission = old_snapshot.get("study_first_submit_date", "")
        new_submission = new_snapshot.get("study_first_submit_date", "")
        if not old_submission or old_submission != new_submission:
            continue
        old_relation = _date_relation_to_first_submission(
            str(change.get("old_value") or ""), old_submission
        )
        new_relation = _date_relation_to_first_submission(
            str(change.get("new_value") or ""), new_submission
        )
        if old_relation == new_relation or "UNCERTAIN" in {old_relation, new_relation}:
            continue
        boundary_change = {
            **change,
            "old_value": {
                "study_start_date": change.get("old_value"),
                "study_first_submit_date": old_submission,
                "classification": old_relation,
            },
            "new_value": {
                "study_start_date": change.get("new_value"),
                "study_first_submit_date": new_submission,
                "classification": new_relation,
            },
        }
        add_flag(
            boundary_change,
            "START_DATE_CROSSED_FIRST_SUBMISSION_BOUNDARY",
            "The reported study-start date crossed from before to on/after first submission, or vice versa.",
            domain="start_date_vs_first_submission",
        )

    for nct_id, versions in versions_by_nct.items():
        ordered = sorted(versions)
        first_snapshot = snapshots[(nct_id, ordered[0])]
        first_post = first_snapshot["posting_date"]
        start = anchors[nct_id]["study_start"]
        if parse_partial_date(first_post) and parse_partial_date(start) and parse_partial_date(first_post) > parse_partial_date(start):
            timing = _timing_context(first_post, **anchors[nct_id])
            flags.append(
                {
                    "nct_id": nct_id,
                    "flag_code": "FIRST_POST_AFTER_STUDY_START",
                    "change_subtype_code": "",
                    "from_version": "",
                    "to_version": ordered[0],
                    "event_posting_date": first_post,
                    "field_or_domain": "registration_timing",
                    "old_value": start,
                    "new_value": first_post,
                    "rule": "The earliest public registry version was posted after the earliest reported study start date.",
                    **timing,
                }
            )

    atomic_write_csv(outputs["fields"], field_changes, FIELD_CHANGE_COLUMNS, overwrite=replace)
    atomic_write_csv(outputs["nested"], nested_changes, NESTED_CHANGE_COLUMNS, overwrite=replace)
    unexpected_codes = {row["flag_code"] for row in flags} - MAIN_REVIEW_FLAG_CODES
    unexpected_subtypes = {
        row["change_subtype_code"] for row in flags if row["change_subtype_code"]
    } - CHANGE_SUBTYPE_CODES
    if unexpected_codes or unexpected_subtypes:
        raise ValueError(
            f"Unexpected review taxonomy values: flags={sorted(unexpected_codes)}, "
            f"subtypes={sorted(unexpected_subtypes)}"
        )
    atomic_write_csv(outputs["flags"], flags, REVIEW_FLAG_COLUMNS, overwrite=replace)
    _update_run_manifest(run_dir, stage="compare", status="COMPLETED")
    return {"field_changes": len(field_changes), "nested_changes": len(nested_changes), "review_flags": len(flags)}
