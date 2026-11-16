from __future__ import annotations
from collections import Counter
from datetime import date
from typing import Any, Mapping
BOUNDARY_FLAG = 'START_DATE_CROSSED_FIRST_SUBMISSION_BOUNDARY'
PUBLIC_POST_COMPARISON = 'FIRST_PUBLIC_POSTING_VS_EARLIEST_REPORTED_START'
MAGNITUDE_BINS = (('SAME_DAY', 0, 0, 'Same day'), ('1_TO_14_DAYS', 1, 14, '1-14 days'), ('15_TO_28_DAYS', 15, 28, '15-28 days'), ('29_TO_90_DAYS', 29, 90, '29-90 days'), ('3_TO_LT_6_MONTHS', 91, 182, '3 to <6 months'), ('6_TO_LT_12_MONTHS', 183, 365, '6 to <12 months'), ('1_TO_LT_2_YEARS', 366, 730, '1 to <2 years'), ('2_TO_LT_5_YEARS', 731, 1825, '2 to <5 years'), ('AT_LEAST_5_YEARS', 1826, None, 'At least 5 years'))

def _precision(value: str) -> str:
    value = (value or '').strip()
    if len(value) == 4 and value.isdigit():
        try:
            date(int(value), 1, 1)
        except ValueError:
            return 'UNKNOWN'
        return 'YEAR'
    if len(value) == 7 and value[4] == '-' and value.replace('-', '').isdigit():
        try:
            date(int(value[:4]), int(value[5:]), 1)
        except ValueError:
            return 'UNKNOWN'
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
            year, month = (int(value[:4]), int(value[5:]))
            first = date(year, month, 1)
            next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            return (first, date.fromordinal(next_month.toordinal() - 1))
        if precision == 'DAY':
            parsed = date.fromisoformat(value)
            return (parsed, parsed)
    except ValueError:
        return None
    return None

def _magnitude_bin(lag_days: int | None) -> str:
    if lag_days is None:
        return ''
    magnitude = abs(lag_days)
    for code, lower, upper, _ in MAGNITUDE_BINS:
        if magnitude >= lower and (upper is None or magnitude <= upper):
            return code
    raise AssertionError(f'No magnitude bin for {lag_days}')

def describe_relative_date(reported_date: str, reference_day: str) -> dict[str, Any]:
    interval = _date_interval(reported_date)
    reference = _date_interval(reference_day)
    precision = _precision(reported_date)
    if interval is None or reference is None or _precision(reference_day) != 'DAY':
        return {'reported_date': reported_date, 'precision': precision, 'possible_first_date': '', 'possible_last_date': '', 'possible_lag_min_days': None, 'possible_lag_max_days': None, 'exact_lag_days': None, 'exact_lag_bin': '', 'relation': 'UNCERTAIN'}
    earliest, latest = interval
    reference_date = reference[0]
    min_lag = (earliest - reference_date).days
    max_lag = (latest - reference_date).days
    if latest < reference_date:
        relation = 'BEFORE_FIRST_SUBMISSION'
    elif earliest >= reference_date:
        relation = 'ON_OR_AFTER_FIRST_SUBMISSION'
    else:
        relation = 'UNCERTAIN'
    exact = min_lag if precision == 'DAY' else None
    return {'reported_date': reported_date, 'precision': precision, 'possible_first_date': earliest.isoformat(), 'possible_last_date': latest.isoformat(), 'possible_lag_min_days': min_lag, 'possible_lag_max_days': max_lag, 'exact_lag_days': exact, 'exact_lag_bin': _magnitude_bin(exact), 'relation': relation}

