from __future__ import annotations
import copy
import json
from collections import defaultdict
from typing import Any
from study import semantic_helpers as condensed, lexical_matching as v2
RECLASSIFICATION_TYPES = ('outcome promotion', 'outcome demotion')
CHANGE_TYPES = (*condensed.CHANGE_TYPES, *RECLASSIFICATION_TYPES)
INFORMATIVE_TITLE_EXCLUSIONS = frozenset(v2._GRAMMAR_TOKENS) | {'number', 'percentage', 'percent', 'participants', 'participant', 'patients', 'patient', 'change', 'baseline', 'outcome', 'measure', 'measured', 'week', 'weeks', 'month', 'months', 'year', 'years', 'score', 'rate', 'primary', 'secondary'}

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)

def _model_record(record: dict[str, Any]) -> dict[str, str]:
    return {field: condensed.normalize_field(str(record[field])) for field in ('title', 'description', 'timeframe')}

def _metric_record(record: dict[str, Any]) -> dict[str, str]:
    return {'measure': str(record['title']), 'description': str(record['description']), 'time_frame': str(record['timeframe'])}

def _clerical_key(record: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    value = _metric_record(record)
    return tuple((tuple(v2.clerical_tokens(value[field])) for field in ('measure', 'description', 'time_frame')))

def _distance(left: str, right: str) -> dict[str, Any]:
    return v2._distance_metrics(v2.clerical_tokens(left), v2.clerical_tokens(right))

def _pair_score(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    old = _metric_record(left)
    new = _metric_record(right)
    title = _distance(old['measure'], new['measure'])
    description = _distance(old['description'], new['description'])
    timeframe = _distance(old['time_frame'], new['time_frame'])
    combined = _distance('\n'.join(old.values()), '\n'.join(new.values()))
    complete_record_exact = _clerical_key(left) == _clerical_key(right)
    rank = [0 if complete_record_exact else 1, combined['cosine_distance'], combined['jaccard_distance'], title['cosine_distance'], title['jaccard_distance'], description['cosine_distance'], timeframe['cosine_distance']]
    return {'rank': rank, 'complete_record_clerical_exact': complete_record_exact, 'title': title, 'description': description, 'timeframe': timeframe, 'combined': combined}

def _informative_title_tokens(record: dict[str, Any]) -> set[str]:
    return {token for token in v2.clerical_tokens(str(record['title'])) if len(token) >= 3 and (not token.isdigit()) and (token not in INFORMATIVE_TITLE_EXCLUSIONS)}

def _plausibility(left: dict[str, Any], right: dict[str, Any], score: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if score['complete_record_clerical_exact']:
        reasons.append('COMPLETE_RECORD_CLERICAL_EXACT')
    if _informative_title_tokens(left) & _informative_title_tokens(right):
        reasons.append('SHARED_INFORMATIVE_TITLE_TOKEN')
    if str(left['description']).strip() and str(right['description']).strip() and score['description']['clerical_exact']:
        reasons.append('NONEMPTY_DESCRIPTION_CLERICAL_EXACT')
    return reasons

def _unique_primary_anchors(old_primary: list[dict[str, Any]], new_primary: list[dict[str, Any]]) -> tuple[list[dict[str, int]], list[int], list[int]]:
    old_by_key: dict[tuple[tuple[str, ...], ...], list[int]] = defaultdict(list)
    new_by_key: dict[tuple[tuple[str, ...], ...], list[int]] = defaultdict(list)
    for index, item in enumerate(old_primary):
        old_by_key[_clerical_key(item)].append(index)
    for index, item in enumerate(new_primary):
        new_by_key[_clerical_key(item)].append(index)
    anchors: list[dict[str, int]] = []
    matched_old: set[int] = set()
    matched_new: set[int] = set()
    for key, old_indexes in old_by_key.items():
        new_indexes = new_by_key.get(key, [])
        if len(old_indexes) == len(new_indexes) == 1:
            old_index, new_index = (old_indexes[0], new_indexes[0])
            anchors.append({'old_primary_index': old_index + 1, 'new_primary_index': new_index + 1})
            matched_old.add(old_index)
            matched_new.add(new_index)
    return (anchors, [index for index in range(len(old_primary)) if index not in matched_old], [index for index in range(len(new_primary)) if index not in matched_new])

def _select_candidates(*, target_primary: list[dict[str, Any]], target_indexes: list[int], same_priority: list[dict[str, Any]], same_priority_indexes: list[int], secondary: list[dict[str, Any]], direction: str, from_version: int, to_version: int, primary_state_version: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    for primary_index in target_indexes:
        primary = target_primary[primary_index]
        scored_secondary = []
        for index, item in enumerate(secondary):
            score = _pair_score(primary, item)
            scored_secondary.append({'secondary_index': index + 1, 'score': score, 'plausibility_reasons': _plausibility(primary, item, score)})
        eligible_secondary = [item for item in scored_secondary if item['plausibility_reasons']]
        best_secondary_indexes: list[int] = []
        best_rank: list[Any] | None = None
        if eligible_secondary:
            best_rank = min((item['score']['rank'] for item in eligible_secondary))
            best_secondary_indexes = [item['secondary_index'] - 1 for item in eligible_secondary if item['score']['rank'] == best_rank]
        same_scores = [_pair_score(primary, same_priority[index])['rank'] for index in same_priority_indexes]
        best_same_rank = min(same_scores) if same_scores else None
        unique_secondary = len(best_secondary_indexes) == 1
        mutual = False
        plausible_reasons: list[str] = []
        secondary_index: int | None = best_secondary_indexes[0] if unique_secondary else None
        if secondary_index is not None:
            candidate = secondary[secondary_index]
            plausible_reasons = _plausibility(primary, candidate, _pair_score(primary, candidate))
            reverse_scores = [(index, _pair_score(target_primary[index], candidate)['rank']) for index in target_indexes if _plausibility(target_primary[index], candidate, _pair_score(target_primary[index], candidate))]
            reverse_best = min((rank for _, rank in reverse_scores), default=None)
            reverse_best_indexes = [index for index, rank in reverse_scores if rank == reverse_best]
            mutual = reverse_best_indexes == [primary_index]
        beats_same_priority = best_rank is not None and (best_same_rank is None or best_rank < best_same_rank)
        accepted = bool(unique_secondary and mutual and beats_same_priority and plausible_reasons)
        reason = 'SELECTED' if accepted else 'NO_SECONDARY_OUTCOMES' if not scored_secondary else 'NO_PLAUSIBLE_SECONDARY' if not eligible_secondary else 'AMBIGUOUS_TOP_SECONDARY_TIE' if not unique_secondary else 'NOT_MUTUAL_NEAREST' if not mutual else 'NOT_BETTER_THAN_PRIMARY_ALTERNATIVE' if not beats_same_priority else 'NO_PLAUSIBILITY_SIGNAL'
        evaluation = {'primary_index': primary_index + 1, 'primary': copy.deepcopy(primary), 'secondary_scores': scored_secondary, 'plausible_secondary_indexes': [item['secondary_index'] for item in eligible_secondary], 'best_secondary_indexes': [index + 1 for index in best_secondary_indexes], 'best_same_priority_rank': best_same_rank, 'unique_best_secondary': unique_secondary, 'mutual_nearest': mutual, 'beats_same_priority_alternative': beats_same_priority, 'plausibility_reasons': plausible_reasons, 'decision': reason}
        evaluations.append(evaluation)
        if accepted and secondary_index is not None:
            selected.append({'from_version': from_version, 'to_version': to_version, 'direction': direction, 'primary_state_version': primary_state_version, 'primary_index': primary_index + 1, 'secondary': _model_record(secondary[secondary_index])})
    return (selected, evaluations)

def parse_assessment(text: Any, trial_input: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(text, str), 'semantic assessment response has no output text')
    try:
        diagnostic = json.loads(text, object_pairs_hook=condensed._unique_object)
    except json.JSONDecodeError as exc:
        raise ValueError('semantic assessment response is not a JSON object') from exc
    _require(isinstance(diagnostic, dict) and set(diagnostic) == {'meaningful_change', 'change_type', 'versions', 'outcome_reclassifications'}, 'semantic assessment response has missing or extra fields')
    _require(type(diagnostic['meaningful_change']) is bool, 'semantic assessment meaningful_change must be a Boolean')
    change_types = diagnostic['change_type']
    versions = diagnostic['versions']
    reclassifications = diagnostic['outcome_reclassifications']
    _require(isinstance(change_types, list) and all((type(item) is str and item in CHANGE_TYPES for item in change_types)), 'semantic assessment change_type has invalid values')
    _require(len(change_types) == len(set(change_types)), 'semantic assessment change_type contains duplicates')
    _require(isinstance(reclassifications, list) and all((type(item) is str and item in RECLASSIFICATION_TYPES for item in reclassifications)), 'semantic assessment outcome_reclassifications has invalid values')
    _require(len(reclassifications) == len(set(reclassifications)), 'semantic assessment outcome_reclassifications contains duplicates')
    _require(isinstance(versions, list) and all((type(item) is str for item in versions)), 'semantic assessment versions must be strings')
    states = trial_input['states']
    state_labels = [f'V{state['version']}' for state in states]
    candidates = trial_input.get('reclassification_candidates', [])
    candidate_pairs = {('outcome promotion' if candidate['direction'] == 'secondary_to_primary' else 'outcome demotion', f'V{candidate['from_version']}', f'V{candidate['to_version']}') for candidate in candidates}
    available_reclassifications = {item[0] for item in candidate_pairs}
    _require(set(reclassifications) <= available_reclassifications, 'semantic assessment reported a reclassification without a supplied candidate')
    decisive_reclassifications = set(change_types) & set(RECLASSIFICATION_TYPES)
    _require(decisive_reclassifications <= set(reclassifications), 'semantic assessment decisive reclassification is absent from outcome_reclassifications')
    if diagnostic['meaningful_change']:
        _require(bool(change_types) and len(versions) == 2, 'Positive semantic assessment result needs a change type and one version pair')
        if decisive_reclassifications:
            _require(all(((item, versions[0], versions[1]) in candidate_pairs for item in decisive_reclassifications)), 'semantic assessment decisive reclassification versions do not match a supplied candidate boundary')
        else:
            _require(all((label in state_labels for label in versions)), 'semantic assessment cited a version absent from its primary states')
            first, last = (state_labels.index(label) for label in versions)
            _require(first < last and (last == first + 1 or (first == 0 and last == len(state_labels) - 1)), 'semantic assessment primary version pair must be consecutive states or earliest-to-latest')
    else:
        _require(not change_types and (not versions) and (not reclassifications), 'Negative semantic assessment result must have empty diagnostic lists')
    _require(not reclassifications or diagnostic['meaningful_change'], 'semantic assessment reclassification requires meaningful_change true')
    return {'meaningful_change': diagnostic['meaningful_change'], 'diagnostic': diagnostic}
