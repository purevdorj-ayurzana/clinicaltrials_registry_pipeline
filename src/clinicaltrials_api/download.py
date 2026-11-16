from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .client import (
    ClinicalTrialsClient,
    HttpRequestError,
    JsonResponse,
    validate_api_version,
    validate_current_study,
    validate_history_index,
    validate_history_version,
)
from .config import ROOT, RUN_FORMAT_VERSION, config_hash
from .identity import (
    IDENTITY_RESOLUTION_COLUMNS,
    identity_rows_from_run,
    resolve_current_identity,
)
from .util import (
    atomic_write_bytes,
    atomic_write_csv,
    atomic_write_json,
    canonical_json_hash,
    load_json,
    read_csv,
    sha256_file,
    source_provenance,
    utc_now_iso,
)


def _update_run_manifest(run_dir: Path, *, stage: str, status: str) -> None:
    path = run_dir / "run_manifest.json"
    manifest = load_json(path)
    manifest.setdefault("stage_status", {})[stage] = status
    provenance = {
        "recorded_at_utc": utc_now_iso(),
        **source_provenance(ROOT),
    }
    previous = manifest.setdefault("stage_provenance", {}).get(stage)
    if previous:
        history = manifest.setdefault("stage_provenance_history", {}).setdefault(
            stage, []
        )
        if not history or history[-1] != previous:
            history.append(previous)
    manifest["stage_provenance"][stage] = provenance
    manifest["updated_at_utc"] = utc_now_iso()
    atomic_write_json(path, manifest, overwrite=True)