def _boundary_direction(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    pair = (old['classification'], new['classification'])
    directions = {('BEFORE_FIRST_SUBMISSION', 'ON_OR_AFTER_FIRST_SUBMISSION'): 'RETROSPECTIVE_TO_PROSPECTIVE', ('ON_OR_AFTER_FIRST_SUBMISSION', 'BEFORE_FIRST_SUBMISSION'): 'PROSPECTIVE_TO_RETROSPECTIVE'}
    try:
        return directions[pair]
    except KeyError as exc:
        raise ValueError(f'Unexpected boundary transition {pair!r}') from exc

def _first_post_detail(lag_rows: list[dict[str, str]], first_post_flag_ids: set[str]) -> list[dict[str, Any]]:
    selected = [row for row in lag_rows if row['comparison_code'] == PUBLIC_POST_COMPARISON]
    if len(selected) != len({row['nct_id'] for row in selected}):
        raise ValueError('Lag evidence has duplicate public-posting trial rows')
    by_nct = {row['nct_id']: row for row in selected}
    if first_post_flag_ids - set(by_nct):
        raise ValueError('First-post flag ledger contains trials absent from lag evidence')
    details: list[dict[str, Any]] = []
    for nct_id in sorted(first_post_flag_ids):
        source = by_nct[nct_id]
        start = _date_interval(source['start_date'])
        posting = _date_interval(source['registration_date'])
        if start is None or posting is None or _precision(source['registration_date']) != 'DAY':
            raise ValueError(f'Unusable public-posting evidence for {nct_id}')
        posting_day = posting[0]
        start_earliest, start_latest = start
        exact_lag = int(source['exact_lag_days']) if source['exact_lag_days'] else None
        if exact_lag is not None and source['start_date_precision'] != 'DAY':
            raise ValueError(f'Partial start date incorrectly has exact lag for {nct_id}')
        possible_min = (posting_day - start_latest).days
        possible_max = (posting_day - start_earliest).days
        if exact_lag is not None:
            interpretation = 'EXACT_DAY'
        elif source['date_relation'] == 'REGISTERED_AFTER_START':
            interpretation = 'MONTH_PRECISION_INTERVAL_CERTAIN_AFTER'
        else:
            interpretation = 'MONTH_PRECISION_ACTUAL_RELATION_INDETERMINATE'
        details.append({'nct_id': nct_id, 'first_public_posting_date': source['registration_date'], 'earliest_reported_start_date': source['start_date'], 'earliest_reported_start_precision': source['start_date_precision'], 'existing_flag_triggered': True, 'conservative_date_relation': source['date_relation'], 'start_possible_first_date': start_earliest.isoformat(), 'start_possible_last_date': start_latest.isoformat(), 'possible_lag_min_days': possible_min, 'possible_lag_max_days': possible_max, 'exact_lag_days': exact_lag, 'exact_lag_bin': _magnitude_bin(exact_lag), 'lag_interpretation': interpretation})
    return details

def _summary_row(*, analysis: str, section: str, direction: str='', start_state: str='', category_order: int, category: str, count: int, denominator: int, unit: str, notes: str='') -> dict[str, Any]:
    return {'analysis': analysis, 'section': section, 'direction': direction, 'start_state': start_state, 'category_order': category_order, 'category': category, 'count': count, 'denominator': denominator, 'percent_of_denominator': round(100 * count / denominator, 4) if denominator else '', 'unit': unit, 'notes': notes}

def _first_post_summary(details: list[dict[str, Any]], all_lag_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    total = len(details)
    by_interpretation = Counter((row['lag_interpretation'] for row in details))
    summary = [_summary_row(analysis='FIRST_POST_AFTER_STUDY_START', section='FLAGGED_TRIAL_STATUS', category_order=1, category='ALL_EXISTING_FLAGGED_TRIALS', count=total, denominator=total, unit='TRIAL', notes='Existing flag uses first public posting versus earliest reported study start.')]
    for order, category in enumerate(('EXACT_DAY', 'MONTH_PRECISION_INTERVAL_CERTAIN_AFTER', 'MONTH_PRECISION_ACTUAL_RELATION_INDETERMINATE'), start=2):
        summary.append(_summary_row(analysis='FIRST_POST_AFTER_STUDY_START', section='FLAGGED_TRIAL_STATUS', category_order=order, category=category, count=by_interpretation[category], denominator=total, unit='TRIAL', notes='Partial dates retain possible day ranges; no arbitrary day is assigned.'))
    exact = [row for row in details if row['exact_lag_days'] is not None]
    counts = Counter((row['exact_lag_bin'] for row in exact))
    for order, (code, _, _, label) in enumerate(MAGNITUDE_BINS[1:], start=1):
        summary.append(_summary_row(analysis='FIRST_POST_AFTER_STUDY_START', section='EXACT_POSITIVE_DAY_LAG', category_order=order, category=code, count=counts[code], denominator=len(exact), unit='TRIAL', notes=label))
    all_public = [row for row in all_lag_rows if row['comparison_code'] == PUBLIC_POST_COMPARISON]
    non_flagged_uncertain = sum((row['date_relation'] == 'UNCERTAIN_PARTIAL_DATE' and row['nct_id'] not in {item['nct_id'] for item in details} for row in all_public))
    summary.append(_summary_row(analysis='FIRST_POST_AFTER_STUDY_START', section='CONTEXT', category_order=1, category='UNFLAGGED_MONTH_PRECISION_INDETERMINATE', count=non_flagged_uncertain, denominator=len(all_public), unit='TRIAL', notes='Context only: interval-uncertain public-posting comparisons not triggered by the existing earliest-boundary rule.'))
    return summary

def _boundary_summary(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total_events = len(details)
    total_trials = len({row['nct_id'] for row in details})
    summary = [_summary_row(analysis=BOUNDARY_FLAG, section='EVENT_AND_TRIAL_COUNTS', category_order=1, category='ALL_BOUNDARY_EVENTS', count=total_events, denominator=total_events, unit='EVENT', notes='Every historical boundary-crossing event is retained.'), _summary_row(analysis=BOUNDARY_FLAG, section='EVENT_AND_TRIAL_COUNTS', category_order=2, category='DISTINCT_TRIALS_WITH_ANY_BOUNDARY_EVENT', count=total_trials, denominator=total_trials, unit='TRIAL', notes='Trials can contribute more than one historical event.')]
    directions = ('RETROSPECTIVE_TO_PROSPECTIVE', 'PROSPECTIVE_TO_RETROSPECTIVE')
    for order, direction in enumerate(directions, start=3):
        selected = [row for row in details if row['direction'] == direction]
        summary.append(_summary_row(analysis=BOUNDARY_FLAG, section='EVENT_AND_TRIAL_COUNTS', direction=direction, category_order=order, category='EVENTS', count=len(selected), denominator=total_events, unit='EVENT', notes='Direction-specific event count.'))
        summary.append(_summary_row(analysis=BOUNDARY_FLAG, section='EVENT_AND_TRIAL_COUNTS', direction=direction, category_order=order, category='DISTINCT_TRIALS', count=len({row['nct_id'] for row in selected}), denominator=total_trials, unit='TRIAL', notes='Direction-specific trial counts can overlap.'))
    both_directions = {nct_id for nct_id in {row['nct_id'] for row in details} if len({row['direction'] for row in details if row['nct_id'] == nct_id}) == 2}
    summary.append(_summary_row(analysis=BOUNDARY_FLAG, section='EVENT_AND_TRIAL_COUNTS', category_order=5, category='TRIALS_WITH_BOTH_DIRECTIONS', count=len(both_directions), denominator=total_trials, unit='TRIAL', notes='These trials make direction-specific trial totals non-additive.'))
    for direction in directions:
        selected = [row for row in details if row['direction'] == direction]
        for state, prefix in (('OLD_REPORTED_START', 'old'), ('NEW_REPORTED_START', 'new')):
            exact = [row for row in selected if row[f'{prefix}_exact_lag_days'] is not None]
            bin_counts = Counter((row[f'{prefix}_exact_lag_bin'] for row in exact))
            for order, (code, _, _, label) in enumerate(MAGNITUDE_BINS, start=1):
                summary.append(_summary_row(analysis=BOUNDARY_FLAG, section='EXACT_SIGNED_DAY_LAG', direction=direction, start_state=state, category_order=order, category=code, count=bin_counts[code], denominator=len(exact), unit='EVENT', notes=label))
            partial = Counter((row[f'{prefix}_start_precision'] for row in selected if row[f'{prefix}_exact_lag_days'] is None))
            for order, precision in enumerate(('MONTH', 'YEAR', 'UNKNOWN', 'MISSING'), start=1):
                if partial[precision]:
                    summary.append(_summary_row(analysis=BOUNDARY_FLAG, section='PARTIAL_DATE_INTERVALS', direction=direction, start_state=state, category_order=order, category=f'{precision}_PRECISION', count=partial[precision], denominator=len(selected), unit='EVENT', notes='See event detail for each possible signed day-lag range.'))
    return summary
