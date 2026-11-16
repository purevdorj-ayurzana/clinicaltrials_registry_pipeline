from __future__ import annotations
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence
ENROLLMENT_REFERENCE = 619
COVARIATE_COLUMNS = ['nct_id', 'endpoint_label', 'endpoint_positive', 'first_submission_date', 'first_submission_year', 'submission_year_5y_centered', 'initial_full_version', 'initial_history_date', 'initial_raw_file_path', 'initial_raw_sha256', 'initial_phases_json', 'phase_group', 'initial_lead_sponsor_name', 'initial_lead_sponsor_class', 'industry_lead_sponsor', 'initial_enrollment_count', 'initial_enrollment_type', 'log2_initial_enrollment_centered_619', 'complete_case_eligible', 'exclusion_reason']
EXCLUSION_COLUMNS = ['nct_id', 'endpoint_label', 'exclusion_reason', 'initial_full_version', 'initial_raw_file_path', 'initial_phases_json', 'initial_lead_sponsor_name', 'initial_lead_sponsor_class', 'initial_enrollment_count', 'initial_enrollment_type']

def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return value

def classify_phase(value: str) -> str:
    if not value:
        return 'MISSING'
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all((isinstance(item, str) for item in parsed)):
        raise ValueError(f'Invalid phase representation: {value!r}')
    phases = frozenset(parsed)
    mapping = {frozenset({'EARLY_PHASE1'}): 'EARLY_PHASE', frozenset({'PHASE1'}): 'EARLY_PHASE', frozenset({'PHASE1', 'PHASE2'}): 'EARLY_PHASE', frozenset({'PHASE2'}): 'EARLY_PHASE', frozenset({'PHASE2', 'PHASE3'}): 'LATE_PHASE', frozenset({'PHASE3'}): 'LATE_PHASE', frozenset({'PHASE4'}): 'POST_MARKETING', frozenset({'NA'}): 'NOT_APPLICABLE'}
    if phases not in mapping:
        raise ValueError(f'Unmapped phase combination: {value!r}')
    return mapping[phases]

def _earliest_full_snapshots(path: Path, cohort_ids: set[str]) -> dict[str, dict[str, str]]:
    first: dict[str, dict[str, str]] = {}
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            nct_id = row.get('nct_id', '')
            version = int(row.get('version', '0'))
            if nct_id not in cohort_ids or version <= 0:
                continue
            if nct_id not in first or version < int(first[nct_id]['version']):
                first[nct_id] = {key: value or '' for key, value in row.items()}
    return first

def _read_sponsor(raw_path: Path) -> tuple[str, str]:
    raw = _read_json(raw_path)
    study = raw.get('study', raw)
    module = study.get('protocolSection', {}).get('sponsorCollaboratorsModule', {})
    lead = module.get('leadSponsor', {}) if isinstance(module, dict) else {}
    return (str(lead.get('name', '') or ''), str(lead.get('class', '') or ''))

def _build_covariates(source_dataset: Sequence[Mapping[str, str]], run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    cohort_ids = {row['nct_id'] for row in source_dataset}
    snapshots_path = run_dir / 'processed' / 'version_snapshots.csv'
    first = _earliest_full_snapshots(snapshots_path, cohort_ids)
    covariates: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for source in source_dataset:
        nct_id = source['nct_id']
        snapshot = first.get(nct_id)
        phase_group = 'MISSING'
        sponsor_name = sponsor_class = ''
        enrollment_count: int | None = None
        enrollment_type = ''
        raw_relative = raw_hash = phases = version = history_date = ''
        reason = ''
        if snapshot is None:
            reason = 'NO_EARLIEST_FULL_SNAPSHOT'
        else:
            version = snapshot['version']
            history_date = snapshot.get('history_date', '')
            raw_relative = snapshot.get('raw_file_path', '')
            raw_hash = snapshot.get('raw_sha256', '')
            phases = snapshot.get('phases', '')
            phase_group = classify_phase(phases)
            sponsor_name, sponsor_class = _read_sponsor(run_dir / raw_relative.replace(chr(92), "/"))
            enrollment_type = snapshot.get('enrollment_type', '')
            count_text = snapshot.get('enrollment_count', '')
            if count_text.isdigit() and int(count_text) > 0:
                enrollment_count = int(count_text)
            if enrollment_type != 'ESTIMATED':
                reason = 'INITIAL_ENROLLMENT_NOT_ESTIMATED'
            elif enrollment_count is None:
                reason = 'INITIAL_ESTIMATED_ENROLLMENT_MISSING_OR_NONPOSITIVE'
            elif not sponsor_class:
                reason = 'INITIAL_LEAD_SPONSOR_CLASS_MISSING'
        complete = reason == ''
        output: dict[str, Any] = {key: source.get(key, '') for key in source}
        output.update({'initial_full_version': int(version) if version else None, 'initial_history_date': history_date, 'initial_raw_file_path': raw_relative, 'initial_raw_sha256': raw_hash, 'initial_phases_json': phases, 'phase_group': phase_group, 'initial_lead_sponsor_name': sponsor_name, 'initial_lead_sponsor_class': sponsor_class, 'industry_lead_sponsor': sponsor_class == 'INDUSTRY' if sponsor_class else None, 'initial_enrollment_count': enrollment_count, 'initial_enrollment_type': enrollment_type, 'log2_initial_enrollment_centered_619': math.log2(enrollment_count / ENROLLMENT_REFERENCE) if complete else None, 'complete_case_eligible': complete, 'exclusion_reason': reason})
        covariates.append(output)
        if not complete:
            exclusions.append({column: output.get(column) for column in EXCLUSION_COLUMNS})
    covariates.sort(key=lambda row: row['nct_id'])
    exclusions.sort(key=lambda row: row['nct_id'])
    complete_rows = [row for row in covariates if row['complete_case_eligible']]
    counts = {'cohort_trials': len(covariates), 'complete_case_trials': len(complete_rows), 'excluded_trials': len(exclusions), 'complete_case_positive': sum((row['endpoint_label'] == 'POSITIVE' for row in complete_rows)), 'complete_case_negative': sum((row['endpoint_label'] == 'NEGATIVE' for row in complete_rows)), 'phase_distribution': dict(Counter((row['phase_group'] for row in complete_rows))), 'sponsor_distribution': {'INDUSTRY': sum((bool(row['industry_lead_sponsor']) for row in complete_rows)), 'NON_INDUSTRY': sum((not bool(row['industry_lead_sponsor']) for row in complete_rows))}, 'exclusion_reasons': dict(Counter((row['exclusion_reason'] for row in exclusions)))}
    expected_phase = {'EARLY_PHASE': 163, 'LATE_PHASE': 739, 'POST_MARKETING': 131, 'NOT_APPLICABLE': 359, 'MISSING': 8}
    if counts['cohort_trials'] != 1443 or counts['complete_case_trials'] != 1400 or counts['excluded_trials'] != 43:
        raise ValueError(f'Complete-case reconciliation failed: {counts}')
    if counts['complete_case_positive'] != 967 or counts['complete_case_negative'] != 433:
        raise ValueError(f'Complete-case endpoint reconciliation failed: {counts}')
    if counts['phase_distribution'] != expected_phase or counts['sponsor_distribution'] != {'INDUSTRY': 577, 'NON_INDUSTRY': 823}:
        raise ValueError(f'Covariate distribution reconciliation failed: {counts}')
    return (covariates, exclusions, counts)
