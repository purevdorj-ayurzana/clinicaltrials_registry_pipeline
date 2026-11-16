from __future__ import annotations
import csv
import io
import json
import sys
import tarfile
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from study.release import compare_csv, read_csv, restore_archives, safe_member, verify_data, write_csv
from study.reproduction import reproduce
from study.semantic import execute_assessments, request_body
from study.semantic_candidates import parse_assessment
from study.event_timing import classify_two_window
from study.timing import classify

class ReleaseIntegrityTests(unittest.TestCase):
    def test_bundled_assets_and_cohort(self):
        self.assertEqual(verify_data(deep=False)["canonical_trials"],1758)

    def test_archive_traversal_and_links_rejected(self):
        for name in ["../escape","/absolute","C:/drive","a\\escape"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_member(tarfile.TarInfo(name))
        member=tarfile.TarInfo("link");member.type=tarfile.SYMTYPE
        with self.assertRaises(ValueError):safe_member(member)

    def test_archive_restoration_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bundle=root/"data/registry";bundle.mkdir(parents=True)
            with tarfile.open(bundle/"records_01.tar.xz","w:xz") as handle:
                member=tarfile.TarInfo("study.json");member.size=2;handle.addfile(member,io.BytesIO(b"{}"))
            output=root/"restored"
            with patch("study.release.ROOT",root):
                restore_archives(output)
                with self.assertRaises(ValueError):restore_archives(output)
            self.assertEqual((output/"study.json").read_bytes(),b"{}")

    def test_changed_asset_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/"data").mkdir();(root/"input.csv").write_text("changed")
            (root/"data/manifest.json").write_text(json.dumps({"files":{"input.csv":"0"*64}}))
            with patch("study.release.ROOT",root),self.assertRaisesRegex(ValueError,"changed study asset"):
                verify_data(deep=False)

    def test_scientific_table_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            a,b=Path(directory)/"actual.csv",Path(directory)/"reference.csv"
            write_csv(a,[{"nct_id":"NCT00000001","label":"NEGATIVE"}])
            write_csv(b,[{"nct_id":"NCT00000001","label":"POSITIVE"}])
            with self.assertRaisesRegex(ValueError,"Scientific value differs"):compare_csv(a,b)

    def test_numeric_publication_ids_and_counts_require_exact_agreement(self):
        with tempfile.TemporaryDirectory() as directory:
            a,b=Path(directory)/"actual.csv",Path(directory)/"reference.csv"
            for field,old,new in [("index_primary_publication_pmid","34108229","34108230"),
                                  ("count","1400000","1400001")]:
                write_csv(a,[{field:new}]);write_csv(b,[{field:old}])
                with self.subTest(field=field),self.assertRaisesRegex(ValueError,"Scientific value differs"):
                    compare_csv(a,b)

    def test_reproduction_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory);(output/"keep.txt").write_text("keep")
            with self.assertRaises(FileExistsError):reproduce(output)
            self.assertEqual((output/"keep.txt").read_text(),"keep")

    def test_missing_phase_retained_and_complete_case_counts(self):
        rows=[r for r in read_csv(ROOT/"results/trial_covariates.csv") if r["complete_case_eligible"]=="TRUE"]
        self.assertEqual(len(rows),1400)
        self.assertEqual(sum(r["endpoint_label"]=="POSITIVE" for r in rows),967)
        self.assertEqual(sum(r["phase_group"]=="MISSING" for r in rows),8)

    def test_workflow_does_not_require_external_executables(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)
            with patch("study.reproduction.verify_data", return_value={"status":"VERIFIED"}), \
                 patch("study.reproduction.restore_run", return_value=output/"registry"), \
                 patch("study.reproduction.prepare_tables"), \
                 patch("study.reproduction.compare_results"), \
                 patch("study.semantic.prepare_assessments"), \
                 patch("shutil.which", return_value=None), \
                 patch("subprocess.run", side_effect=AssertionError("Unexpected external process")):
                reproduce(output)


class SemanticAndTimingTests(unittest.TestCase):
    def setUp(self):
        self.input={"nct_id":"NCT00000001","states":[{"version":0,"outcomes":[]},{"version":3,"outcomes":[]}],
                    "reclassification_candidates":[]}

    def test_frozen_request_profile_and_dynamic_registry_versions(self):
        body=request_body(self.input)
        self.assertEqual(body["model"],"gpt-5.6-sol")
        self.assertEqual(body["reasoning"]["effort"],"medium")
        self.assertEqual(body["text"]["format"]["schema"]["properties"]["versions"]["items"]["enum"],["V0","V3"])
        self.assertEqual(body["instructions"],(ROOT/"prompts/semantic_assessment.txt").read_text(encoding="utf-8"))

    def test_false_assessment_requires_empty_diagnostics(self):
        value={"meaningful_change":False,"change_type":["addition"],"versions":[],"outcome_reclassifications":[]}
        with self.assertRaises(ValueError):parse_assessment(json.dumps(value),self.input)

    def test_promotion_requires_supplied_secondary_candidate(self):
        value={"meaningful_change":True,"change_type":["outcome promotion"],"versions":["V0","V3"],"outcome_reclassifications":["outcome promotion"]}
        with self.assertRaises(ValueError):parse_assessment(json.dumps(value),self.input)
        self.input["reclassification_candidates"]=[{"direction":"secondary_to_primary","from_version":2,"to_version":3}]
        value["versions"]=["V2","V3"]
        self.assertTrue(parse_assessment(json.dumps(value),self.input)["meaningful_change"])

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(ValueError):parse_assessment('{"meaningful_change":false,"meaningful_change":true}',self.input)

    def test_new_inference_requires_explicit_execution(self):
        with patch("urllib.request.urlopen") as network,self.assertRaises(ValueError):
            execute_assessments("absent","absent",expected_calls=0)
        network.assert_not_called()

    def test_partial_date_uncertainty_and_same_day_boundary(self):
        lower,upper=date(2020,5,1),date(2020,5,31)
        self.assertEqual(classify([lower],lower,upper)[0],"EARLY_ONLY")
        self.assertEqual(classify([date(2020,5,15)],lower,upper)[0],"TIMING_INDETERMINATE")
        self.assertEqual(classify([lower,date(2020,5,15),date(2020,6,1)],lower,upper)[0],"BOTH_PERIODS")


class PrimaryCompletionWindowTests(unittest.TestCase):
    def test_partial_date_boundary_classification_is_preserved(self):
        lower,upper=date(2020,5,1),date(2020,5,31)
        self.assertEqual(classify_two_window([],lower,upper),("NO_EVENT",""))
        self.assertEqual(classify_two_window([lower],lower,upper),("THROUGH_PRIMARY_COMPLETION",""))
        self.assertEqual(classify_two_window([date(2020,5,15)],lower,upper),("TIMING_INDETERMINATE","PRIMARY_COMPLETION"))
        self.assertEqual(classify_two_window([date(2020,6,1)],lower,upper),("AFTER_PRIMARY_COMPLETION",""))

if __name__=="__main__":unittest.main()
