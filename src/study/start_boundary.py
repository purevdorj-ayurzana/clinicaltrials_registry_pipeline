from __future__ import annotations
from collections import defaultdict
from typing import Any, Mapping, Sequence
R_TO_P = 'RETROSPECTIVE_TO_PROSPECTIVE'
P_TO_R = 'PROSPECTIVE_TO_RETROSPECTIVE'

def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().upper()
    if text == 'TRUE':
        return True
    if text in {'FALSE', ''}:
        return False
    raise ValueError(f'Invalid Boolean value: {value!r}')

def classify_direction_history(directions: Sequence[str]) -> tuple[str, bool]:
    observed = set(directions)
    if not observed:
        return ('NO_CROSSING', False)
    if not observed.issubset({R_TO_P, P_TO_R}):
        raise ValueError(f'Unexpected direction set: {sorted(observed)}')
    if observed == {R_TO_P}:
        return ('RETROSPECTIVE_TO_PROSPECTIVE_ONLY', True)
    if observed == {P_TO_R}:
        return ('PROSPECTIVE_TO_RETROSPECTIVE_ONLY', False)
    return ('BIDIRECTIONAL', False)

def build_trial_exposure(covariates: Sequence[Mapping[str, str]], events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_trial: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        by_trial[str(event['nct_id'])].append(event)
    output: list[dict[str, Any]] = []
    for row in sorted(covariates, key=lambda item: item['nct_id']):
        trial_events = by_trial.get(row['nct_id'], [])
        history, exposed = classify_direction_history([str(event['direction']) for event in trial_events])
        output.append({'nct_id': row['nct_id'], 'endpoint_label': row['endpoint_label'], 'endpoint_positive': row['endpoint_label'] == 'POSITIVE', 'complete_case_eligible': _bool(row['complete_case_eligible']), 'retrospective_to_prospective_events': sum((event['direction'] == R_TO_P for event in trial_events)), 'prospective_to_retrospective_events': sum((event['direction'] == P_TO_R for event in trial_events)), 'direction_history': history, 'revised_flag_exposed': exposed})
    return output
