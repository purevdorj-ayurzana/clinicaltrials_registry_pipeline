from __future__ import annotations
from collections import Counter, defaultdict
from datetime import date
from typing import Any
COMPARISONS = ({'comparison_code': 'FIRST_PUBLIC_POSTING_VS_EARLIEST_REPORTED_START', 'label': 'First public posting versus earliest reported study start', 'registration_anchor': 'FIRST_PUBLIC_POSTING', 'start_anchor': 'EARLIEST_REPORTED_START'}, {'comparison_code': 'FIRST_SUBMISSION_VS_FIRST_ACTUAL_START', 'label': 'First submission versus first chronologically reported ACTUAL study start', 'registration_anchor': 'FIRST_SUBMISSION', 'start_anchor': 'FIRST_ACTUAL_START'})
RELATIONS = ('REGISTERED_BEFORE_START', 'SAME_DAY', 'REGISTERED_AFTER_START', 'UNCERTAIN_PARTIAL_DATE', 'MISSING')
EXACT_LAG_CATEGORIES = ('AT_LEAST_5_YEARS_BEFORE_START', '2_TO_LT_5_YEARS_BEFORE_START', '1_TO_LT_2_YEARS_BEFORE_START', '6_TO_LT_12_MONTHS_BEFORE_START', '3_TO_LT_6_MONTHS_BEFORE_START', '29_TO_90_DAYS_BEFORE_START', '15_TO_28_DAYS_BEFORE_START', '1_TO_14_DAYS_BEFORE_START', 'SAME_DAY', '1_TO_14_DAYS_AFTER_START', '15_TO_28_DAYS_AFTER_START', '29_TO_90_DAYS_AFTER_START', '3_TO_LT_6_MONTHS_AFTER_START', '6_TO_LT_12_MONTHS_AFTER_START', '1_TO_LT_2_YEARS_AFTER_START', '2_TO_LT_5_YEARS_AFTER_START', 'AT_LEAST_5_YEARS_AFTER_START')

def _precision(value: str) -> str:
    value = (value or '').strip()
    if len(value) == 4 and value.isdigit():
        return 'YEAR'
    if len(value) == 7 and value[4] == '-' and value.replace('-', '').isdigit():
        return 'MONTH'
    if len(value) == 10 and value[4] == '-' and (value[7] == '-'):
        try:
            date.fromisoformat(value)
        except ValueError:
            return 'UNKNOWN'
        return 'DAY'
    return 'MISSING' if not value else 'UNKNOWN'

def _date_interval(value: str) -> tuple[date, date] | None:
    precision = _precision(value)
    try:
        if precision == 'YEAR':
            year = int(value)
            return (date(year, 1, 1), date(year, 12, 31))
        if precision == 'MONTH':
            year, month = (int(part) for part in value.split('-'))
            first = date(year, month, 1)
            if month == 12:
                return (first, date(year, 12, 31))
            return (first, date.fromordinal(date(year, month + 1, 1).toordinal() - 1))
        if precision == 'DAY':
            parsed = date.fromisoformat(value)
            return (parsed, parsed)
    except ValueError:
        return None
    return None

def _date_relation(registration_date: str, start_date: str) -> tuple[str, int | None]:
    registration = _date_interval(registration_date)
    start = _date_interval(start_date)
    if not registration_date or not start_date:
        return ('MISSING', None)
    if registration is None or start is None:
        return ('UNCERTAIN_PARTIAL_DATE', None)
    registration_earliest, registration_latest = registration
    start_earliest, start_latest = start
    exact_lag = None
    if _precision(registration_date) == 'DAY' and _precision(start_date) == 'DAY':
        exact_lag = (registration_earliest - start_earliest).days
    if registration_latest < start_earliest:
        return ('REGISTERED_BEFORE_START', exact_lag)
    if registration_earliest > start_latest:
        return ('REGISTERED_AFTER_START', exact_lag)
    if registration_earliest == registration_latest == start_earliest == start_latest:
        return ('SAME_DAY', exact_lag)
    return ('UNCERTAIN_PARTIAL_DATE', exact_lag)

def _exact_lag_category(lag_days: int | None) -> str:
    if lag_days is None:
        return ''
    if lag_days <= -1826:
        return 'AT_LEAST_5_YEARS_BEFORE_START'
    if lag_days <= -731:
        return '2_TO_LT_5_YEARS_BEFORE_START'
    if lag_days <= -366:
        return '1_TO_LT_2_YEARS_BEFORE_START'
    if lag_days <= -183:
        return '6_TO_LT_12_MONTHS_BEFORE_START'
    if lag_days <= -91:
        return '3_TO_LT_6_MONTHS_BEFORE_START'
    if lag_days <= -29:
        return '29_TO_90_DAYS_BEFORE_START'
    if lag_days <= -15:
        return '15_TO_28_DAYS_BEFORE_START'
    if lag_days <= -1:
        return '1_TO_14_DAYS_BEFORE_START'
    if lag_days == 0:
        return 'SAME_DAY'
    if lag_days <= 14:
        return '1_TO_14_DAYS_AFTER_START'
    if lag_days <= 28:
        return '15_TO_28_DAYS_AFTER_START'
    if lag_days <= 90:
        return '29_TO_90_DAYS_AFTER_START'
    if lag_days <= 182:
        return '3_TO_LT_6_MONTHS_AFTER_START'
    if lag_days <= 365:
        return '6_TO_LT_12_MONTHS_AFTER_START'
    if lag_days <= 730:
        return '1_TO_LT_2_YEARS_AFTER_START'
    if lag_days <= 1825:
        return '2_TO_LT_5_YEARS_AFTER_START'
    return 'AT_LEAST_5_YEARS_AFTER_START'

