from __future__ import annotations
import html
import math
import re
import unicodedata
from collections import Counter
from typing import Any
_REGISTRY_TAG_RE = re.compile('</?(?:p|br|li|ul|ol|div|span|strong|em|b|i|sup|sub|table|thead|tbody|tr|td|th|h[1-6])(?:\\s[^>]*)?/?>', flags=re.IGNORECASE)
_DASH_TRANSLATION = str.maketrans({'−': '-', '–': '-', '—': '-', '‑': '-'})
_TOKEN_RE = re.compile('<=|>=|!=|==|[<>=±%+/]|(?<!\\w)-(?=\\d)|(?<=\\d)-(?=\\d)|\\d+(?:[.,]\\d+)*|\\w+', flags=re.UNICODE)
_GRAMMAR_TOKENS = {'a', 'an', 'the', 'of', 'to', 'for', 'and', 'or', 'at', 'is', 'are', 'was', 'were'}

def clerical_tokens(value: str) -> list[str]:
    decoded = html.unescape(value or '')
    without_markup = _REGISTRY_TAG_RE.sub(' ', decoded)
    normalized = unicodedata.normalize('NFKC', without_markup).casefold()
    normalized = normalized.translate(_DASH_TRANSLATION)
    normalized = normalized.replace('≤', '<=').replace('≥', '>=')
    normalized = re.sub('(?<=\\d)\\s*-\\s*(?=\\d)', '-', normalized)
    normalized = re.sub('(?<!\\w)-\\s+(?=\\d)', '-', normalized)
    return _TOKEN_RE.findall(normalized)

def _distance_metrics(left_tokens: list[str], right_tokens: list[str]) -> dict[str, Any]:
    left_counts = Counter(left_tokens)
    right_counts = Counter(right_tokens)
    union = set(left_counts) | set(right_counts)
    if not union:
        jaccard = 0.0
    else:
        jaccard = 1.0 - len(set(left_counts) & set(right_counts)) / len(union)
    if not left_counts and (not right_counts):
        cosine = 0.0
    elif not left_counts or not right_counts:
        cosine = 1.0
    else:
        dot = sum((left_counts[token] * right_counts[token] for token in union))
        left_norm = math.sqrt(sum((value * value for value in left_counts.values())))
        right_norm = math.sqrt(sum((value * value for value in right_counts.values())))
        cosine = 1.0 - dot / (left_norm * right_norm)
    return {'clerical_exact': left_tokens == right_tokens, 'jaccard_distance': round(max(0.0, min(1.0, jaccard)), 6), 'cosine_distance': round(max(0.0, min(1.0, cosine)), 6)}
