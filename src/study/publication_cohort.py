from __future__ import annotations
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any
VALID_ENDPOINT_LABELS = frozenset({'POSITIVE', 'NEGATIVE', 'UNCLEAR'})
PRODUCTION_EXPECTED_COUNTS = {'initially_linked_trials': 1759, 'no_clear_endpoint_excluded': 193, 'at_least_one_clear_endpoint': 1566, 'positive_negative_discordant_excluded': 13, 'concordantly_labelled': 1553, 'relevant_multi_nct_publications': 57, 'relevant_multi_nct_pairs': 118, 'multi_nct_affected_trials_excluded': 110, 'multi_nct_positive_excluded': 93, 'multi_nct_negative_excluded': 17, 'final_mapping_cohort': 1443, 'positive': 993, 'negative': 450, 'publication_timing_evaluable': 1410, 'publication_timing_unevaluable': 33, 'timing_unevaluable_positive': 24, 'timing_unevaluable_negative': 9}

def _canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

def _pmid(value: str) -> str:
    normalized = (value or '').strip()
    if not normalized or not normalized.isdigit() or int(normalized) <= 0:
        raise ValueError(f'Missing or invalid PMID: {value!r}')
    return str(int(normalized))

def _false_value(value: str) -> bool:
    return (value or '').strip().casefold() in {'', '0', 'false', 'no'}

def _publication_date(value: str) -> str:
    raw = (value or '').strip()
    if not raw:
        return ''
    for pattern in ('%Y/%m/%d', '%Y-%m-%d'):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f'Publication date is not a complete valid date: {value!r}')

def validate_endpoint_rows(rows: list[dict[str, str]], *, endpoint_run_id: str, upstream_rct_run_id: str, endpoint_arm: str, prompt_hash: str, schema_hash: str, model: str, reasoning_effort: str, expected_pmid_hash: str) -> tuple[dict[str, dict[str, str]], Counter[str]]:
    if not rows:
        raise ValueError('Endpoint assessment export is empty')
    normalized: dict[str, dict[str, str]] = {}
    labels: Counter[str] = Counter()
    for source in rows:
        row = dict(source)
        pmid = _pmid(row.get('pmid', ''))
        if pmid in normalized:
            raise ValueError(f'Duplicate PMID in endpoint assessments: {pmid}')
        label = (row.get('primary_endpoint_result') or '').strip().upper()
        if label not in VALID_ENDPOINT_LABELS:
            raise ValueError(f'Invalid endpoint label for PMID {pmid}: {label!r}')
        expected_values = {'run_id': endpoint_run_id, 'upstream_rct_run_id': upstream_rct_run_id, 'endpoint_arm': endpoint_arm, 'model': model, 'reasoning_effort': reasoning_effort, 'prompt_hash': prompt_hash, 'schema_hash': schema_hash, 'upstream_rct_final_label': 'PRIMARY_RCT_REPORT'}
        for column, expected in expected_values.items():
            if row.get(column, '') != expected:
                raise ValueError(f'Endpoint row {pmid} has inconsistent {column}: {row.get(column, '')!r} != {expected!r}')
        if row.get('error_code') or row.get('error') or (not _false_value(row.get('technical_retry_pending', ''))):
            raise ValueError(f'Endpoint row {pmid} retains an unresolved technical failure')
        row['pmid'] = pmid
        row['primary_endpoint_result'] = label
        normalized[pmid] = row
        labels[label] += 1
    calculated_pmid_hash = _canonical_json_hash(sorted(normalized))
    if calculated_pmid_hash != expected_pmid_hash:
        raise ValueError(f'Endpoint PMID hash mismatch: {calculated_pmid_hash} != {expected_pmid_hash}')
    return (normalized, labels)

