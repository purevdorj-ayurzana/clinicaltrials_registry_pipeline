from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any

def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode('utf-8'))

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8-sig'))
