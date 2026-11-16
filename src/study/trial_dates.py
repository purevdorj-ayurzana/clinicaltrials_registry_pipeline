from __future__ import annotations
import csv
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable
IDENTITY_REQUIRED = {'source_nct_id', 'canonical_nct_id', 'retrieval_status'}
SUMMARY_REQUIRED = {'nct_id', 'validation_status', 'retrieval_status', 'error_count'}
SNAPSHOT_REQUIRED = {'nct_id', 'version', 'start_date', 'start_date_type', 'primary_completion_date', 'primary_completion_date_type'}

def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected a JSON object: {path}')
    return value

def _read_csv(path: Path, required: set[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f'CSV has no header: {path}')
        missing = sorted(required - set(reader.fieldnames))
        if missing:
            raise ValueError(f'Malformed schema in {path}; missing columns: {', '.join(missing)}')
        return [{key: value or '' for key, value in row.items()} for row in reader]

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

def _file_record(path: Path) -> dict[str, Any]:
    return {'path': str(path), 'sha256': _sha256(path), 'bytes': path.stat().st_size}

def _date_interval(value: str) -> tuple[date, date, str] | None:
    value = (value or '').strip().replace('/', '-')
    try:
        if re.fullmatch('\\d{4}', value):
            year = int(value)
            return (date(year, 1, 1), date(year, 12, 31), 'YEAR')
        if re.fullmatch('\\d{4}-\\d{2}', value):
            year, month = (int(part) for part in value.split('-'))
            start = date(year, month, 1)
            if month == 12:
                end = date(year, 12, 31)
            else:
                next_month = date(year, month + 1, 1)
                end = date.fromordinal(next_month.toordinal() - 1)
            return (start, end, 'MONTH')
        if re.fullmatch('\\d{4}-\\d{2}-\\d{2}', value):
            parsed = date.fromisoformat(value)
            return (parsed, parsed, 'DAY')
    except ValueError:
        return None
    return None

def _select_earliest_date(values: Iterable[str]) -> dict[str, Any] | None:
    candidates = []
    for value in sorted(set((item for item in values if item))):
        interval = _date_interval(value)
        if interval:
            candidates.append((interval[0], interval[1], value, interval[2]))
    if not candidates:
        return None
    earliest, latest, value, precision = min(candidates)
    return {'value': value, 'earliest': earliest, 'latest': latest, 'precision': precision}

def classify_event_window(event_date: str, start_anchor: dict[str, Any] | None, primary_completion_anchor: dict[str, Any] | None) -> str:
    event_interval = _date_interval(event_date)
    if event_interval is None or event_interval[2] != 'DAY':
        return 'INDETERMINATE_EVENT_DATE'
    event = event_interval[0]
    if start_anchor is None:
        return 'INDETERMINATE_PARTIAL_DATE_BOUNDARY'
    if event <= start_anchor['earliest']:
        return 'PRE_OR_ON_START'
    if primary_completion_anchor is None:
        return 'INDETERMINATE_PRIMARY_COMPLETION_MISSING'
    if primary_completion_anchor['latest'] < start_anchor['earliest']:
        return 'INDETERMINATE_ANCHOR_ORDER'
    if event > primary_completion_anchor['latest']:
        return 'POST_PRIMARY_COMPLETION'
    if event > start_anchor['latest'] and event <= primary_completion_anchor['earliest']:
        return 'POST_START_TO_PRIMARY_COMPLETION'
    return 'INDETERMINATE_PARTIAL_DATE_BOUNDARY'

def _log_choose(n: int, k: int) -> float:
    if k < 0 or k > n:
        return -math.inf
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)

def _fisher_support(a: int, b: int, c: int, d: int) -> tuple[list[int], list[float]]:
    row1 = a + b
    row2 = c + d
    col1 = a + c
    low = max(0, col1 - row2)
    high = min(row1, col1)
    support = list(range(low, high + 1))
    base_logs = [_log_choose(row1, x) + _log_choose(row2, col1 - x) for x in support]
    return (support, base_logs)

def _normalized_noncentral_probabilities(support: list[int], base_logs: list[float], log_odds: float) -> list[float]:
    logs = [base + x * log_odds for x, base in zip(support, base_logs)]
    maximum = max(logs)
    weights = [math.exp(value - maximum) for value in logs]
    total = sum(weights)
    return [weight / total for weight in weights]

def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    support, base_logs = _fisher_support(a, b, c, d)
    probs = _normalized_noncentral_probabilities(support, base_logs, 0.0)
    observed = probs[support.index(a)]
    return min(1.0, sum((prob for prob in probs if prob <= observed * (1 + 1e-12))))

def _bisect_monotone(function: Callable[[float], float], target: float, low: float, high: float, *, increasing: bool) -> float:
    for _ in range(180):
        middle = (low + high) / 2
        value = function(middle)
        if (value < target) == increasing:
            low = middle
        else:
            high = middle
    return (low + high) / 2

