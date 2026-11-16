from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clinicaltrials_api.client import (  # noqa: E402
    ClinicalTrialsClient,
    validate_current_study,
    validate_history_index,
    validate_history_version,
)


class FakeResponse:
    status = 200

    def __init__(self, data: dict, headers: dict[str, str]) -> None:
        self.content = json.dumps(data).encode("utf-8")
        self.headers = headers

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.content


class ClientTests(unittest.TestCase):
    def test_accepts_only_official_nct_alias_redirects(self) -> None:
        payload = {
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT00000001",
                    "nctIdAliases": ["NCT00000002"],
                }
            }
        }
        canonical = validate_current_study(payload, "NCT00000001")
        self.assertEqual(canonical["identity_status"], "CANONICAL")
        alias = validate_current_study(payload, "NCT00000002")
        self.assertEqual(alias["identity_status"], "OFFICIAL_ALIAS")
        self.assertEqual(alias["canonical_nct_id"], "NCT00000001")

        with self.assertRaisesRegex(ValueError, "not listed in nctIdAliases"):
            validate_current_study(payload, "NCT00000003")

    def test_content_type_header_is_case_insensitive(self) -> None:
        client = ClinicalTrialsClient(
            {
                "max_attempts": 1,
                "request_interval_seconds": 0,
                "timeout_seconds": 1,
                "user_agent": "test",
            },
            opener=lambda request, timeout: FakeResponse(
                {"apiVersion": "2.0", "dataTimestamp": "2026-01-01"},
                {"content-type": "application/json"},
            ),
        )
        response = client.get_json("https://example.test")
        self.assertEqual(response.data["apiVersion"], "2.0")

    def test_history_schema_validation(self) -> None:
        versions = validate_history_index(
            {
                "study": {},
                "history": {
                    "changes": [
                        {"version": 0},
                        {"version": 1},
                    ]
                },
            },
            "NCT00000001",
        )
        self.assertEqual(versions, [0, 1])
        validate_history_version(
            {
                "studyVersion": 0,
                "study": {
                    "protocolSection": {
                        "identificationModule": {"nctId": "NCT00000001"}
                    }
                },
            },
            "NCT00000001",
            0,
        )

    def test_parses_final_curl_header_block(self) -> None:
        status, headers = ClinicalTrialsClient._parse_curl_headers(
            "HTTP/1.1 200 Connection established\r\n\r\n"
            "HTTP/2 200\r\ncontent-type: application/json\r\netag: abc\r\n\r\n"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")


if __name__ == "__main__":
    unittest.main()
