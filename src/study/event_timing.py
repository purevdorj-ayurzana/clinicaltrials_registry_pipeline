from __future__ import annotations
from datetime import date
from typing import Sequence

def classify_first_occurrence(event_dates: Sequence[date], start_earliest: date, start_latest: date, primary_completion_earliest: date, primary_completion_latest: date, publication_date: date) -> tuple[str, str]:
    if not event_dates:
        return ('NO_EVENT', '')
    first = min(event_dates)
    if first <= start_earliest:
        return ('THROUGH_START', '')
    if first <= start_latest:
        return ('TIMING_INDETERMINATE', 'START')
    if first <= primary_completion_earliest:
        return ('AFTER_START_THROUGH_PRIMARY_COMPLETION', '')
    if first <= primary_completion_latest:
        return ('TIMING_INDETERMINATE', 'PRIMARY_COMPLETION')
    if first <= publication_date:
        return ('AFTER_PRIMARY_COMPLETION_THROUGH_PUBLICATION', '')
    return ('AFTER_PUBLICATION', '')


def classify_two_window(event_dates: Sequence[date], primary_completion_earliest: date, primary_completion_latest: date) -> tuple[str, str]:
    if not event_dates:
        return ('NO_EVENT', '')
    first = min(event_dates)
    if first <= primary_completion_earliest:
        return ('THROUGH_PRIMARY_COMPLETION', '')
    if first <= primary_completion_latest:
        return ('TIMING_INDETERMINATE', 'PRIMARY_COMPLETION')
    return ('AFTER_PRIMARY_COMPLETION', '')
