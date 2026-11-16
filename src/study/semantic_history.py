from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from study import semantic_helpers as condensed, semantic_candidates as semantic_candidates
from study.semantic_io import canonical_json, read_json, sha256_file, sha256_text

def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)

def _verified_version(source_run: Path, nct_id: str, snapshot: dict[str, str], extracted: dict[tuple[int, str], list[dict[str, str]]]) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    version = int(snapshot['version'])
    relative = Path(snapshot['raw_file_path'].replace('\\', '/'))
    raw_path = (source_run / relative).resolve()
    require(raw_path.is_relative_to(source_run) and raw_path.is_file(), f'Missing/invalid raw file: {nct_id} V{version}')
    require(sha256_file(raw_path) == snapshot['raw_sha256'], f'Raw hash mismatch: {nct_id} V{version}')
    payload = read_json(raw_path)
    require(payload.get('studyVersion') == version, f'Raw version mismatch: {nct_id} V{version}')
    section = payload.get('study', {}).get('protocolSection')
    require(isinstance(section, dict), f'Malformed protocol section: {nct_id} V{version}')
    require(section.get('identificationModule', {}).get('nctId') == nct_id, f'Raw NCT mismatch: {nct_id} V{version}')
    module = section.get('outcomesModule')
    require(module is None or isinstance(module, dict), f'Malformed outcomes module: {nct_id} V{version}')
    missing_primary_module = not isinstance(module, dict) or 'primaryOutcomes' not in module
    module = module or {}
    outcomes: dict[str, list[dict[str, Any]]] = {}
    for kind, key in (('PRIMARY', 'primaryOutcomes'), ('SECONDARY', 'secondaryOutcomes')):
        items = module.get(key, [])
        require(isinstance(items, list), f'Malformed {key}: {nct_id} V{version}')
        subset = sorted(extracted.pop((version, kind), []), key=lambda row: int(row['position']))
        require(len(subset) == len(items), f'Extracted {kind} count mismatch: {nct_id} V{version}')
        records: list[dict[str, Any]] = []
        for position, (item, row) in enumerate(zip(items, subset, strict=True), 1):
            fields, missing = condensed._raw_fields(item, f'{nct_id} V{version} {kind} {position}')
            require(int(row['position']) == position, f'Extracted position mismatch: {nct_id} V{version} {kind} {position}')
            require((row['measure'], row['description'], row['time_frame']) == (fields['title'], fields['description'], fields['timeframe']), f'Extracted text mismatch: {nct_id} V{version} {kind} {position}')
            require(json.loads(row['source_json']) == item, f'Extracted source JSON mismatch: {nct_id} V{version} {kind} {position}')
            item_hash = sha256_text(canonical_json({'outcome_type': kind, 'item': item}))
            require(row['item_hash'] == item_hash, f'Outcome hash mismatch: {nct_id} V{version} {kind} {position}')
            records.append({**fields, 'position': position, 'missing_fields': missing, 'source_item': item, 'item_hash': item_hash})
        outcomes[kind] = records
    provenance = {'version': version, 'raw_file_path': snapshot['raw_file_path'], 'raw_sha256': snapshot['raw_sha256'], 'missing_primary_outcomes_module': missing_primary_module, 'primary_outcomes': outcomes['PRIMARY']}
    return (provenance, outcomes)

def _model_records(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [semantic_candidates._model_record(record) for record in records]

def _build_trial(source_run: Path, nct_id: str, snapshots: list[dict[str, str]], extracted: dict[tuple[int, str], list[dict[str, str]]]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    ordered = sorted(snapshots, key=lambda row: int(row['version']))
    versions = [int(row['version']) for row in ordered]
    require(bool(versions) and len(versions) == len(set(versions)) and (versions == list(range(versions[0], versions[-1] + 1))), f'Missing/duplicate raw version: {nct_id}')
    source_ledger: list[dict[str, Any]] = []
    all_versions: list[dict[str, Any]] = []
    by_version: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for snapshot in ordered:
        provenance, outcomes = _verified_version(source_run, nct_id, snapshot, extracted)
        version = int(snapshot['version'])
        source_ledger.append(provenance)
        by_version[version] = outcomes
        all_versions.append({'version': version, 'outcomes': outcomes['PRIMARY']})
    require(not extracted, f'Unmatched extracted outcome rows: {nct_id} {list(extracted)[:3]}')
    states, collapsed_ledger = condensed.collapse_states(all_versions)
    for provenance, collapse in zip(source_ledger, collapsed_ledger, strict=True):
        provenance['retained'] = collapse['retained']
        provenance['state_start_version'] = collapse['state_start_version']
    candidates: list[dict[str, Any]] = []
    candidate_ledger: list[dict[str, Any]] = []
    candidate_boundaries = 0
    for previous, current in zip(states, states[1:]):
        to_version = int(current['version'])
        from_version = to_version - 1
        require(from_version in by_version, f'Missing immediate candidate boundary: {nct_id} V{from_version}-V{to_version}')
        old, new = (by_version[from_version], by_version[to_version])
        require(_model_records(old['PRIMARY']) == previous['outcomes'], f'Previous retained state mismatch: {nct_id} V{from_version}')
        require(_model_records(new['PRIMARY']) == current['outcomes'], f'Current retained state mismatch: {nct_id} V{to_version}')
        anchors, residual_old, residual_new = semantic_candidates._unique_primary_anchors(old['PRIMARY'], new['PRIMARY'])
        promotions, promotion_evaluations = semantic_candidates._select_candidates(target_primary=new['PRIMARY'], target_indexes=residual_new, same_priority=old['PRIMARY'], same_priority_indexes=residual_old, secondary=old['SECONDARY'], direction='secondary_to_primary', from_version=from_version, to_version=to_version, primary_state_version=to_version)
        demotions, demotion_evaluations = semantic_candidates._select_candidates(target_primary=old['PRIMARY'], target_indexes=residual_old, same_priority=new['PRIMARY'], same_priority_indexes=residual_new, secondary=new['SECONDARY'], direction='primary_to_secondary', from_version=from_version, to_version=to_version, primary_state_version=int(previous['version']))
        selected = [*promotions, *demotions]
        candidates.extend(selected)
        if selected:
            candidate_boundaries += 1
        candidate_ledger.append({'from_version': from_version, 'to_version': to_version, 'previous_retained_state_version': int(previous['version']), 'source': {'from_raw_file_path': source_ledger[versions.index(from_version)]['raw_file_path'], 'from_raw_sha256': source_ledger[versions.index(from_version)]['raw_sha256'], 'to_raw_file_path': source_ledger[versions.index(to_version)]['raw_file_path'], 'to_raw_sha256': source_ledger[versions.index(to_version)]['raw_sha256']}, 'old_primary': old['PRIMARY'], 'new_primary': new['PRIMARY'], 'old_secondary': old['SECONDARY'], 'new_secondary': new['SECONDARY'], 'unique_primary_anchors': anchors, 'residual_old_primary_indexes': [index + 1 for index in residual_old], 'residual_new_primary_indexes': [index + 1 for index in residual_new], 'promotion_evaluations': promotion_evaluations, 'demotion_evaluations': demotion_evaluations, 'selected_candidates': selected})
    trial_input = {'nct_id': nct_id, 'states': states, 'reclassification_candidates': candidates}
    metadata = {'raw_versions': len(versions), 'retained_states': len(states), 'candidate_pairs': len(candidates), 'candidate_boundaries': candidate_boundaries, 'missing_primary_module_versions': [row['version'] for row in source_ledger if row['missing_primary_outcomes_module']]}
    return (trial_input, source_ledger, candidate_ledger, metadata)
