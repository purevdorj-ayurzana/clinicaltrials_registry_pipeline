from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clinicaltrials_api.cohort import (  # noqa: E402
    CORRECTION_COLUMNS,
    NCT_ASSOCIATION_COLUMNS,
    PUBLICATION_COLUMNS,
    TRIAL_COLUMNS,
    ingest_classifier_export,
    verify_classifier_export,
)
from clinicaltrials_api.cli import main  # noqa: E402
from clinicaltrials_api.config import load_config  # noqa: E402
from clinicaltrials_api.util import (  # noqa: E402
    canonical_json_hash,
    load_json,
    read_csv,
    sha256_file,
)


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


class CohortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.export = self.root / "export"
        self.export.mkdir()
        self.publication = {column: "" for column in PUBLICATION_COLUMNS}
        self.publication.update(
            {
                "pmid": "101",
                "title": "Trial report",
                "publication_date": "2025-01-01",
                "classifier_run_id": "classifier_run",
                "classifier_model": "gpt-test",
                "classifier_reasoning_effort": "medium",
                "classifier_label": "PRIMARY_RCT_REPORT",
                "nct_abstract_regex_found": "TRUE",
                "nct_abstract_registry_numbers": "NCT00000001",
            }
        )
        self.association = {column: "" for column in NCT_ASSOCIATION_COLUMNS}
        self.association.update(
            {
                "pmid": "101",
                "nct_id_raw": "NCT00000001",
                "nct_id_normalized": "NCT00000001",
                "classifier_run_id": "classifier_run",
                "classifier_label": "PRIMARY_RCT_REPORT",
                "nct_extraction_method": "ABSTRACT_REGEX",
                "nct_source_field": "abstract",
            }
        )
        self._write_contract(
            export_format_version=1,
            classifier_arm="terra",
            selection_rule="terra.final_label == PRIMARY_RCT_REPORT",
        )

    def _write_contract(
        self,
        *,
        export_format_version: int,
        classifier_arm: str,
        selection_rule: str,
        publication_arm: str | None = None,
        association_arm: str | None = None,
    ) -> None:
        self.publication["classifier_arm"] = publication_arm or classifier_arm
        self.association["classifier_arm"] = association_arm or classifier_arm
        write_csv(
            self.export / "selected_publications.csv",
            PUBLICATION_COLUMNS,
            [self.publication],
        )
        write_csv(
            self.export / "nct_candidates.csv",
            NCT_ASSOCIATION_COLUMNS,
            [self.association],
        )
        manifest = {
            "export_format_version": export_format_version,
            "export_type": "clinicaltrials_gov_history_cohort",
            "classifier_run_id": "classifier_run",
            "classifier_arm": classifier_arm,
            "selection_rule": selection_rule,
            "selected_publications": 1,
            "selected_publications_with_nct": 1,
            "selected_publications_without_nct": 0,
            "pmid_nct_associations": 1,
            "unique_candidate_nct_ids": 1,
            "selected_pmid_hash": canonical_json_hash(["101"]),
            "association_hash": canonical_json_hash(["101|NCT00000001"]),
            "unique_nct_id_hash": canonical_json_hash(["NCT00000001"]),
            "files": {
                "selected_publications.csv": {
                    "sha256": sha256_file(self.export / "selected_publications.csv"),
                    "rows": 1,
                    "columns": PUBLICATION_COLUMNS,
                },
                "nct_candidates.csv": {
                    "sha256": sha256_file(self.export / "nct_candidates.csv"),
                    "rows": 1,
                    "columns": NCT_ASSOCIATION_COLUMNS,
                },
            },
        }
        (self.export / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _write_version_3_contract(self) -> None:
        publication_one = dict(self.publication)
        publication_one["classifier_arm"] = "sol"
        publication_two = {column: "" for column in PUBLICATION_COLUMNS}
        publication_two.update(
            {
                "pmid": "102",
                "title": "Trial report with truncated registration",
                "abstract": "This trial was registered as NCT0000002.",
                "publication_date": "2025-02-01",
                "classifier_run_id": "classifier_run",
                "classifier_arm": "sol",
                "classifier_model": "gpt-test",
                "classifier_reasoning_effort": "medium",
                "classifier_label": "PRIMARY_RCT_REPORT",
                "nct_abstract_regex_found": "FALSE",
            }
        )
        regex_association = dict(self.association)
        regex_association["classifier_arm"] = "sol"
        manual_association = {column: "" for column in NCT_ASSOCIATION_COLUMNS}
        manual_association.update(
            {
                "pmid": "102",
                "nct_id_raw": "NCT0000002",
                "nct_id_normalized": "NCT00000002",
                "classifier_run_id": "classifier_run",
                "classifier_arm": "sol",
                "classifier_label": "PRIMARY_RCT_REPORT",
                "nct_extraction_method": "MANUAL_VERIFIED_CORRECTION",
                "nct_source_field": "abstract",
            }
        )
        correction = {
            "pmid": "102",
            "nct_id_source_text": "NCT0000002",
            "nct_id_normalized": "NCT00000002",
            "nct_source_field": "abstract",
            "correction_type": "TRUNCATED_NCT_IDENTIFIER",
            "correction_reason": "The source displays a truncated identifier.",
            "publication_evidence_url": "https://pubmed.ncbi.nlm.nih.gov/102/",
            "registry_evidence_url": "https://clinicaltrials.gov/study/NCT00000002",
            "verified_date": "2026-08-06",
        }
        publications = [publication_one, publication_two]
        associations = [regex_association, manual_association]
        corrections = [correction]
        write_csv(
            self.export / "selected_publications.csv",
            PUBLICATION_COLUMNS,
            publications,
        )
        write_csv(
            self.export / "nct_candidates.csv",
            NCT_ASSOCIATION_COLUMNS,
            associations,
        )
        write_csv(
            self.export / "nct_candidate_corrections.csv",
            CORRECTION_COLUMNS,
            corrections,
        )
        manifest = {
            "export_format_version": 3,
            "export_type": "clinicaltrials_gov_history_cohort",
            "classifier_run_id": "classifier_run",
            "classifier_arm": "sol",
            "selection_rule": "sol.final_label == PRIMARY_RCT_REPORT",
            "candidate_selection_rule": "abstract_regex OR manual_verified_correction",
            "selected_publications": 2,
            "selected_publications_with_nct": 2,
            "selected_publications_without_nct": 0,
            "selected_publications_with_abstract_regex_nct": 1,
            "selected_publications_with_manual_verified_correction": 1,
            "manual_verified_corrections": 1,
            "pmid_nct_associations": 2,
            "unique_candidate_nct_ids": 2,
            "selected_pmid_hash": canonical_json_hash(["101", "102"]),
            "association_hash": canonical_json_hash(
                ["101|NCT00000001", "102|NCT00000002"]
            ),
            "unique_nct_id_hash": canonical_json_hash(
                ["NCT00000001", "NCT00000002"]
            ),
            "correction_set_hash": canonical_json_hash(corrections),
            "base_export": {
                "export_format_version": 2,
                "classifier_run_id": "classifier_run",
                "manifest_sha256": "0" * 64,
                "selected_publications_sha256": sha256_file(
                    self.export / "selected_publications.csv"
                ),
                "nct_candidates_sha256": "0" * 64,
                "association_hash": canonical_json_hash(["101|NCT00000001"]),
                "unique_nct_id_hash": canonical_json_hash(["NCT00000001"]),
            },
            "files": {
                "selected_publications.csv": {
                    "sha256": sha256_file(self.export / "selected_publications.csv"),
                    "rows": 2,
                    "columns": PUBLICATION_COLUMNS,
                },
                "nct_candidates.csv": {
                    "sha256": sha256_file(self.export / "nct_candidates.csv"),
                    "rows": 2,
                    "columns": NCT_ASSOCIATION_COLUMNS,
                },
                "nct_candidate_corrections.csv": {
                    "sha256": sha256_file(
                        self.export / "nct_candidate_corrections.csv"
                    ),
                    "rows": 1,
                    "columns": CORRECTION_COLUMNS,
                },
            },
        }
        (self.export / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_verifies_and_ingests_version_1_terra_export(self) -> None:
        verified = verify_classifier_export(self.export)
        self.assertEqual(len(verified["associations"]), 1)
        run = ingest_classifier_export(
            export_dir=self.export,
            config=load_config(ROOT / "config.json"),
            output_root=self.root / "runs",
        )
        trials = read_csv(run / "input" / "trials.csv")
        self.assertEqual(list(trials[0]), TRIAL_COLUMNS)
        self.assertEqual({row["nct_id"] for row in trials}, {"NCT00000001"})
        self.assertEqual(trials[0]["cohort_source"], "CLASSIFIER_EXPORT")
        manifest = load_json(run / "run_manifest.json")
        self.assertEqual(manifest["cohort_nct_ids"], ["NCT00000001"])
        self.assertEqual(manifest["trial_count"], manifest["classifier_candidate_count"])
        self.assertEqual(manifest["classifier_export"]["export_format_version"], 1)
        self.assertEqual(manifest["classifier_export"]["classifier_arm"], "terra")
        self.assertNotIn("reference_nct_ids", manifest)
        self.assertTrue((run / "config_snapshot.json").is_file())

    def test_version_2_sol_export_passes_verify_check_and_ingest_commands(self) -> None:
        self._write_contract(
            export_format_version=2,
            classifier_arm="sol",
            selection_rule="sol.final_label == PRIMARY_RCT_REPORT",
        )

        verify_output = io.StringIO()
        with contextlib.redirect_stdout(verify_output):
            self.assertEqual(
                main(["verify-export", "--export-dir", str(self.export)]),
                0,
            )
        verified = json.loads(verify_output.getvalue())
        self.assertEqual(verified["export_format_version"], 2)
        self.assertEqual(verified["classifier_arm"], "sol")
        self.assertEqual(
            verified["selection_rule"],
            "sol.final_label == PRIMARY_RCT_REPORT",
        )

        with (
            mock.patch(
                "clinicaltrials_api.cli.check_external_interfaces",
                return_value={"probe_nct_id": "NCT00000001"},
            ) as checked,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(["check", "--export-dir", str(self.export)]),
                0,
            )
        checked.assert_called_once_with(
            load_config(ROOT / "config.json"),
            probe_nct_id="NCT00000001",
        )

        output_root = self.root / "sol-runs"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "ingest",
                        "--export-dir",
                        str(self.export),
                        "--output-root",
                        str(output_root),
                    ]
                ),
                0,
            )
        run_dirs = list(output_root.iterdir())
        self.assertEqual(len(run_dirs), 1)
        run_manifest = load_json(run_dirs[0] / "run_manifest.json")
        self.assertEqual(run_manifest["classifier_export"]["export_format_version"], 2)
        self.assertEqual(run_manifest["classifier_export"]["classifier_arm"], "sol")
        links = read_csv(run_dirs[0] / "input" / "publication_trial_links.csv")
        self.assertEqual({row["classifier_arm"] for row in links}, {"sol"})

    def test_version_3_curated_export_passes_verify_check_and_ingest(self) -> None:
        self._write_version_3_contract()

        verify_output = io.StringIO()
        with contextlib.redirect_stdout(verify_output):
            self.assertEqual(
                main(["verify-export", "--export-dir", str(self.export)]), 0
            )
        summary = json.loads(verify_output.getvalue())
        self.assertEqual(summary["export_format_version"], 3)
        self.assertEqual(summary["manual_verified_corrections"], 1)
        self.assertEqual(summary["unique_candidate_nct_ids"], 2)

        with (
            mock.patch(
                "clinicaltrials_api.cli.check_external_interfaces",
                return_value={"probe_nct_id": "NCT00000001"},
            ) as checked,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(["check", "--export-dir", str(self.export)]), 0
            )
        checked.assert_called_once_with(
            load_config(ROOT / "config.json"), probe_nct_id="NCT00000001"
        )

        run = ingest_classifier_export(
            export_dir=self.export,
            config=load_config(ROOT / "config.json"),
            output_root=self.root / "curated-runs",
        )
        self.assertTrue(
            (run / "input" / "upstream_classifier" / "nct_candidate_corrections.csv").is_file()
        )
        trials = read_csv(run / "input" / "trials.csv")
        self.assertEqual(
            {row["nct_id"] for row in trials},
            {"NCT00000001", "NCT00000002"},
        )
        links = read_csv(run / "input" / "publication_trial_links.csv")
        self.assertEqual(
            {row["nct_extraction_method"] for row in links},
            {"ABSTRACT_REGEX", "MANUAL_VERIFIED_CORRECTION"},
        )

    def test_version_3_rejects_manual_association_not_matching_ledger(self) -> None:
        self._write_version_3_contract()
        correction_path = self.export / "nct_candidate_corrections.csv"
        corrections = read_csv(correction_path)
        corrections[0]["nct_id_source_text"] = "registered"
        write_csv(correction_path, CORRECTION_COLUMNS, corrections)
        manifest_path = self.export / "manifest.json"
        manifest = load_json(manifest_path)
        manifest["files"]["nct_candidate_corrections.csv"]["sha256"] = sha256_file(
            correction_path
        )
        manifest["correction_set_hash"] = canonical_json_hash(corrections)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "manual associations do not match"):
            verify_classifier_export(self.export)

    def test_rejects_inconsistent_version_arm_and_selection_rule_combinations(self) -> None:
        cases = [
            (
                2,
                "terra",
                "terra.final_label == PRIMARY_RCT_REPORT",
                "requires classifier_arm 'sol'",
            ),
            (
                2,
                "sol",
                "terra.final_label == PRIMARY_RCT_REPORT",
                "requires selection_rule 'sol.final_label == PRIMARY_RCT_REPORT'",
            ),
            (
                1,
                "sol",
                "sol.final_label == PRIMARY_RCT_REPORT",
                "requires classifier_arm 'terra'",
            ),
        ]
        for version, arm, rule, message in cases:
            with self.subTest(version=version, arm=arm, rule=rule):
                self._write_contract(
                    export_format_version=version,
                    classifier_arm=arm,
                    selection_rule=rule,
                )
                with self.assertRaisesRegex(ValueError, message):
                    verify_classifier_export(self.export)

    def test_rejects_csv_arm_that_disagrees_with_version_2_manifest(self) -> None:
        self._write_contract(
            export_format_version=2,
            classifier_arm="sol",
            selection_rule="sol.final_label == PRIMARY_RCT_REPORT",
            publication_arm="terra",
        )
        with self.assertRaisesRegex(ValueError, "Selected publications.*'sol'"):
            verify_classifier_export(self.export)

        self._write_contract(
            export_format_version=2,
            classifier_arm="sol",
            selection_rule="sol.final_label == PRIMARY_RCT_REPORT",
            association_arm="terra",
        )
        with self.assertRaisesRegex(ValueError, "NCT associations.*'sol'"):
            verify_classifier_export(self.export)

    def test_rejects_unsupported_export_format_version(self) -> None:
        self._write_contract(
            export_format_version=4,
            classifier_arm="sol",
            selection_rule="sol.final_label == PRIMARY_RCT_REPORT",
        )
        with self.assertRaisesRegex(ValueError, "Unsupported classifier export format version"):
            verify_classifier_export(self.export)

    def test_rejects_changed_export_file(self) -> None:
        with (self.export / "nct_candidates.csv").open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            verify_classifier_export(self.export)


if __name__ == "__main__":
    unittest.main()
