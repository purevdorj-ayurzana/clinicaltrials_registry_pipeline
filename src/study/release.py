"""Portable storage and integrity checks for the frozen study inputs."""
from __future__ import annotations
import csv
import hashlib
import json
import math
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]

def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()

def cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return f"{value:.12g}"
    return value

def write_csv(path, rows, columns=None):
    rows = list(rows)
    if columns is None:
        if not rows:
            raise ValueError(f"Columns required for empty table: {path}")
        columns = list(rows[0])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: cell(row.get(key)) for key in columns})

def safe_member(member):
    path = PurePosixPath(member.name)
    if (not member.isfile() or path.is_absolute() or ".." in path.parts
            or "\\" in member.name or ":" in member.name or not path.parts):
        raise ValueError(f"Unsafe archive entry: {member.name}")
    return path

def restore_archives(destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    seen = set()
    for archive in sorted((ROOT / "data/registry").glob("records_*.tar.xz")):
        with tarfile.open(archive, "r:xz") as handle:
            for member in handle:
                relative = safe_member(member)
                if member.name in seen:
                    raise ValueError(f"Duplicate archive entry: {member.name}")
                seen.add(member.name)
                target = destination.joinpath(*relative.parts)
                if not target.resolve().is_relative_to(destination) or target.exists():
                    raise ValueError(f"Refusing to overwrite archive destination: {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.extractfile(member) as source, target.open("xb") as output:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(block)

def verify_data(deep=True):
    from clinicaltrials_api.cohort import verify_classifier_export
    manifest = read_json(ROOT / "data/manifest.json")
    for relative, expected in manifest["files"].items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file() or sha256(path) != expected:
            raise ValueError(f"Missing or changed study asset: {relative}")
    verified = verify_classifier_export(ROOT / "data/classifier")
    upstream = verified["manifest"]
    for key, expected in {"selected_publications": 2178, "pmid_nct_associations": 1890,
                          "unique_candidate_nct_ids": 1759, "manual_verified_corrections": 2}.items():
        if upstream[key] != expected:
            raise ValueError(f"Classifier count mismatch: {key}")
    assessments = read_csv(ROOT / "data/semantic_assessment/assessments.csv")
    if len(assessments) != 1758 or len({r["nct_id"] for r in assessments}) != 1758:
        raise ValueError("Semantic assessment trial identity mismatch")
    if sum(r["meaningful_change"].lower() == "true" for r in assessments) != 780:
        raise ValueError("Semantic assessment label count mismatch")
    origins = [r["result_origin"] for r in assessments]
    if origins.count("openai_api") != 1367 or origins.count("deterministic_no_comparison") != 391:
        raise ValueError("Semantic assessment origin mismatch")
    raw_count = 0
    if deep:
        actual, retrievals, seen = {}, {}, set()
        for archive in sorted((ROOT / "data/registry").glob("records_*.tar.xz")):
            with tarfile.open(archive, "r:xz") as handle:
                for member in handle:
                    relative = safe_member(member)
                    if member.name in seen:
                        raise ValueError(f"Duplicate archive entry: {member.name}")
                    seen.add(member.name)
                    with handle.extractfile(member) as source:
                        digest = hashlib.sha256()
                        content = bytearray() if relative.name == "retrieval_manifest.json" else None
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(block)
                            if content is not None:
                                content.extend(block)
                    actual[member.name] = digest.hexdigest()
                    if content is not None:
                        retrievals[relative.parent.as_posix()] = json.loads(content)
                    if relative.name.startswith("version_"):
                        raw_count += 1
        for trial, retrieval in retrievals.items():
            for record in retrieval["files"].values():
                relative = f"{trial}/{record['file']}"
                if actual.get(relative) != record["sha256"]:
                    raise ValueError(f"Raw registry response missing or changed: {relative}")
        if raw_count != 54631 or len(retrievals) != 1761:
            raise ValueError("Archived registry count mismatch")
    return {"exported_identifiers": 1759, "canonical_trials": 1758,
            "historical_versions": raw_count if deep else "not expanded", "status": "VERIFIED"}

def compare_csv(actual_path, reference_path, ignored=(), tolerance=1e-5):
    actual, reference = read_csv(actual_path), read_csv(reference_path)
    if len(actual) != len(reference):
        raise ValueError(f"Row count differs: {reference_path.name}: {len(actual)} != {len(reference)}")
    if not reference:
        return
    columns = [key for key in reference[0] if key not in ignored]
    for number, (observed, expected) in enumerate(zip(actual, reference, strict=True), 2):
        for key in columns:
            left, right = observed.get(key, ""), expected[key]
            if left.replace("\\", "/") == right.replace("\\", "/"):
                continue
            if left.startswith(("[", "{")) and right.startswith(("[", "{")):
                if json.loads(left) == json.loads(right):
                    continue
            exact = ("pmid" in key.lower() or "nct_id" in key.lower()
                     or key in {"count", "trials", "flag_rows", "positive", "negative"}
                     or key.endswith(("_trials", "_count", "_positive", "_negative", "_rows")))
            if exact:
                raise ValueError(f"Scientific value differs: {reference_path.name}:{number} {key}: {left!r} != {right!r}")
            try:
                a, b = float(left), float(right)
            except ValueError:
                raise ValueError(f"Scientific value differs: {reference_path.name}:{number} {key}: {left!r} != {right!r}")
            if not math.isclose(a, b, rel_tol=tolerance, abs_tol=1e-6 if "p_value" in key or "_p" in key else 1e-9):
                raise ValueError(f"Numerical value differs: {reference_path.name}:{number} {key}: {left} != {right}")
