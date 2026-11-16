from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .util import canonical_json_hash, load_json


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config.json"
PROJECT_SCHEMA_VERSION = 3
RUN_FORMAT_VERSION = 2


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config = load_json(path)
    deprecated = {"pilot_output_root", "reference_nct_ids"} & set(config)
    if deprecated:
        raise ValueError(f"Configuration contains retired keys: {sorted(deprecated)}")
    required = {
        "project_name",
        "project_schema_version",
        "run_output_root",
        "clinicaltrials_gov",
        "scalar_fields",
    }
    missing = required - set(config)
    if missing:
        raise ValueError(f"Configuration is missing keys: {sorted(missing)}")
    if config["project_schema_version"] != PROJECT_SCHEMA_VERSION:
        raise ValueError(
            f"Configuration schema version must be {PROJECT_SCHEMA_VERSION}; "
            f"found {config['project_schema_version']}."
        )
    labels = [field.get("label") for field in config["scalar_fields"]]
    paths = [field.get("path") for field in config["scalar_fields"]]
    if any(not value for value in labels + paths):
        raise ValueError("Each scalar field requires a non-empty label and path.")
    if len(labels) != len(set(labels)) or len(paths) != len(set(paths)):
        raise ValueError("Scalar field labels and paths must be unique.")
    return config


def config_hash(config: dict[str, Any]) -> str:
    return canonical_json_hash(config)


def assert_python_version() -> None:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(
            f"ClinicalTrials API requires Python 3.12; active version is "
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}."
        )
