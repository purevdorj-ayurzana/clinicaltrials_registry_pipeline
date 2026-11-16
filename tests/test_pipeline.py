from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clinicaltrials_api.compare import (  # noqa: E402
    _arms_interventions_subtype,
    _date_relation_to_first_submission,
    _primary_outcome_subtype,
    CHANGE_SUBTYPE_CODES,
    MAIN_REVIEW_FLAG_CODES,
    compare_run,
    is_arms_interventions_change,
    is_arms_interventions_flaggable_change,
    is_primary_outcome_change,
    is_primary_outcome_flaggable_change,
)
from clinicaltrials_api.config import RUN_FORMAT_VERSION, config_hash, load_config  # noqa: E402
from clinicaltrials_api.client import JsonResponse  # noqa: E402
from clinicaltrials_api.download import download_histories  # noqa: E402
from clinicaltrials_api.extract import extract_run  # noqa: E402
from clinicaltrials_api.summarize import run_status, summarize_run  # noqa: E402
from clinicaltrials_api.util import (  # noqa: E402
    atomic_write_csv,
    canonical_json_hash,
    csv_columns,
    read_csv,
    sha256_file,
)


SYNTHETIC_NCT_ID = "NCT00000002"
ALIAS_NCT_ID = "NCT00000003"


class AliasClient:
    def __init__(self, history_payload: dict) -> None:
        self.history_payload = history_payload
        self.history_index_calls = 0
        self.history_version_calls = 0

    @staticmethod
    def _response(url: str, data: dict) -> JsonResponse:
        content = json.dumps(data).encode("utf-8")
        return JsonResponse(
            url=url,
            status=200,
            headers={"content-type": "application/json"},
            content=content,
            data=data,
            attempts=1,
        )

    def api_version(self) -> JsonResponse:
        return self._response(
            "https://example.test/version",
            {"apiVersion": "2.0", "dataTimestamp": "2026-01-01"},
        )

    def current_study(self, nct_id: str) -> JsonResponse:
        return self._response(
            f"https://example.test/studies/{nct_id}",
            {
                "protocolSection": {
                    "identificationModule": {
                        "nctId": SYNTHETIC_NCT_ID,
                        "nctIdAliases": [ALIAS_NCT_ID],
                    }
                }
            },
        )

    def history_index(self, nct_id: str) -> JsonResponse:
        self.history_index_calls += 1
        return self._response(
            f"https://example.test/studies/{nct_id}?history=true",
            {
                "study": {
                    "protocolSection": {
                        "identificationModule": {"nctId": SYNTHETIC_NCT_ID}
                    }
                },
                "history": {"changes": [{"version": 4, "date": "2020-01-01"}]},
            },
        )

    def history_version(self, nct_id: str, version: int) -> JsonResponse:
        self.history_version_calls += 1
        return self._response(
            f"https://example.test/studies/{nct_id}/history/{version}",
            self.history_payload,
        )


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "fixture_run"
        self.config = load_config(ROOT / "config.json")
        (self.run / "input").mkdir(parents=True)
        (self.run / "raw" / "clinicaltrials_gov" / SYNTHETIC_NCT_ID).mkdir(parents=True)
        trials = [
            {
                "nct_id": SYNTHETIC_NCT_ID,
                "cohort_source": "CLASSIFIER_EXPORT",
                "classifier_run_id": "synthetic_classifier_run",
                "linked_publication_count": 0,
                "earliest_linked_publication_date": "",
            }
        ]
        trial_columns = [
            "nct_id",
            "cohort_source",
            "classifier_run_id",
            "linked_publication_count",
            "earliest_linked_publication_date",
        ]
        atomic_write_csv(self.run / "input" / "trials.csv", trials, trial_columns)
        link_columns = [
            "pmid",
            "nct_id_raw",
            "nct_id_normalized",
            "classifier_run_id",
            "classifier_arm",
            "classifier_label",
            "nct_extraction_method",
            "nct_source_field",
            "title",
            "doi",
            "journal",
            "publication_year",
            "publication_date",
            "publication_role",
        ]
        atomic_write_csv(self.run / "input" / "publication_trial_links.csv", [], link_columns)
        (self.run / "config_snapshot.json").write_text(
            json.dumps(self.config, indent=2), encoding="utf-8"
        )
        fixture = ROOT / "tests" / "fixtures" / "synthetic_reversal"
        raw = self.run / "raw" / "clinicaltrials_gov" / SYNTHETIC_NCT_ID
        for filename in (
            "history_index.json",
            "version_0004.json",
            "version_0005.json",
            "version_0006.json",
        ):
            shutil.copy2(fixture / filename, raw / filename)
        retrieval = {
            "nct_id": SYNTHETIC_NCT_ID,
            "validation_status": "FOUND",
            "retrieval_status": "COMPLETED",
            "expected_versions": [4, 5, 6],
            "files": {},
            "errors": [],
        }
        (raw / "retrieval_manifest.json").write_text(json.dumps(retrieval), encoding="utf-8")
        manifest = {
            "run_format_version": RUN_FORMAT_VERSION,
            "run_id": "fixture_run",
            "config_hash": config_hash(self.config),
            "trial_count": 1,
            "classifier_candidate_count": 1,
            "cohort_nct_ids": [SYNTHETIC_NCT_ID],
            "cohort_hash": canonical_json_hash([SYNTHETIC_NCT_ID]),
            "input_files": {
                "config_snapshot.json": sha256_file(self.run / "config_snapshot.json"),
                "trials.csv": sha256_file(self.run / "input" / "trials.csv"),
                "publication_trial_links.csv": sha256_file(
                    self.run / "input" / "publication_trial_links.csv"
                ),
            },
            "stage_status": {
                "ingest": "COMPLETED",
                "download": "COMPLETED",
                "extract": "PENDING",
                "compare": "PENDING",
                "summarize": "PENDING",
            },
        }
        (self.run / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def configure_description_history(self, *, mixed_structural_changes: bool) -> None:
        raw = self.run / "raw" / "clinicaltrials_gov" / SYNTHETIC_NCT_ID
        old_payload = json.loads(
            (raw / "version_0004.json").read_text(encoding="utf-8")
        )
        new_payload = json.loads(json.dumps(old_payload))
        new_payload["studyVersion"] = 5
        protocol = new_payload["study"]["protocolSection"]
        status = protocol["statusModule"]
        status["lastUpdateSubmitDate"] = "2024-03-19"
        status["lastUpdatePostDateStruct"] = {
            "date": "2024-03-21",
            "type": "ACTUAL",
        }
        outcomes = protocol["outcomesModule"]["primaryOutcomes"]
        arms_module = protocol["armsInterventionsModule"]
        outcomes[0]["description"] = "Revised objective response description"
        arms_module["armGroups"][0]["description"] = "Revised treatment arm description"
        arms_module["interventions"][0]["description"] = "Revised treatment description"
        if mixed_structural_changes:
            outcomes[0]["timeFrame"] = "2 years"
            arms_module["armGroups"][0]["type"] = "ACTIVE_COMPARATOR"
            arms_module["interventions"].append(
                {
                    "type": "DRUG",
                    "name": "Drug C",
                    "description": "Additional treatment",
                    "armGroupLabels": ["Treatment"],
                }
            )
        (raw / "version_0005.json").write_text(
            json.dumps(new_payload), encoding="utf-8"
        )
        history = json.loads(
            (raw / "history_index.json").read_text(encoding="utf-8")
        )
        history["history"]["changes"] = history["history"]["changes"][:2]
        history["history"]["changes"][1].update(
            {"studyType": "INTERVENTIONAL", "date": "2024-03-19"}
        )
        (raw / "history_index.json").write_text(json.dumps(history), encoding="utf-8")
        retrieval_path = raw / "retrieval_manifest.json"
        retrieval = json.loads(retrieval_path.read_text(encoding="utf-8"))
        retrieval["expected_versions"] = [4, 5]
        retrieval_path.write_text(json.dumps(retrieval), encoding="utf-8")

    def configure_normalization_only_history(self) -> None:
        raw = self.run / "raw" / "clinicaltrials_gov" / SYNTHETIC_NCT_ID
        old_payload = json.loads((raw / "version_0004.json").read_text(encoding="utf-8"))
        new_payload = json.loads(json.dumps(old_payload))
        new_payload["studyVersion"] = 5
        protocol = new_payload["study"]["protocolSection"]
        status = protocol["statusModule"]
        status["lastUpdateSubmitDate"] = "2024-03-19"
        status["lastUpdatePostDateStruct"] = {
            "date": "2024-03-21",
            "type": "ACTUAL",
        }

        outcome = protocol["outcomesModule"]["primaryOutcomes"][0]
        outcome["measure"] = "  orr  "
        outcome["description"] = "  objective   RESPONSE  "
        outcome["timeFrame"] = "  1   YEAR  "

        arms_module = protocol["armsInterventionsModule"]
        arm = arms_module["armGroups"][0]
        arm["label"] = "  treatment  "
        arm["description"] = "  treatment   ARM  "
        arm["interventionNames"] = ["  drug   a  "]

        intervention = arms_module["interventions"][0]
        intervention["name"] = "  dRuG   A  "
        intervention["description"] = "  experimental   TREATMENT  "
        intervention["armGroupLabels"] = ["  treatment  "]

        (raw / "version_0005.json").write_text(
            json.dumps(new_payload), encoding="utf-8"
        )
        history = json.loads((raw / "history_index.json").read_text(encoding="utf-8"))
        history["history"]["changes"] = history["history"]["changes"][:2]
        history["history"]["changes"][1].update(
            {"studyType": "INTERVENTIONAL", "date": "2024-03-19"}
        )
        (raw / "history_index.json").write_text(json.dumps(history), encoding="utf-8")
        retrieval_path = raw / "retrieval_manifest.json"
        retrieval = json.loads(retrieval_path.read_text(encoding="utf-8"))
        retrieval["expected_versions"] = [4, 5]
        retrieval_path.write_text(json.dumps(retrieval), encoding="utf-8")

    def test_description_only_nested_changes_are_preserved_but_not_flagged(self) -> None:
        self.configure_description_history(mixed_structural_changes=False)
        extract_run(run_dir=self.run, config=self.config)
        compare_run(run_dir=self.run, config=self.config)

        nested = read_csv(self.run / "processed" / "nested_changes.csv")
        descriptions = [row for row in nested if row["field"] == "description"]
        self.assertEqual(
            {row["domain"] for row in descriptions},
            {"OUTCOME", "ARM", "INTERVENTION"},
        )
        self.assertEqual(len(descriptions), 3)
        parent_flags = [
            row
            for row in read_csv(self.run / "reports" / "review_flags.csv")
            if row["flag_code"]
            in {"PRIMARY_OUTCOME_CHANGED", "ARMS_INTERVENTIONS_CHANGED"}
        ]
        self.assertEqual(parent_flags, [])

    def test_mixed_description_and_structural_changes_flag_only_structure(self) -> None:
        self.configure_description_history(mixed_structural_changes=True)
        extract_run(run_dir=self.run, config=self.config)
        compare_run(run_dir=self.run, config=self.config)

        nested = read_csv(self.run / "processed" / "nested_changes.csv")
        self.assertEqual(len([row for row in nested if row["field"] == "description"]), 3)
        parent_flags = [
            row
            for row in read_csv(self.run / "reports" / "review_flags.csv")
            if row["flag_code"]
            in {"PRIMARY_OUTCOME_CHANGED", "ARMS_INTERVENTIONS_CHANGED"}
        ]
        self.assertEqual(len(parent_flags), 3)
        self.assertEqual(
            {row["change_subtype_code"] for row in parent_flags},
            {
                "PRIMARY_OUTCOME_TIME_FRAME_CHANGED",
                "ARM_TYPE_CHANGED",
                "INTERVENTION_ADDED",
            },
        )
        description_values = {
            value
            for row in nested
            if row["field"] == "description"
            for value in (row["old_value"], row["new_value"])
        }
        self.assertTrue(
            all(
                row["old_value"] not in description_values
                and row["new_value"] not in description_values
                for row in parent_flags
            )
        )

    def test_normalization_only_nested_changes_are_preserved_but_not_flagged(self) -> None:
        self.configure_normalization_only_history()
        extract_run(run_dir=self.run, config=self.config)
        compare_run(run_dir=self.run, config=self.config)

        nested = read_csv(self.run / "processed" / "nested_changes.csv")
        normalization_evidence = {
            (row["domain"], row["field"], row["change_type"])
            for row in nested
            if row["domain"] in {"OUTCOME", "ARM", "INTERVENTION"}
        }
        self.assertTrue(
            {
                ("OUTCOME", "measure", "MODIFIED"),
                ("OUTCOME", "description", "MODIFIED"),
                ("OUTCOME", "time_frame", "MODIFIED"),
                ("ARM", "arm_label", "MODIFIED"),
                ("ARM", "description", "MODIFIED"),
                ("ARM", "intervention_names", "REORDERED"),
                ("INTERVENTION", "intervention_name", "MODIFIED"),
                ("INTERVENTION", "description", "MODIFIED"),
                ("INTERVENTION", "arm_labels", "REORDERED"),
            }
            <= normalization_evidence
        )
        measure_change = next(
            row
            for row in nested
            if row["domain"] == "OUTCOME" and row["field"] == "measure"
        )
        self.assertEqual(measure_change["old_value"], "ORR")
        self.assertEqual(measure_change["new_value"], "  orr  ")
        parent_flags = [
            row
            for row in read_csv(self.run / "reports" / "review_flags.csv")
            if row["flag_code"]
            in {"PRIMARY_OUTCOME_CHANGED", "ARMS_INTERVENTIONS_CHANGED"}
        ]
        self.assertEqual(parent_flags, [])

    def test_extract_compare_and_summarize_synthetic_history(self) -> None:
        extracted = extract_run(run_dir=self.run, config=self.config)
        self.assertEqual(extracted["versions"], 3)
        snapshots = read_csv(self.run / "processed" / "version_snapshots.csv")
        self.assertEqual(
            [row["study_type"] for row in snapshots],
            ["INTERVENTIONAL", "OBSERVATIONAL", "INTERVENTIONAL"],
        )
        compared = compare_run(run_dir=self.run, config=self.config)
        self.assertGreater(compared["field_changes"], 0)
        flags = read_csv(self.run / "reports" / "review_flags.csv")
        codes = {row["flag_code"] for row in flags}
        self.assertNotIn("STUDY_TYPE_REVERSAL", codes)
        self.assertIn("STUDY_TYPE_CHANGED", codes)
        self.assertIn("PRIMARY_OUTCOME_CHANGED", codes)
        self.assertIn("ARMS_INTERVENTIONS_CHANGED", codes)
        self.assertIn("START_DATE_CROSSED_FIRST_SUBMISSION_BOUNDARY", codes)
        subtypes = {row["change_subtype_code"] for row in flags}
        self.assertIn("PRIMARY_OUTCOME_ADDED", subtypes)
        self.assertIn("PRIMARY_OUTCOME_REMOVED", subtypes)
        self.assertIn("ARM_ADDED", subtypes)
        self.assertIn("ARM_REMOVED", subtypes)
        self.assertIn("INTERVENTION_ADDED", subtypes)
        self.assertIn("INTERVENTION_REMOVED", subtypes)
        summarized = summarize_run(run_dir=self.run, config=self.config)
        self.assertEqual(summarized["trials"], 1)
        summary = read_csv(self.run / "reports" / "trial_summary.csv")
        self.assertEqual(summary[0]["version_count"], "3")
        self.assertNotIn("reference_case", summary[0])
        self.assertIn("PRIMARY_OUTCOME_ADDED", summary[0]["review_change_subtype_codes"])
        self.assertIn("INTERVENTION_ADDED", summary[0]["review_change_subtype_codes"])
        self.assertIn("INTERVENTION_REMOVED", summary[0]["review_change_subtype_codes"])
        manual = read_csv(self.run / "reports" / "manual_review.csv")
        self.assertTrue(all(row["review_status"] == "PENDING" for row in manual))
        self.assertTrue(all("reference_case" not in row for row in manual))
        self.assertTrue(all("change_subtype_code" in row for row in manual))

    def test_change_subtype_rules_and_partial_date_certainty(self) -> None:
        self.assertEqual(len(MAIN_REVIEW_FLAG_CODES), 11)
        self.assertEqual(len(CHANGE_SUBTYPE_CODES), 11)
        self.assertNotIn("STUDY_TYPE_REVERSAL", MAIN_REVIEW_FLAG_CODES)
        self.assertFalse(
            is_arms_interventions_change(
                {"domain": "INTERVENTION", "change_type": "REORDERED"}
            )
        )
        self.assertFalse(
            is_primary_outcome_change(
                {
                    "domain": "OUTCOME",
                    "item_key": "PRIMARY|overall survival",
                    "change_type": "REORDERED",
                }
            )
        )
        primary_description = {
            "domain": "OUTCOME",
            "item_key": "PRIMARY|overall survival",
            "field": "description",
            "change_type": "MODIFIED",
        }
        self.assertTrue(is_primary_outcome_change(primary_description))
        self.assertFalse(is_primary_outcome_flaggable_change(primary_description))
        self.assertFalse(
            is_primary_outcome_flaggable_change(
                {
                    **primary_description,
                    "field": "measure",
                    "old_value": "Overall Survival",
                    "new_value": "  overall   SURVIVAL ",
                }
            )
        )
        self.assertFalse(
            is_primary_outcome_flaggable_change(
                {
                    **primary_description,
                    "field": "time_frame",
                    "old_value": "12 Weeks",
                    "new_value": "  12   weeks ",
                }
            )
        )
        self.assertTrue(
            is_primary_outcome_flaggable_change(
                {
                    **primary_description,
                    "field": "time_frame",
                    "old_value": "12 weeks",
                    "new_value": "24 weeks",
                }
            )
        )
        for domain in ("ARM", "INTERVENTION"):
            description = {
                "domain": domain,
                "field": "description",
                "change_type": "MODIFIED",
            }
            self.assertTrue(is_arms_interventions_change(description))
            self.assertFalse(is_arms_interventions_flaggable_change(description))
            normalized_field = (
                "arm_label" if domain == "ARM" else "intervention_name"
            )
            self.assertFalse(
                is_arms_interventions_flaggable_change(
                    {
                        **description,
                        "field": normalized_field,
                        "old_value": "Treatment A",
                        "new_value": "  treatment   a ",
                    }
                )
            )
            self.assertTrue(
                is_arms_interventions_flaggable_change(
                    {**description, "field": "item", "change_type": "ADDED"}
                )
            )
        self.assertEqual(
            _primary_outcome_subtype(
                {
                    "domain": "OUTCOME",
                    "item_key": "PRIMARY|survival",
                    "field": "time_frame",
                    "change_type": "MODIFIED",
                }
            ),
            "PRIMARY_OUTCOME_TIME_FRAME_CHANGED",
        )
        self.assertEqual(
            _primary_outcome_subtype(
                {
                    "domain": "OUTCOME",
                    "field": "outcome_type",
                    "change_type": "RECLASSIFIED",
                    "old_value": "SECONDARY",
                    "new_value": "PRIMARY",
                }
            ),
            "OUTCOME_PROMOTED_TO_PRIMARY",
        )
        self.assertEqual(
            _primary_outcome_subtype(
                {
                    "domain": "OUTCOME",
                    "field": "outcome_type",
                    "change_type": "RECLASSIFIED",
                    "old_value": "PRIMARY",
                    "new_value": "SECONDARY",
                }
            ),
            "PRIMARY_OUTCOME_DEMOTED",
        )
        self.assertEqual(
            _date_relation_to_first_submission("2020", "2020-06-15"), "UNCERTAIN"
        )
        self.assertEqual(
            _date_relation_to_first_submission("2019", "2020-06-15"),
            "BEFORE_FIRST_SUBMISSION",
        )
        self.assertEqual(
            _date_relation_to_first_submission("2021", "2020-06-15"),
            "ON_OR_AFTER_FIRST_SUBMISSION",
        )

    def test_arm_type_and_confident_intervention_assignment_subtypes(self) -> None:
        type_change = {
            "domain": "ARM",
            "field": "arm_type",
            "change_type": "MODIFIED",
        }
        self.assertEqual(
            _arms_interventions_subtype(type_change, {}), "ARM_TYPE_CHANGED"
        )
        for change_type, expected in (
            ("ADDED", "INTERVENTION_ADDED"),
            ("REMOVED", "INTERVENTION_REMOVED"),
        ):
            self.assertEqual(
                _arms_interventions_subtype(
                    {
                        "domain": "INTERVENTION",
                        "field": "item",
                        "change_type": change_type,
                    },
                    {},
                ),
                expected,
            )
        arm_groups = {
            (SYNTHETIC_NCT_ID, "1"): [
                {
                    "arm_label": "Arm A",
                    "intervention_names": '["Drug: Drug A"]',
                },
                {"arm_label": "Arm B", "intervention_names": "[]"},
            ],
            (SYNTHETIC_NCT_ID, "2"): [
                {"arm_label": "Arm A", "intervention_names": "[]"},
                {
                    "arm_label": "Arm B",
                    "intervention_names": '["Drug: Drug A"]',
                },
            ],
        }
        assignment_change = {
            "nct_id": SYNTHETIC_NCT_ID,
            "from_version": "1",
            "to_version": "2",
            "domain": "INTERVENTION",
            "item_key": "DRUG|drug a",
            "field": "arm_labels",
            "change_type": "MODIFIED",
            "old_value": '["Arm A"]',
            "new_value": '["Arm B"]',
        }
        self.assertEqual(
            _arms_interventions_subtype(assignment_change, arm_groups),
            "INTERVENTION_ARM_ASSIGNMENT_CHANGED",
        )
        renamed_arms = {**arm_groups, (SYNTHETIC_NCT_ID, "2"): [
            {"arm_label": "Renamed arm", "intervention_names": '["Drug: Drug A"]'}
        ]}
        self.assertEqual(
            _arms_interventions_subtype(assignment_change, renamed_arms), ""
        )

    def test_derived_outputs_require_explicit_replace(self) -> None:
        extract_run(run_dir=self.run, config=self.config)
        with self.assertRaises(FileExistsError):
            extract_run(run_dir=self.run, config=self.config)

    def test_download_lock_refuses_concurrent_writer(self) -> None:
        (self.run / "download.lock").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Refusing concurrent access"):
            download_histories(run_dir=self.run, config=self.config)

    def test_official_aliases_share_one_canonical_history_and_analysis_row(self) -> None:
        raw_root = self.run / "raw" / "clinicaltrials_gov"
        shutil.rmtree(raw_root)
        raw_root.mkdir(parents=True)
        trial_columns = csv_columns(self.run / "input" / "trials.csv")
        trials = [
            {
                "nct_id": nct_id,
                "cohort_source": "CLASSIFIER_EXPORT",
                "classifier_run_id": "synthetic_classifier_run",
                "linked_publication_count": 1,
                "earliest_linked_publication_date": "2020-01-01",
            }
            for nct_id in (SYNTHETIC_NCT_ID, ALIAS_NCT_ID)
        ]
        atomic_write_csv(
            self.run / "input" / "trials.csv",
            trials,
            trial_columns,
            overwrite=True,
        )
        link_path = self.run / "input" / "publication_trial_links.csv"
        link_columns = csv_columns(link_path)
        links = []
        for nct_id in (SYNTHETIC_NCT_ID, ALIAS_NCT_ID):
            row = {column: "" for column in link_columns}
            row.update(
                {
                    "pmid": "123",
                    "nct_id_raw": nct_id,
                    "nct_id_normalized": nct_id,
                    "classifier_run_id": "synthetic_classifier_run",
                    "classifier_arm": "sol",
                    "classifier_label": "PRIMARY_RCT_REPORT",
                    "nct_extraction_method": "ABSTRACT_REGEX",
                    "nct_source_field": "abstract",
                    "title": "Synthetic publication",
                    "publication_date": "2020-01-01",
                }
            )
            links.append(row)
        atomic_write_csv(link_path, links, link_columns, overwrite=True)

        manifest_path = self.run / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ids = sorted([SYNTHETIC_NCT_ID, ALIAS_NCT_ID])
        manifest["trial_count"] = 2
        manifest["classifier_candidate_count"] = 2
        manifest["cohort_nct_ids"] = ids
        manifest["cohort_hash"] = canonical_json_hash(ids)
        manifest["input_files"]["trials.csv"] = sha256_file(
            self.run / "input" / "trials.csv"
        )
        manifest["input_files"]["publication_trial_links.csv"] = sha256_file(link_path)
        manifest["stage_status"]["download"] = "PENDING"
        manifest["stage_provenance"] = {
            "download": {
                "recorded_at_utc": "2025-01-01T00:00:00+00:00",
                "files": {},
                "bundle_hash": "old-bundle",
            }
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        history_payload = json.loads(
            (
                ROOT
                / "tests"
                / "fixtures"
                / "synthetic_reversal"
                / "version_0004.json"
            ).read_text(encoding="utf-8")
        )
        client = AliasClient(history_payload)
        result = download_histories(
            run_dir=self.run, config=self.config, client=client
        )
        self.assertEqual(result["trials"], 2)
        self.assertEqual(result["canonical_trials"], 1)
        self.assertEqual(result["official_aliases"], 1)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["completed_canonical_trials"], 1)
        self.assertEqual(client.history_index_calls, 1)
        self.assertEqual(client.history_version_calls, 1)
        completed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        history = completed_manifest["stage_provenance_history"]["download"]
        self.assertEqual(history[0]["bundle_hash"], "old-bundle")
        self.assertTrue(
            (raw_root / SYNTHETIC_NCT_ID / "version_0004.json").is_file()
        )
        self.assertFalse((raw_root / ALIAS_NCT_ID / "version_0004.json").exists())

        identities = read_csv(raw_root / "identity_resolutions.csv")
        alias = next(row for row in identities if row["source_nct_id"] == ALIAS_NCT_ID)
        self.assertEqual(alias["canonical_nct_id"], SYNTHETIC_NCT_ID)
        self.assertEqual(alias["identity_status"], "OFFICIAL_ALIAS")

        extracted = extract_run(run_dir=self.run, config=self.config)
        self.assertEqual(extracted["trials"], 1)
        canonical = read_csv(self.run / "processed" / "canonical_trials.csv")
        self.assertEqual(canonical[0]["nct_id"], SYNTHETIC_NCT_ID)
        self.assertEqual(canonical[0]["linked_publication_count"], "1")
        self.assertIn(ALIAS_NCT_ID, canonical[0]["cohort_alias_nct_ids"])
        compare_run(run_dir=self.run, config=self.config)
        summarized = summarize_run(run_dir=self.run, config=self.config)
        self.assertEqual(summarized["trials"], 1)
        summary = read_csv(self.run / "reports" / "trial_summary.csv")
        self.assertEqual(summary[0]["nct_id"], SYNTHETIC_NCT_ID)
        self.assertEqual(summary[0]["identity_status"], "OFFICIAL_ALIAS_RESOLVED")
        self.assertIn(ALIAS_NCT_ID, summary[0]["source_nct_ids"])

        restart_client = AliasClient(history_payload)
        restarted = download_histories(
            run_dir=self.run, config=self.config, client=restart_client
        )
        self.assertEqual(restarted["completed"], 2)
        self.assertEqual(restart_client.history_index_calls, 0)
        self.assertEqual(restart_client.history_version_calls, 0)

    def test_legacy_run_is_read_only(self) -> None:
        path = self.run / "run_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["run_format_version"] = 1
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(run_status(self.run)["run_id"], "fixture_run")
        with self.assertRaisesRegex(ValueError, "legacy immutable run"):
            extract_run(run_dir=self.run, config=self.config)


if __name__ == "__main__":
    unittest.main()
