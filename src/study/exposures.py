from __future__ import annotations
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Mapping, Sequence
from study.trial_dates import classify_event_window
from study.event_timing import classify_two_window
from study.event_timing import classify_first_occurrence
PARENT_FLAG_ORDER = ['STUDY_TYPE_CHANGED', 'ALLOCATION_CHANGED', 'INTERVENTION_MODEL_CHANGED', 'PRIMARY_PURPOSE_CHANGED', 'MASKING_CHANGED', 'NUMBER_OF_ARMS_CHANGED', 'PLANNED_ENROLLMENT_CHANGED', 'PRIMARY_OUTCOME_CHANGED', 'ARMS_INTERVENTIONS_CHANGED', 'START_DATE_CROSSED_FIRST_SUBMISSION_BOUNDARY', 'FIRST_POST_AFTER_STUDY_START']
SUBTYPE_ORDER = [('PRIMARY_OUTCOME_CHANGED', 'PRIMARY_OUTCOME_ADDED'), ('PRIMARY_OUTCOME_CHANGED', 'PRIMARY_OUTCOME_REMOVED'), ('PRIMARY_OUTCOME_CHANGED', 'OUTCOME_PROMOTED_TO_PRIMARY'), ('PRIMARY_OUTCOME_CHANGED', 'PRIMARY_OUTCOME_DEMOTED'), ('PRIMARY_OUTCOME_CHANGED', 'PRIMARY_OUTCOME_TIME_FRAME_CHANGED'), ('ARMS_INTERVENTIONS_CHANGED', 'ARM_ADDED'), ('ARMS_INTERVENTIONS_CHANGED', 'ARM_REMOVED'), ('ARMS_INTERVENTIONS_CHANGED', 'ARM_TYPE_CHANGED'), ('ARMS_INTERVENTIONS_CHANGED', 'INTERVENTION_ADDED'), ('ARMS_INTERVENTIONS_CHANGED', 'INTERVENTION_REMOVED'), ('ARMS_INTERVENTIONS_CHANGED', 'INTERVENTION_ARM_ASSIGNMENT_CHANGED')]
TIMING_EXCLUDED_FLAGS = {'NUMBER_OF_ARMS_CHANGED', 'FIRST_POST_AFTER_STUDY_START'}
TERM_COLUMNS = ['exposure_id', 'exposure_level', 'flag_code', 'subtype_code', 'display_label', 'timing_applicable', 'atomic_flag_rows', 'unique_transition_events', 'exposed_trials_any', 'four_window_population_trials', 'two_window_population_trials']
EVENT_COLUMNS = ['exposure_id', 'exposure_level', 'flag_code', 'subtype_code', 'nct_id', 'endpoint_label', 'from_version', 'to_version', 'event_posting_date', 'timing_window', 'atomic_flag_rows_collapsed']
EXPOSURE_COLUMNS = ['nct_id', 'source_nct_id', 'endpoint_label', 'exposure_id', 'exposure_level', 'flag_code', 'subtype_code', 'timing_applicable', 'any_occurrence', 'unique_event_count', 'first_event_posting_date', 'index_primary_publication_date', 'four_window_population_eligible', 'four_window_evaluable', 'four_window_category', 'four_window_indeterminate_boundary', 'two_window_population_eligible', 'two_window_evaluable', 'two_window_category', 'two_window_indeterminate_boundary']

def _term_id(level: str, flag_code: str, subtype_code: str='') -> str:
    return f'{level}|{flag_code}' + (f'|{subtype_code}' if subtype_code else '')