def validate_run(run_dir: str | Path, config: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    run_dir = Path(run_dir).resolve()
    path = run_dir / "run_manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"ClinicalTrials API run manifest not found: {path}")
    manifest = load_json(path)
    if manifest.get("run_id") != run_dir.name:
        raise ValueError("Run directory name does not match run manifest.")
    run_format_version = manifest.get("run_format_version")
    if run_format_version != RUN_FORMAT_VERSION:
        raise ValueError(
            f"Run format version {run_format_version!r} is a legacy immutable run; "
            f"this release transforms only version {RUN_FORMAT_VERSION} runs. "
            "Use status for read-only inspection."
        )
    if manifest.get("config_hash") != config_hash(config):
        raise ValueError("Current config.json does not match the run's frozen config hash.")
    snapshot_path = run_dir / "config_snapshot.json"
    if not snapshot_path.is_file() or sha256_file(snapshot_path) != manifest.get(
        "input_files", {}
    ).get("config_snapshot.json"):
        raise ValueError("Run config_snapshot.json is missing or changed.")
    trials_path = run_dir / "input" / "trials.csv"
    links_path = run_dir / "input" / "publication_trial_links.csv"
    if sha256_file(trials_path) != manifest.get("input_files", {}).get("trials.csv"):
        raise ValueError("Run trials.csv hash mismatch.")
    if sha256_file(links_path) != manifest.get("input_files", {}).get(
        "publication_trial_links.csv"
    ):
        raise ValueError("Run publication_trial_links.csv hash mismatch.")
    trials = read_csv(trials_path)
    ids = sorted(row["nct_id"] for row in trials)
    if len(ids) != manifest.get("trial_count") or canonical_json_hash(ids) != manifest.get(
        "cohort_hash"
    ):
        raise ValueError("Run cohort identity does not match the manifest.")
    run_type = manifest.get("run_type", "CLASSIFIER_COHORT")
    if run_type not in {"CLASSIFIER_COHORT", "AD_HOC_SINGLE_TRIAL"}:
        raise ValueError(f"Unsupported ClinicalTrials API run type: {run_type!r}")
    if run_type == "AD_HOC_SINGLE_TRIAL":
        if len(trials) != 1 or trials[0]["cohort_source"] != "AD_HOC_SINGLE_TRIAL":
            raise ValueError("Ad hoc run must contain exactly one ad hoc source NCT ID.")
        if ids != [manifest.get("requested_nct_id")]:
            raise ValueError("Ad hoc requested NCT ID does not match frozen input.")
        if (read_csv(links_path) or trials[0]["earliest_linked_publication_date"]
                or trials[0]["classifier_run_id"] or trials[0]["linked_publication_count"] != "0"):
            raise ValueError("Ad hoc run cannot contain classifier publication links.")
    return run_dir, manifest


def _response_record(response: JsonResponse, output: Path) -> dict[str, Any]:
    return {
        "url": response.url,
        "method": "GET",
        "status": response.status,
        "content_type": response.headers.get("content-type", ""),
        "etag": response.headers.get("etag", ""),
        "last_modified": response.headers.get("last-modified", ""),
        "retrieved_at_utc": utc_now_iso(),
        "attempts": response.attempts,
        "file": output.name,
        "sha256": sha256_file(output),
        "bytes": output.stat().st_size,
    }


def _save_response(response: JsonResponse, output: Path) -> dict[str, Any]:
    atomic_write_bytes(output, response.content)
    return _response_record(response, output)


def _reuse_response(output: Path, record: dict[str, Any] | None) -> Any:
    if not output.exists():
        return None
    if not record:
        raise ValueError(f"Raw file exists without a retrieval-manifest record: {output}")
    if sha256_file(output) != record.get("sha256"):
        raise ValueError(f"Raw response hash mismatch: {output}")
    return json.loads(output.read_text(encoding="utf-8-sig"))


def _checkpoint(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at_utc"] = utc_now_iso()
    atomic_write_json(path, manifest, overwrite=path.exists())


def _record_http_failure(trial_dir: Path, retrieval: dict[str, Any], exc: Exception) -> None:
    if not isinstance(exc, HttpRequestError) or exc.content is None:
        return
    path = trial_dir / f"failed_http_{uuid4().hex}.bin"
    atomic_write_bytes(path, exc.content)
    retrieval.setdefault("failed_responses", []).append({
        "url": exc.url,
        "status": exc.status,
        "content_type": exc.headers.get("content-type", ""),
        "retrieved_at_utc": utc_now_iso(),
        "file": path.name,
        "sha256": sha256_file(path),
    })


def download_histories(
    *,
    run_dir: str | Path,
    config: dict[str, Any],
    selected_nct_ids: list[str] | None = None,
    client: ClinicalTrialsClient | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    run_dir, run_manifest = validate_run(run_dir, config)
    lock_path = run_dir / "download.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(
            f"Another download may be active for this run. Refusing concurrent access: {lock_path}"
        ) from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"pid": os.getpid(), "created_at_utc": utc_now_iso()},
                ensure_ascii=False,
            )
            + "\n"
        )
    try:
        return _download_histories_locked(
            run_dir=run_dir,
            run_manifest=run_manifest,
            config=config,
            selected_nct_ids=selected_nct_ids,
            client=client,
            progress=progress,
        )
    finally:
        lock_path.unlink(missing_ok=True)


