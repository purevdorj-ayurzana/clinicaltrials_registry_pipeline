from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, Callable

from .identity import resolve_current_identity


@dataclass(frozen=True)
class JsonResponse:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes
    data: Any
    attempts: int


class HttpRequestError(RuntimeError):
    def __init__(
        self, message: str, *, url: str, status: int | None = None,
        content: bytes | None = None, headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.content = content
        self.headers = headers or {}


class _TransportError(RuntimeError):
    pass


class ClinicalTrialsClient:
    def __init__(
        self,
        settings: dict[str, Any],
        *,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._opener = opener or urllib.request.urlopen
        self._use_curl = opener is None and settings.get("transport") == "curl"
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        interval = float(self.settings.get("request_interval_seconds", 0.5))
        if self._last_request_at is None or interval <= 0:
            return
        remaining = interval - (self._monotonic() - self._last_request_at)
        if remaining > 0:
            self._sleep(remaining)

    @staticmethod
    def _headers_to_dict(headers: Message | Any) -> dict[str, str]:
        if hasattr(headers, "items"):
            return {str(key).casefold(): str(value) for key, value in headers.items()}
        return {}

    def _urllib_once(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener(request, timeout=timeout) as response:
                return (
                    int(getattr(response, "status", 200)),
                    self._headers_to_dict(response.headers),
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            return exc.code, self._headers_to_dict(exc.headers), exc.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise _TransportError(str(exc)) from exc

    @staticmethod
    def _parse_curl_headers(content: str) -> tuple[int, dict[str, str]]:
        lines = content.replace("\r\n", "\n").split("\n")
        starts = [index for index, line in enumerate(lines) if line.startswith("HTTP/")]
        if not starts:
            raise _TransportError("curl returned no HTTP status line.")
        start = starts[-1]
        parts = lines[start].split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise _TransportError(f"Invalid curl HTTP status line: {lines[start]!r}")
        response_headers: dict[str, str] = {}
        for line in lines[start + 1 :]:
            if not line:
                break
            if ":" in line:
                key, value = line.split(":", 1)
                response_headers[key.strip().casefold()] = value.strip()
        return int(parts[1]), response_headers

    def _curl_once(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        descriptor, header_name = tempfile.mkstemp(
            prefix="clinicaltrials_headers_", suffix=".txt"
        )
        os.close(descriptor)
        try:
            command = [
                str(self.settings.get("curl_path", "curl")),
                "--silent",
                "--show-error",
                "--location",
                "--max-time",
                str(timeout),
                "--request",
                "GET",
                "--header",
                f"Accept: {headers['Accept']}",
                "--user-agent",
                headers["User-Agent"],
                "--dump-header",
                header_name,
                "--output",
                "-",
                url,
            ]
            try:
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=timeout + 5,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise _TransportError(f"curl execution failed: {exc}") from exc
            if completed.returncode != 0:
                message = completed.stderr.decode("utf-8", errors="replace").strip()
                raise _TransportError(
                    f"curl exited with code {completed.returncode}: {message}"
                )
            header_text = Path(header_name).read_text(encoding="iso-8859-1")
            status, response_headers = self._parse_curl_headers(header_text)
            return status, response_headers, completed.stdout
        finally:
            Path(header_name).unlink(missing_ok=True)

    def get_json(self, url: str, *, allow_not_found: bool = False) -> JsonResponse:
        max_attempts = int(self.settings.get("max_attempts", 4))
        backoff = float(self.settings.get("backoff_initial_seconds", 1.0))
        timeout = float(self.settings.get("timeout_seconds", 30))
        headers = {
            "Accept": "application/json",
            "User-Agent": str(self.settings.get("user_agent", "ClinicalTrials-API-History/0.1")),
        }
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            self._throttle()
            try:
                if self._use_curl:
                    status, response_headers, content = self._curl_once(url, headers, timeout)
                else:
                    status, response_headers, content = self._urllib_once(url, headers, timeout)
                self._last_request_at = self._monotonic()
                if status == 404 and allow_not_found:
                    try:
                        data = json.loads(content.decode("utf-8-sig")) if content else {}
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        data = {}
                    return JsonResponse(
                        url=url,
                        status=404,
                        headers=response_headers,
                        content=content,
                        data=data,
                        attempts=attempt,
                    )
                if status >= 400:
                    retryable = status == 429 or 500 <= status <= 599
                    if not retryable or attempt == max_attempts:
                        raise HttpRequestError(
                            f"HTTP {status} for {url}", url=url, status=status,
                            content=content, headers=response_headers,
                        )
                    retry_after = response_headers.get("retry-after")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                    self._sleep(delay)
                    backoff *= 2
                    continue
                content_type = response_headers.get("content-type", "")
                if "json" not in content_type.casefold():
                    raise HttpRequestError(
                        f"Expected JSON but received Content-Type {content_type!r}.",
                        url=url, status=status, content=content, headers=response_headers,
                    )
                try:
                    data = json.loads(content.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HttpRequestError(
                        "Response was not valid UTF-8 JSON.", url=url, status=status,
                        content=content, headers=response_headers,
                    ) from exc
                return JsonResponse(
                    url=url,
                    status=status,
                    headers=response_headers,
                    content=content,
                    data=data,
                    attempts=attempt,
                )
            except _TransportError as exc:
                self._last_request_at = self._monotonic()
                last_error = exc
                if attempt == max_attempts:
                    break
                self._sleep(backoff)
                backoff *= 2
        raise HttpRequestError(f"Request failed for {url}: {last_error}", url=url)

    def api_version(self) -> JsonResponse:
        return self.get_json(self.settings["official_api_version_url"])

    def current_study(self, nct_id: str) -> JsonResponse:
        return self.get_json(
            self.settings["official_study_url"].format(nct_id=nct_id),
            allow_not_found=True,
        )

    def history_index(self, nct_id: str) -> JsonResponse:
        return self.get_json(self.settings["history_index_url"].format(nct_id=nct_id))

    def history_version(self, nct_id: str, version: int) -> JsonResponse:
        return self.get_json(
            self.settings["history_version_url"].format(nct_id=nct_id, version=version)
        )


def validate_api_version(data: Any) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("apiVersion"), str):
        raise ValueError("ClinicalTrials.gov API version response schema changed.")
    if not isinstance(data.get("dataTimestamp"), str):
        raise ValueError("ClinicalTrials.gov API version response lacks dataTimestamp.")


def validate_current_study(data: Any, nct_id: str) -> dict[str, Any]:
    return resolve_current_identity(data, nct_id)


def validate_history_index(data: Any, nct_id: str) -> list[int]:
    if not isinstance(data, dict) or not isinstance(data.get("study"), dict):
        raise ValueError(f"History index response has no study object for {nct_id}.")
    history = data.get("history")
    if not isinstance(history, dict) or not isinstance(history.get("changes"), list):
        raise ValueError(f"History index response schema changed for {nct_id}.")
    versions: list[int] = []
    for position, change in enumerate(history["changes"]):
        if not isinstance(change, dict) or not isinstance(change.get("version"), int):
            raise ValueError(f"Invalid history change at position {position} for {nct_id}.")
        versions.append(change["version"])
    if len(versions) != len(set(versions)) or versions != sorted(versions):
        raise ValueError(f"History versions are duplicated or not ordered for {nct_id}.")
    if versions and versions != list(range(versions[0], versions[-1] + 1)):
        raise ValueError(f"History versions are not contiguous for {nct_id}: {versions}")
    return versions


def validate_history_version(data: Any, nct_id: str, version: int) -> None:
    if not isinstance(data, dict) or data.get("studyVersion") != version:
        raise ValueError(f"Historical response version mismatch for {nct_id} version {version}.")
    study = data.get("study")
    if not isinstance(study, dict) or not isinstance(study.get("protocolSection"), dict):
        raise ValueError(f"Historical study response schema changed for {nct_id} version {version}.")
    actual = (
        study.get("protocolSection", {})
        .get("identificationModule", {})
        .get("nctId")
    )
    if actual != nct_id:
        raise ValueError(
            f"Historical response returned NCT ID {actual!r}, expected {nct_id}."
        )
