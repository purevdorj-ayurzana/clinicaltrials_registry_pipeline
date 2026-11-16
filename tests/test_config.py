from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clinicaltrials_api.config import load_config  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_rejects_retired_reference_configuration(self) -> None:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary_directory:
            for key, value in (
                ("reference_nct_ids", ["NCT00000002"]),
                ("pilot_output_root", "outputs/pilot_runs"),
            ):
                with self.subTest(key=key):
                    changed = dict(config)
                    changed[key] = value
                    path = Path(temporary_directory) / f"{key}.json"
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "retired keys"):
                        load_config(path)


if __name__ == "__main__":
    unittest.main()
