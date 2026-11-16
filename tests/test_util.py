from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clinicaltrials_api.compare import _change_type, _unordered_equal  # noqa: E402
from clinicaltrials_api.util import date_precision, normalize_nct_id, path_get  # noqa: E402


class UtilityTests(unittest.TestCase):
    def test_normalizes_nct_identifier_conservatively(self) -> None:
        self.assertEqual(normalize_nct_id(" nct-00000002 "), "NCT00000002")
        with self.assertRaises(ValueError):
            normalize_nct_id("ISRCTN123")

    def test_distinguishes_absent_empty_and_value(self) -> None:
        document = {"a": {"empty": "", "value": 0}}
        self.assertEqual(path_get(document, "a.missing"), ("ABSENT", None))
        self.assertEqual(path_get(document, "a.empty"), ("PRESENT_EMPTY", ""))
        self.assertEqual(path_get(document, "a.value"), ("PRESENT_VALUE", 0))

    def test_change_types_and_reordering(self) -> None:
        self.assertEqual(_change_type("ABSENT", "PRESENT_VALUE", None, "x"), "ADDED")
        self.assertEqual(_change_type("PRESENT_VALUE", "ABSENT", "x", None), "REMOVED")
        self.assertTrue(_unordered_equal(["A", "B"], ["B", "A"]))
        self.assertFalse(_unordered_equal(["A", "B"], ["A", "C"]))

    def test_date_precision_is_preserved(self) -> None:
        self.assertEqual(date_precision("2025"), "YEAR")
        self.assertEqual(date_precision("2025-07"), "MONTH")
        self.assertEqual(date_precision("2025-07-01"), "DAY")


if __name__ == "__main__":
    unittest.main()