def derive_mapping(links: list[dict[str, str]], endpoint_by_pmid: dict[str, dict[str, str]]) -> dict[str, Any]:
    links_by_trial: dict[str, list[dict[str, str]]] = defaultdict(list)
    links_by_publication: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in links:
        links_by_trial[row['nct_id_normalized']].append(row)
        links_by_publication[row['pmid']].append(row)
    original_cardinality = {pmid: len(rows) for pmid, rows in links_by_publication.items()}
    original_multi_pmids = {pmid for pmid, count in original_cardinality.items() if count > 1}
    clear_multi_pmids = {pmid for pmid in original_multi_pmids if endpoint_by_pmid[pmid]['primary_endpoint_result'] in {'POSITIVE', 'NEGATIVE'}}
    exclusion_reason: dict[str, str] = {}
    trial_details: dict[str, dict[str, Any]] = {}
    no_clear: set[str] = set()
    discordant: set[str] = set()
    otherwise_eligible: set[str] = set()
    for nct_id, trial_links in links_by_trial.items():
        ordered = sorted(trial_links, key=lambda row: int(row['pmid']))
        clear = [row for row in ordered if endpoint_by_pmid[row['pmid']]['primary_endpoint_result'] in {'POSITIVE', 'NEGATIVE'}]
        clear_labels = {endpoint_by_pmid[row['pmid']]['primary_endpoint_result'] for row in clear}
        detail = {'all_links': ordered, 'clear_links': clear, 'clear_labels': clear_labels, 'multi_clear_pmids': sorted({row['pmid'] for row in clear if row['pmid'] in clear_multi_pmids}, key=int)}
        trial_details[nct_id] = detail
        if not clear:
            no_clear.add(nct_id)
            exclusion_reason[nct_id] = 'NO_CLEAR_ENDPOINT_AFTER_REMOVING_UNCLEAR'
        elif clear_labels == {'POSITIVE', 'NEGATIVE'}:
            discordant.add(nct_id)
            exclusion_reason[nct_id] = 'BOTH_POSITIVE_AND_NEGATIVE_CLEAR_ASSESSMENTS'
        else:
            otherwise_eligible.add(nct_id)
    relevant_multi_pmids = {pmid for pmid in clear_multi_pmids if any((row['nct_id_normalized'] in otherwise_eligible for row in links_by_publication[pmid]))}
    multi_affected = {nct_id for nct_id in otherwise_eligible if trial_details[nct_id]['multi_clear_pmids']}
    for nct_id in multi_affected:
        exclusion_reason[nct_id] = 'LINKED_TO_ORIGINALLY_MULTI_NCT_PUBLICATION'
    retained = otherwise_eligible - multi_affected
    mapping_rows: list[dict[str, Any]] = []
    for nct_id in sorted(retained):
        detail = trial_details[nct_id]
        clear_links = detail['clear_links']
        endpoint_label = next(iter(detail['clear_labels']))
        primary = [row for row in clear_links if row.get('publication_role') == 'PRIMARY_RESULTS']
        dated_primary: list[tuple[date, int, str, dict[str, str]]] = []
        for row in primary:
            normalized_date = _publication_date(row.get('publication_date', ''))
            if normalized_date:
                dated_primary.append((date.fromisoformat(normalized_date), int(row['pmid']), normalized_date, row))
        dated_primary.sort(key=lambda item: (item[0], item[1]))
        index = dated_primary[0] if dated_primary else None
        mapping_rows.append({'nct_id': nct_id, 'endpoint_label': endpoint_label, 'linked_publication_count': len(detail['all_links']), 'linked_clear_publication_count': len(clear_links), 'linked_clear_pmids': [row['pmid'] for row in clear_links], 'clear_primary_results_publication_count': len(primary), 'dated_clear_primary_results_publication_count': len(dated_primary), 'linked_clear_primary_results_pmids': sorted({row['pmid'] for row in primary}, key=int), 'index_primary_publication_pmid': index[3]['pmid'] if index else '', 'index_primary_publication_date': index[2] if index else '', 'primary_publication_timing_evaluable': bool(index)})
    ledger_rows: list[dict[str, Any]] = []
    reason_order = {'NO_CLEAR_ENDPOINT_AFTER_REMOVING_UNCLEAR': 1, 'BOTH_POSITIVE_AND_NEGATIVE_CLEAR_ASSESSMENTS': 2, 'LINKED_TO_ORIGINALLY_MULTI_NCT_PUBLICATION': 3}
    for nct_id in sorted(exclusion_reason, key=lambda value: (reason_order[exclusion_reason[value]], value)):
        detail = trial_details[nct_id]
        ledger_rows.append({'nct_id': nct_id, 'exclusion_step': reason_order[exclusion_reason[nct_id]], 'exclusion_reason': exclusion_reason[nct_id], 'linked_pmids': [row['pmid'] for row in detail['all_links']], 'linked_endpoint_labels': [{'pmid': row['pmid'], 'label': endpoint_by_pmid[row['pmid']]['primary_endpoint_result']} for row in detail['all_links']], 'linked_clear_pmids': [row['pmid'] for row in detail['clear_links']], 'clear_endpoint_labels': sorted(detail['clear_labels']), 'supporting_multi_nct_pmids': detail['multi_clear_pmids']})
    multi_rows: list[dict[str, Any]] = []
    for pmid in sorted(relevant_multi_pmids, key=int):
        endpoint = endpoint_by_pmid[pmid]
        for row in sorted(links_by_publication[pmid], key=lambda item: item['nct_id_normalized']):
            nct_id = row['nct_id_normalized']
            detail = trial_details[nct_id]
            has_single_clear = any((original_cardinality[item['pmid']] == 1 for item in detail['clear_links']))
            multi_rows.append({'pmid': pmid, 'nct_id': nct_id, 'original_publication_nct_cardinality': original_cardinality[pmid], 'endpoint_label': endpoint['primary_endpoint_result'], 'publication_role': row.get('publication_role', ''), 'publication_date': _publication_date(row.get('publication_date', '')), 'otherwise_eligible_before_multi_nct': nct_id in otherwise_eligible, 'excluded_by_multi_nct_rule': nct_id in multi_affected, 'trial_sequential_exclusion_reason': exclusion_reason.get(nct_id, 'RETAINED'), 'also_has_single_nct_clear_publication': has_single_clear})
    mapping_label_counts = Counter((row['endpoint_label'] for row in mapping_rows))
    unevaluable = [row for row in mapping_rows if not row['primary_publication_timing_evaluable']]
    unevaluable_labels = Counter((row['endpoint_label'] for row in unevaluable))
    multi_labels = Counter((next(iter(trial_details[nct_id]['clear_labels'])) for nct_id in multi_affected))
    counts = {'initially_linked_trials': len(links_by_trial), 'no_clear_endpoint_excluded': len(no_clear), 'at_least_one_clear_endpoint': len(links_by_trial) - len(no_clear), 'positive_negative_discordant_excluded': len(discordant), 'concordantly_labelled': len(otherwise_eligible), 'relevant_multi_nct_publications': len(relevant_multi_pmids), 'relevant_multi_nct_pairs': len(multi_rows), 'multi_nct_affected_trials_excluded': len(multi_affected), 'multi_nct_positive_excluded': multi_labels['POSITIVE'], 'multi_nct_negative_excluded': multi_labels['NEGATIVE'], 'final_mapping_cohort': len(mapping_rows), 'positive': mapping_label_counts['POSITIVE'], 'negative': mapping_label_counts['NEGATIVE'], 'publication_timing_evaluable': len(mapping_rows) - len(unevaluable), 'publication_timing_unevaluable': len(unevaluable), 'timing_unevaluable_positive': unevaluable_labels['POSITIVE'], 'timing_unevaluable_negative': unevaluable_labels['NEGATIVE']}
    flow_rows = [{'stage_number': 0, 'stage_code': 'INITIALLY_LINKED_NCT_TRIALS', 'description': 'Distinct exported NCT identifiers with at least one linked publication', 'starting_trials': counts['initially_linked_trials'], 'excluded_trials': 0, 'retained_trials': counts['initially_linked_trials'], 'positive_trials': '', 'negative_trials': ''}, {'stage_number': 1, 'stage_code': 'REMOVE_UNCLEAR_ONLY_TRIALS', 'description': 'Remove UNCLEAR assessments and exclude trials with no remaining clear assessment', 'starting_trials': counts['initially_linked_trials'], 'excluded_trials': counts['no_clear_endpoint_excluded'], 'retained_trials': counts['at_least_one_clear_endpoint'], 'positive_trials': '', 'negative_trials': ''}, {'stage_number': 2, 'stage_code': 'EXCLUDE_POSITIVE_NEGATIVE_DISCORDANCE', 'description': 'Exclude trials retaining both POSITIVE and NEGATIVE publication assessments', 'starting_trials': counts['at_least_one_clear_endpoint'], 'excluded_trials': counts['positive_negative_discordant_excluded'], 'retained_trials': counts['concordantly_labelled'], 'positive_trials': '', 'negative_trials': ''}, {'stage_number': 3, 'stage_code': 'EXCLUDE_ORIGINALLY_MULTI_NCT_PUBLICATIONS', 'description': 'Exclude otherwise-eligible trials linked to a clear publication that originally mapped to multiple NCT records', 'starting_trials': counts['concordantly_labelled'], 'excluded_trials': counts['multi_nct_affected_trials_excluded'], 'retained_trials': counts['final_mapping_cohort'], 'positive_trials': counts['positive'], 'negative_trials': counts['negative']}, {'stage_number': 4, 'stage_code': 'PUBLICATION_TIMING_EVALUABLE', 'description': 'Retained trials with a dated clear PRIMARY_RESULTS publication', 'starting_trials': counts['final_mapping_cohort'], 'excluded_trials': counts['publication_timing_unevaluable'], 'retained_trials': counts['publication_timing_evaluable'], 'positive_trials': counts['positive'] - counts['timing_unevaluable_positive'], 'negative_trials': counts['negative'] - counts['timing_unevaluable_negative']}]
    return {'mapping_rows': mapping_rows, 'ledger_rows': ledger_rows, 'multi_rows': multi_rows, 'flow_rows': flow_rows, 'counts': counts, 'trial_details': trial_details, 'original_cardinality': original_cardinality}