def _download_histories_locked(
    *,
    run_dir: Path,
    run_manifest: dict[str, Any],
    config: dict[str, Any],
    selected_nct_ids: list[str] | None,
    client: ClinicalTrialsClient | None,
    progress: Callable[[int, int], None] | None,
) -> dict[str, int]:
    client = client or ClinicalTrialsClient(config["clinicaltrials_gov"])
    all_trials = read_csv(run_dir / "input" / "trials.csv")
    allowed = set(run_manifest["cohort_nct_ids"])
    selected = set(selected_nct_ids or allowed)
    unknown = selected - allowed
    if unknown:
        raise ValueError(f"Requested NCT IDs are not in this run: {sorted(unknown)}")
    trials = [row for row in all_trials if row["nct_id"] in selected]

    raw_root = run_dir / "raw" / "clinicaltrials_gov"
    raw_root.mkdir(parents=True, exist_ok=True)
    api_version_path = raw_root / "api_version.json"
    api_meta_path = raw_root / "api_version.meta.json"
    if api_version_path.exists():
        api_record = load_json(api_meta_path) if api_meta_path.exists() else None
        api_data = _reuse_response(api_version_path, api_record)
    else:
        response = client.api_version()
        validate_api_version(response.data)
        api_record = _save_response(response, api_version_path)
        atomic_write_json(api_meta_path, api_record)
        api_data = response.data
    validate_api_version(api_data)

    def load_retrieval(nct_id: str) -> tuple[Path, Path, dict[str, Any], bool]:
        trial_dir = raw_root / nct_id
        trial_dir.mkdir(parents=True, exist_ok=True)
        retrieval_path = trial_dir / "retrieval_manifest.json"
        created = not retrieval_path.exists()
        retrieval = (
            load_json(retrieval_path)
            if retrieval_path.exists()
            else {
                "retrieval_format_version": 1,
                "nct_id": nct_id,
                "created_at_utc": utc_now_iso(),
                "api_version": api_data,
                "validation_status": "PENDING",
                "files": {},
                "errors": [],
            }
        )
        return trial_dir, retrieval_path, retrieval, created

    def ensure_current(
        nct_id: str, *, require_self_canonical: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        trial_dir, retrieval_path, retrieval, created = load_retrieval(nct_id)
        current_path = trial_dir / "current.json"
        if retrieval.get("validation_status") == "NOT_FOUND":
            current_record = retrieval.get("files", {}).get("current")
            if not current_record or not current_path.exists() or sha256_file(
                current_path
            ) != current_record.get("sha256"):
                raise ValueError(f"Saved not-found response is missing or changed for {nct_id}.")
            return retrieval, None

        current_data = _reuse_response(
            current_path, retrieval.get("files", {}).get("current")
        )
        fetched = current_data is None
        if fetched:
            response = client.current_study(nct_id)
            retrieval.setdefault("files", {})["current"] = _save_response(
                response, current_path
            )
            if response.status == 404:
                retrieval["validation_status"] = "NOT_FOUND"
                retrieval["retrieval_status"] = "NOT_FOUND"
                retrieval["not_found_response"] = {
                    "url": response.url,
                    "status": response.status,
                    "retrieved_at_utc": utc_now_iso(),
                }
                _checkpoint(retrieval_path, retrieval)
                return retrieval, None
            current_data = response.data
            # Preserve the response and its hash before any identity interpretation.
            _checkpoint(retrieval_path, retrieval)

        identity = resolve_current_identity(current_data, nct_id)
        if require_self_canonical and identity["canonical_nct_id"] != nct_id:
            raise ValueError(
                f"Canonical history request for {nct_id} redirected to "
                f"{identity['canonical_nct_id']}."
            )
        retrieval["validation_status"] = "FOUND"
        persist_identity = fetched or created or identity["identity_status"] == "OFFICIAL_ALIAS"
        if persist_identity:
            retrieval.update(identity)
            retrieval["identity_resolved_at_utc"] = utc_now_iso()
            retrieval["history_storage_nct_id"] = identity["canonical_nct_id"]
            _checkpoint(retrieval_path, retrieval)
        return retrieval, identity

    def ensure_canonical_history(canonical_nct_id: str) -> tuple[dict[str, Any], list[int]]:
        trial_dir, retrieval_path, retrieval, created = load_retrieval(canonical_nct_id)
        retrieval, identity = ensure_current(
            canonical_nct_id, require_self_canonical=True
        )
        if identity is None:
            raise ValueError(
                f"Canonical NCT record {canonical_nct_id} was unexpectedly not found."
            )
        was_completed = retrieval.get("retrieval_status") == "COMPLETED"
        index_path = trial_dir / "history_index.json"
        index_data = _reuse_response(
            index_path, retrieval.get("files", {}).get("history_index")
        )
        if index_data is None:
            response = client.history_index(canonical_nct_id)
            versions = validate_history_index(response.data, canonical_nct_id)
            retrieval.setdefault("files", {})["history_index"] = _save_response(
                response, index_path
            )
            index_data = response.data
            retrieval["expected_versions"] = versions
            _checkpoint(retrieval_path, retrieval)
        versions = validate_history_index(index_data, canonical_nct_id)
        if retrieval.get("expected_versions") not in (None, versions):
            raise ValueError(
                f"History version set changed within the run for {canonical_nct_id}."
            )
        retrieval["expected_versions"] = versions

        if progress:
            progress(0, len(versions))

        for completed_count, version in enumerate(versions, 1):
            key = f"version_{version:04d}"
            version_path = trial_dir / f"{key}.json"
            version_data = _reuse_response(
                version_path, retrieval.get("files", {}).get(key)
            )
            if version_data is None:
                response = client.history_version(canonical_nct_id, version)
                validate_history_version(response.data, canonical_nct_id, version)
                retrieval.setdefault("files", {})[key] = _save_response(
                    response, version_path
                )
                version_data = response.data
                _checkpoint(retrieval_path, retrieval)
            validate_history_version(version_data, canonical_nct_id, version)
            if progress:
                progress(completed_count, len(versions))

        retrieved_versions = sorted(
            int(key.removeprefix("version_"))
            for key in retrieval.get("files", {})
            if key.startswith("version_")
        )
        if retrieved_versions != versions:
            raise ValueError(
                f"Archived versions do not reconcile for {canonical_nct_id}."
            )
        retrieval["validation_status"] = "FOUND"
        retrieval["retrieval_status"] = "COMPLETED"
        retrieval["history_storage_nct_id"] = canonical_nct_id
        if created or not was_completed:
            retrieval["completed_at_utc"] = utc_now_iso()
            _checkpoint(retrieval_path, retrieval)
        return retrieval, versions

    counters = {
        "trials": len(trials),
        "canonical_trials": 0,
        "official_aliases": 0,
        "completed": 0,
        "completed_canonical_trials": 0,
        "not_found": 0,
        "errors": 0,
        "versions": 0,
    }
    _update_run_manifest(run_dir, stage="download", status="IN_PROGRESS")
    canonical_groups: dict[str, list[str]] = {}

    for trial in trials:
        source_nct_id = trial["nct_id"]
        try:
            retrieval, identity = ensure_current(source_nct_id)
            if identity is None:
                counters["not_found"] += 1
                continue
            canonical_nct_id = identity["canonical_nct_id"]
            canonical_groups.setdefault(canonical_nct_id, []).append(source_nct_id)
            if identity["identity_status"] == "OFFICIAL_ALIAS":
                counters["official_aliases"] += 1
        except (HttpRequestError, ValueError, OSError, json.JSONDecodeError) as exc:
            trial_dir, retrieval_path, retrieval, _ = load_retrieval(source_nct_id)
            _record_http_failure(trial_dir, retrieval, exc)
            retrieval["retrieval_status"] = "ERROR"
            retrieval.setdefault("errors", []).append(
                {
                    "at_utc": utc_now_iso(),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            _checkpoint(retrieval_path, retrieval)
            counters["errors"] += 1

    counters["canonical_trials"] = len(canonical_groups)
    for canonical_nct_id, source_nct_ids in sorted(canonical_groups.items()):
        try:
            _, versions = ensure_canonical_history(canonical_nct_id)
            counters["completed_canonical_trials"] += 1
            counters["versions"] += len(versions)
            for source_nct_id in source_nct_ids:
                if source_nct_id == canonical_nct_id:
                    counters["completed"] += 1
                    continue
                _, retrieval_path, retrieval, _ = load_retrieval(source_nct_id)
                retrieval["validation_status"] = "FOUND"
                retrieval["retrieval_status"] = "COMPLETED"
                retrieval["history_storage_nct_id"] = canonical_nct_id
                retrieval["expected_versions"] = versions
                retrieval["completed_at_utc"] = utc_now_iso()
                _checkpoint(retrieval_path, retrieval)
                counters["completed"] += 1
        except (HttpRequestError, ValueError, OSError, json.JSONDecodeError) as exc:
            canonical_dir, canonical_path, canonical_retrieval, _ = load_retrieval(canonical_nct_id)
            _record_http_failure(canonical_dir, canonical_retrieval, exc)
            canonical_retrieval["retrieval_status"] = "ERROR"
            canonical_retrieval.setdefault("errors", []).append(
                {
                    "at_utc": utc_now_iso(),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            _checkpoint(canonical_path, canonical_retrieval)
            for source_nct_id in source_nct_ids:
                if source_nct_id != canonical_nct_id:
                    _, source_path, source_retrieval, _ = load_retrieval(source_nct_id)
                    source_retrieval["retrieval_status"] = "ERROR"
                    source_retrieval.setdefault("errors", []).append(
                        {
                            "at_utc": utc_now_iso(),
                            "error_type": type(exc).__name__,
                            "message": (
                                f"Canonical history retrieval for {canonical_nct_id} failed: {exc}"
                            ),
                        }
                    )
                    _checkpoint(source_path, source_retrieval)
                counters["errors"] += 1

    identity_rows = identity_rows_from_run(run_dir)
    identity_path = raw_root / "identity_resolutions.csv"
    atomic_write_csv(
        identity_path,
        identity_rows,
        IDENTITY_RESOLUTION_COLUMNS,
        overwrite=identity_path.exists(),
    )
    manifest_path = run_dir / "run_manifest.json"
    manifest = load_json(manifest_path)
    resolved_rows = [
        row
        for row in identity_rows
        if row["retrieval_status"] in {"COMPLETED", "NOT_FOUND"}
    ]
    manifest["identity_resolution"] = {
        "source_nct_id_count": len(identity_rows),
        "resolved_source_nct_id_count": len(resolved_rows),
        "canonical_trial_count": len(
            {row["canonical_nct_id"] for row in resolved_rows}
        ),
        "official_alias_count": sum(
            row["identity_status"] == "OFFICIAL_ALIAS" for row in resolved_rows
        ),
        "file": str(identity_path.relative_to(run_dir)).replace("\\", "/"),
        "sha256": sha256_file(identity_path),
    }
    atomic_write_json(manifest_path, manifest, overwrite=True)

    all_statuses = {row["retrieval_status"] for row in identity_rows}
    if all_statuses <= {"COMPLETED", "NOT_FOUND"}:
        final_status = "COMPLETED"
    elif selected == allowed:
        final_status = "COMPLETED_WITH_ERRORS"
    else:
        final_status = "PARTIAL"
    _update_run_manifest(run_dir, stage="download", status=final_status)
    return counters


def check_external_interfaces(config: dict[str, Any], *, probe_nct_id: str) -> dict[str, Any]:
    client = ClinicalTrialsClient(config["clinicaltrials_gov"])
    version = client.api_version()
    validate_api_version(version.data)
    current = client.current_study(probe_nct_id)
    if current.status == 404:
        raise ValueError(f"Schema-probe trial was not found: {probe_nct_id}")
    validate_current_study(current.data, probe_nct_id)
    index = client.history_index(probe_nct_id)
    versions = validate_history_index(index.data, probe_nct_id)
    return {
        "api_version": version.data["apiVersion"],
        "data_timestamp": version.data["dataTimestamp"],
        "probe_nct_id": probe_nct_id,
        "probe_version_count": len(versions),
        "probe_versions": versions,
        "history_top_level_keys": sorted(index.data.keys()),
        "history_keys": sorted(index.data["history"].keys()),
    }