def build_terms(flag_rows: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    observed_flags = {row['flag_code'] for row in flag_rows}
    unknown_flags = sorted(observed_flags - set(PARENT_FLAG_ORDER))
    if unknown_flags:
        raise ValueError(f'Unexpected parent flag codes: {unknown_flags}')
    observed_subtypes = {(row['flag_code'], row['change_subtype_code']) for row in flag_rows if row['change_subtype_code']}
    expected_subtypes = set(SUBTYPE_ORDER)
    if observed_subtypes != expected_subtypes:
        raise ValueError(f'Subtype schema mismatch; missing={sorted(expected_subtypes - observed_subtypes)}, unexpected={sorted(observed_subtypes - expected_subtypes)}')
    terms: list[dict[str, Any]] = []
    for flag in PARENT_FLAG_ORDER:
        terms.append({'exposure_id': _term_id('PARENT', flag), 'exposure_level': 'PARENT', 'flag_code': flag, 'subtype_code': '', 'display_label': flag, 'timing_applicable': flag not in TIMING_EXCLUDED_FLAGS})
        for subtype_flag, subtype in SUBTYPE_ORDER:
            if subtype_flag != flag:
                continue
            terms.append({'exposure_id': _term_id('SUBTYPE', flag, subtype), 'exposure_level': 'SUBTYPE', 'flag_code': flag, 'subtype_code': subtype, 'display_label': subtype, 'timing_applicable': True})
    return terms

def aggregate_events(trials: Mapping[str, Mapping[str, Any]], flag_rows: Sequence[Mapping[str, str]], terms: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    term_by_id = {str(term['exposure_id']): term for term in terms}
    event_map: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    atomic_counts: Counter[str] = Counter()
    for row in flag_rows:
        nct_id = row['nct_id']
        if nct_id not in trials:
            continue
        ids = [_term_id('PARENT', row['flag_code'])]
        if row['change_subtype_code']:
            ids.append(_term_id('SUBTYPE', row['flag_code'], row['change_subtype_code']))
        for exposure_id in ids:
            if exposure_id not in term_by_id:
                raise ValueError(f'Unregistered exposure term: {exposure_id}')
            atomic_counts[exposure_id] += 1
            key = (exposure_id, nct_id, row['from_version'], row['to_version'], row['event_posting_date'])
            if key not in event_map:
                term = term_by_id[exposure_id]
                event_map[key] = {'exposure_id': exposure_id, 'exposure_level': term['exposure_level'], 'flag_code': term['flag_code'], 'subtype_code': term['subtype_code'], 'nct_id': nct_id, 'endpoint_label': trials[nct_id]['endpoint_label'], 'from_version': row['from_version'], 'to_version': row['to_version'], 'event_posting_date': row['event_posting_date'], 'atomic_flag_rows_collapsed': 0}
            event_map[key]['atomic_flag_rows_collapsed'] += 1
    event_rows: list[dict[str, Any]] = []
    by_trial_term: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    term_index = {term['exposure_id']: index for index, term in enumerate(terms)}
    for key in sorted(event_map, key=lambda item: (term_index[item[0]], item[1], item[4], item[2], item[3])):
        event = event_map[key]
        trial = trials[event['nct_id']]
        term = term_by_id[event['exposure_id']]
        event['timing_window'] = classify_event_window(event['event_posting_date'], trial['study_start_anchor'], trial['primary_completion_anchor']) if term['timing_applicable'] else 'NOT_APPLICABLE'
        event_rows.append(event)
        by_trial_term[event['nct_id'], event['exposure_id']].append(event)
    exposure_rows: list[dict[str, Any]] = []
    for nct_id in sorted(trials):
        trial = trials[nct_id]
        for term in terms:
            events = by_trial_term.get((nct_id, term['exposure_id']), [])
            applicable = bool(term['timing_applicable'])
            event_dates = sorted((date.fromisoformat(event['event_posting_date']) for event in events))
            four_eligible = bool(applicable and trial['four_window_population_eligible'])
            two_eligible = bool(applicable and trial['two_window_population_eligible'])
            four_category = ''
            four_boundary = ''
            if four_eligible:
                four_category, four_boundary = classify_first_occurrence(event_dates, trial['study_start_anchor']['earliest'], trial['study_start_anchor']['latest'], trial['primary_completion_anchor']['earliest'], trial['primary_completion_anchor']['latest'], date.fromisoformat(trial['index_primary_publication_date']))
            two_category = ''
            two_boundary = ''
            if two_eligible:
                two_category, two_boundary = classify_two_window(event_dates, trial['primary_completion_anchor']['earliest'], trial['primary_completion_anchor']['latest'])
            exposure_rows.append({'nct_id': nct_id, 'source_nct_id': trial['source_nct_id'], 'endpoint_label': trial['endpoint_label'], 'exposure_id': term['exposure_id'], 'exposure_level': term['exposure_level'], 'flag_code': term['flag_code'], 'subtype_code': term['subtype_code'], 'timing_applicable': applicable, 'any_occurrence': bool(events), 'unique_event_count': len(events), 'first_event_posting_date': event_dates[0].isoformat() if event_dates else '', 'index_primary_publication_date': trial['index_primary_publication_date'], 'four_window_population_eligible': four_eligible, 'four_window_evaluable': bool(four_eligible and four_category != 'TIMING_INDETERMINATE'), 'four_window_category': four_category, 'four_window_indeterminate_boundary': four_boundary, 'two_window_population_eligible': two_eligible, 'two_window_evaluable': bool(two_eligible and two_category != 'TIMING_INDETERMINATE'), 'two_window_category': two_category, 'two_window_indeterminate_boundary': two_boundary})
    trial_counts = Counter(((row['exposure_id'], row['nct_id']) for row in exposure_rows if row['any_occurrence']))
    term_rows = []
    for term in terms:
        exposure_id = term['exposure_id']
        term_rows.append({**term, 'atomic_flag_rows': atomic_counts[exposure_id], 'unique_transition_events': sum((event['exposure_id'] == exposure_id for event in event_rows)), 'exposed_trials_any': sum((key[0] == exposure_id for key in trial_counts)), 'four_window_population_trials': sum((row['exposure_id'] == exposure_id and row['four_window_population_eligible'] for row in exposure_rows)), 'two_window_population_trials': sum((row['exposure_id'] == exposure_id and row['two_window_population_eligible'] for row in exposure_rows))})
    return (event_rows, exposure_rows, term_rows)
