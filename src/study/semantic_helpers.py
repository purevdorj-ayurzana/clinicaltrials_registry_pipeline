from __future__ import annotations
import html
from typing import Any
CHANGE_TYPES = ('outcome_replacement', 'addition', 'removal', 'definition_or_components', 'measurement_method', 'threshold', 'timeframe', 'other')

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)

def normalize_field(value: str) -> str:
    return ' '.join(html.unescape(value).split())

def _raw_fields(item: Any, label: str) -> tuple[dict[str, str], list[str]]:
    _require(isinstance(item, dict), f'{label}: primary outcome is not an object')
    _require(set(item) <= {'measure', 'description', 'timeFrame', 'reviewUnit'}, f'{label}: unknown outcome keys {set(item) - {'measure', 'description', 'timeFrame', 'reviewUnit'}}')
    fields: dict[str, str] = {}
    missing: list[str] = []
    for api_name, output_name in (('measure', 'title'), ('description', 'description'), ('timeFrame', 'timeframe')):
        if api_name not in item:
            missing.append(api_name)
        value = item.get(api_name, '')
        _require(isinstance(value, str), f'{label}: {api_name} is not text')
        fields[output_name] = value
    return (fields, missing)

def collapse_states(versions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    states: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    previous: tuple[tuple[str, str, str], ...] | None = None
    for version in versions:
        normalized = [{key: normalize_field(item[key]) for key in ('title', 'description', 'timeframe')} for item in version['outcomes']]
        key = tuple(((item['title'], item['description'], item['timeframe']) for item in normalized))
        retained = key != previous
        if retained:
            states.append({'version': version['version'], 'outcomes': normalized})
        ledger.append({**version, 'retained': retained, 'state_start_version': states[-1]['version']})
        previous = key
    return (states, ledger)

def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, f'Duplicate JSON key: {key}')
        result[key] = value
    return result