def _first_submission(versions: list[dict[str, str]]) -> tuple[str, bool]:
    values = {row['study_first_submit_date'] for row in versions if row['study_first_submit_date']}
    if not values:
        return ('', False)
    if len(values) == 1:
        return (next(iter(values)), False)
    return ('', True)

def _build_trial_rows(canonical_ids: set[str], snapshots: list[dict[str, str]]) -> list[dict[str, Any]]:
    by_trial: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in snapshots:
        by_trial[row['nct_id']].append(row)
    unexpected = sorted(set(by_trial) - canonical_ids)
    missing = sorted(canonical_ids - set(by_trial))
    if unexpected or missing:
        raise ValueError(f'Snapshot/canonical trial mismatch: unexpected={len(unexpected)}, missing={len(missing)}')
    rows: list[dict[str, Any]] = []
    for nct_id in sorted(canonical_ids):
        versions = sorted(by_trial[nct_id], key=lambda row: int(row['version']))
        first_post = versions[0]
        reported_starts = [row for row in versions if row['start_date']]
        earliest_reported = min(reported_starts, key=lambda row: row['start_date']) if reported_starts else None
        first_actual = next((row for row in versions if row['start_date'] and row['start_date_type'] == 'ACTUAL'), None)
        first_submission_date, first_submission_conflict = _first_submission(versions)
        anchors = {'FIRST_PUBLIC_POSTING': (first_post['posting_date'], first_post['version'], ''), 'FIRST_SUBMISSION': (first_submission_date, '', ''), 'EARLIEST_REPORTED_START': (earliest_reported['start_date'] if earliest_reported else '', earliest_reported['version'] if earliest_reported else '', earliest_reported['start_date_type'] if earliest_reported else ''), 'FIRST_ACTUAL_START': (first_actual['start_date'] if first_actual else '', first_actual['version'] if first_actual else '', first_actual['start_date_type'] if first_actual else '')}
        for comparison in COMPARISONS:
            registration_date, registration_version, _ = anchors[comparison['registration_anchor']]
            start_date, start_version, start_type = anchors[comparison['start_anchor']]
            relation, lag_days = _date_relation(registration_date, start_date)
            rows.append({'nct_id': nct_id, 'comparison_code': comparison['comparison_code'], 'comparison_label': comparison['label'], 'registration_anchor': comparison['registration_anchor'], 'registration_date': registration_date, 'registration_date_precision': _precision(registration_date), 'registration_anchor_version': registration_version, 'start_anchor': comparison['start_anchor'], 'start_date': start_date, 'start_date_precision': _precision(start_date), 'start_anchor_version': start_version, 'start_date_type': start_type, 'date_relation': relation, 'exact_lag_days': lag_days, 'exact_lag_category': _exact_lag_category(lag_days), 'first_submission_date_conflict': first_submission_conflict})
    return rows

def _summary_rows(trial_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for comparison in COMPARISONS:
        code = comparison['comparison_code']
        selected = [row for row in trial_rows if row['comparison_code'] == code]
        relation_counts = Counter((row['date_relation'] for row in selected))
        for order, relation in enumerate(RELATIONS, start=1):
            output.append({'comparison_code': code, 'comparison_label': comparison['label'], 'table': 'ALL_TRIALS_DATE_RELATION', 'category_order': order, 'category': relation, 'trial_count': relation_counts[relation], 'denominator': len(selected), 'percent_of_denominator': round(100 * relation_counts[relation] / len(selected), 4)})
        exact = [row for row in selected if row['exact_lag_days'] != '' and row['exact_lag_days'] is not None]
        exact_counts = Counter((row['exact_lag_category'] for row in exact))
        for order, category in enumerate(EXACT_LAG_CATEGORIES, start=1):
            output.append({'comparison_code': code, 'comparison_label': comparison['label'], 'table': 'EXACT_DAY_LAG_DISTRIBUTION', 'category_order': order, 'category': category, 'trial_count': exact_counts[category], 'denominator': len(exact), 'percent_of_denominator': round(100 * exact_counts[category] / len(exact), 4) if exact else ''})
    return output
