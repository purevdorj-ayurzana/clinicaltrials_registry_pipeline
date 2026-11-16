from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


NCT_PATTERN = re.compile(r"^NCT\d{8}$", re.IGNORECASE)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canonical_json_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_write_bytes(path: str | Path, content: bytes, *, overwrite: bool = False) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {output}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(content)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_text(
    path: str | Path, content: str, *, encoding: str = "utf-8", overwrite: bool = False
) -> None:
    atomic_write_bytes(path, content.encode(encoding), overwrite=overwrite)


def atomic_write_json(path: str | Path, value: Any, *, overwrite: bool = False) -> None:
    atomic_write_text(
        path,
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        overwrite=overwrite,
    )


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return [{key: (value or "") for key, value in row.items()} for row in csv.DictReader(handle)]


def csv_columns(path: str | Path) -> list[str]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        return next(reader, [])


def atomic_write_csv(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
    columns: Sequence[str],
    *,
    overwrite: bool = False,
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {output}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow({column: serialize_csv_value(row.get(column, "")) for column in columns})
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def serialize_csv_value(value: Any) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (dict, list, tuple)):
        return canonical_json(value)
    return value


def normalize_nct_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "", (value or "").strip()).upper()
    if not NCT_PATTERN.fullmatch(normalized):
        raise ValueError(f"Invalid NCT identifier: {value!r}")
    return normalized


def date_precision(value: str) -> str:
    if not value:
        return ""
    if re.fullmatch(r"\d{4}", value):
        return "YEAR"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return "MONTH"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "DAY"
    if "T" in value:
        return "DATETIME"
    return "UNKNOWN"


def parse_partial_date(value: str, *, boundary: str = "start") -> date | None:
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{4}", value):
            return date(int(value), 1 if boundary == "start" else 12, 1 if boundary == "start" else 31)
        if re.fullmatch(r"\d{4}-\d{2}", value):
            year, month = (int(part) for part in value.split("-"))
            if boundary == "start":
                return date(year, month, 1)
            if month == 12:
                return date(year, 12, 31)
            return date(year, month + 1, 1).fromordinal(date(year, month + 1, 1).toordinal() - 1)
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def path_get(document: Any, dotted_path: str) -> tuple[str, Any]:
    current = document
    for component in dotted_path.split("."):
        if not isinstance(current, dict) or component not in current:
            return "ABSENT", None
        current = current[component]
    if current is None or current == "" or current == [] or current == {}:
        return "PRESENT_EMPTY", current
    return "PRESENT_VALUE", current


def stable_text(value: str) -> str:
    return " ".join((value or "").casefold().split())


def source_provenance(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    files = [root / "config.json", *sorted((root / "src").rglob("*.py"))]
    records = {
        str(path.relative_to(root)).replace("\\", "/"): sha256_file(path)
        for path in files
        if path.is_file()
    }
    return {"files": records, "bundle_hash": canonical_json_hash(records)}