def conditional_odds_ratio_exact_ci(a: int, b: int, c: int, d: int, alpha: float=0.05) -> tuple[float, float, float]:
    support, base_logs = _fisher_support(a, b, c, d)
    low_x, high_x = (support[0], support[-1])

    def probabilities(log_odds: float) -> list[float]:
        return _normalized_noncentral_probabilities(support, base_logs, log_odds)
    if a == low_x:
        estimate = 0.0
    elif a == high_x:
        estimate = math.inf
    else:

        def mean(log_odds: float) -> float:
            probs = probabilities(log_odds)
            return sum((x * probability for x, probability in zip(support, probs)))
        estimate = math.exp(_bisect_monotone(mean, float(a), -50.0, 50.0, increasing=True))
    if a == low_x:
        lower = 0.0
    else:

        def upper_tail(log_odds: float) -> float:
            probs = probabilities(log_odds)
            return sum((prob for x, prob in zip(support, probs) if x >= a))
        lower = math.exp(_bisect_monotone(upper_tail, alpha / 2, -50.0, 50.0, increasing=True))
    if a == high_x:
        upper = math.inf
    else:

        def lower_tail(log_odds: float) -> float:
            probs = probabilities(log_odds)
            return sum((prob for x, prob in zip(support, probs) if x <= a))
        upper = math.exp(_bisect_monotone(lower_tail, alpha / 2, -50.0, 50.0, increasing=False))
    return (estimate, lower, upper)

def _build_trial_cohort(mapping_rows: list[dict[str, str]], run_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    run_manifest_path = run_dir / 'run_manifest.json'
    identity_path = run_dir / 'raw' / 'clinicaltrials_gov' / 'identity_resolutions.csv'
    summary_path = run_dir / 'reports' / 'trial_summary.csv'
    snapshots_path = run_dir / 'processed' / 'version_snapshots.csv'
    flags_path = run_dir / 'reports' / 'review_flags.csv'
    run_manifest = _read_json(run_manifest_path)
    if run_manifest.get('run_format_version') != 2 or run_manifest.get('run_id') != run_dir.name:
        raise ValueError('Unexpected ClinicalTrials source-run identity')
    expected_identity = run_manifest.get('identity_resolution', {}).get('sha256')
    if not expected_identity or _sha256(identity_path) != expected_identity:
        raise ValueError('ClinicalTrials identity-resolution hash mismatch')
    mapping_run_id = run_manifest.get('run_id')
    identities = _read_csv(identity_path, IDENTITY_REQUIRED)
    identity_by_source = {row['source_nct_id']: row for row in identities}
    if len(identity_by_source) != len(identities):
        raise ValueError('Duplicate source identifiers in identity resolutions')
    trials: dict[str, dict[str, Any]] = {}
    for mapping in mapping_rows:
        source = mapping['nct_id']
        if source not in identity_by_source:
            raise ValueError(f'Missing identity resolution for {source}')
        identity = identity_by_source[source]
        canonical = identity['canonical_nct_id']
        if identity['retrieval_status'] != 'COMPLETED':
            raise ValueError(f'Incomplete registry retrieval for {source}')
        if canonical in trials:
            raise ValueError(f'Mapping cohort collapses to duplicate canonical trial {canonical}')
        trials[canonical] = {'nct_id': canonical, 'source_nct_id': source, 'endpoint_label': mapping['endpoint_label'], 'endpoint_positive': mapping['endpoint_label'] == 'POSITIVE'}
    summaries = {row['nct_id']: row for row in _read_csv(summary_path, SUMMARY_REQUIRED)}
    for nct_id in trials:
        row = summaries.get(nct_id)
        if not row or row['validation_status'] != 'FOUND' or row['retrieval_status'] != 'COMPLETED' or (row['error_count'] != '0'):
            raise ValueError(f'Registry history is not fully evaluable for {nct_id}')
    snapshots = _read_csv(snapshots_path, SNAPSHOT_REQUIRED)
    snapshots_by_trial: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in snapshots:
        if row['nct_id'] in trials:
            snapshots_by_trial[row['nct_id']].append(row)
    for nct_id, trial in trials.items():
        rows = snapshots_by_trial.get(nct_id, [])
        if not rows:
            raise ValueError(f'No version snapshots for {nct_id}')
        start = _select_earliest_date((row['start_date'] for row in rows))
        primary = _select_earliest_date((row['primary_completion_date'] for row in rows if row['primary_completion_date_type'] == 'ACTUAL'))
        trial['study_start_anchor'] = start
        trial['primary_completion_anchor'] = primary
        trial['anchor_missing_primary_completion'] = primary is None
        trial['anchor_order_indeterminate'] = bool(start and primary and (primary['latest'] < start['earliest']))
        trial['anchor_evaluable'] = bool(start and primary and (not trial['anchor_order_indeterminate']))
    provenance = {'clinicaltrials_run_id': mapping_run_id, 'canonical_trial_count': len(trials), 'complete_history_trial_count': len(trials), 'study_start_available_count': sum((trial['study_start_anchor'] is not None for trial in trials.values())), 'actual_primary_completion_available_count': sum((trial['primary_completion_anchor'] is not None for trial in trials.values())), 'anchor_evaluable_count': sum((trial['anchor_evaluable'] for trial in trials.values())), 'anchor_missing_primary_completion_count': sum((trial['anchor_missing_primary_completion'] for trial in trials.values())), 'anchor_order_indeterminate_count': sum((trial['anchor_order_indeterminate'] for trial in trials.values()))}
    source_files = {str(path): _file_record(path) for path in (run_manifest_path, identity_path, summary_path, snapshots_path, flags_path)}
    return (trials, provenance, source_files)
