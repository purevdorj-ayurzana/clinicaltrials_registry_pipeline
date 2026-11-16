from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cohort import ingest_classifier_export, verify_classifier_export
from .compare import compare_run
from .config import assert_python_version, load_config
from .download import check_external_interfaces, download_histories
from .extract import extract_run
from .summarize import run_status, summarize_run
from .util import normalize_nct_id


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ClinicalTrials API",
        description="Auditable ClinicalTrials.gov record-history extraction and comparison.",
    )
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check", help="Check Python and current ClinicalTrials.gov response shapes."
    )
    check.add_argument("--export-dir", required=True, type=Path)

    ingest = subparsers.add_parser("ingest", help="Verify and import a classifier cohort export.")
    ingest.add_argument("--export-dir", required=True, type=Path)
    ingest.add_argument("--output-root", type=Path)

    verify = subparsers.add_parser("verify-export", help="Verify an upstream classifier export only.")
    verify.add_argument("--export-dir", required=True, type=Path)

    download = subparsers.add_parser("download", help="Download current and historical registry JSON.")
    download.add_argument("--run-dir", required=True, type=Path)
    download.add_argument("--nct-id", action="append", default=[])

    for command, help_text in (
        ("extract", "Extract normalized design-focused CSV tables."),
        ("compare", "Compare consecutive versions and create review flags."),
        ("summarize", "Create trial-level and manual-review CSVs."),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("--run-dir", required=True, type=Path)
        child.add_argument("--replace", action="store_true")

    status = subparsers.add_parser("status", help="Show run stage and output status.")
    status.add_argument("--run-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        assert_python_version()
        config = load_config(args.config)
        if args.command == "check":
            verified = verify_classifier_export(args.export_dir)
            probe_ids = sorted(
                {row["nct_id_normalized"] for row in verified["associations"]}
            )
            if not probe_ids:
                raise ValueError("The verified classifier export contains no candidate NCT IDs.")
            _print(
                {
                    "python": sys.version.split()[0],
                    **check_external_interfaces(config, probe_nct_id=probe_ids[0]),
                }
            )
        elif args.command == "verify-export":
            verified = verify_classifier_export(args.export_dir)
            manifest = verified["manifest"]
            summary = {
                "export_format_version": manifest["export_format_version"],
                "classifier_run_id": manifest["classifier_run_id"],
                "classifier_arm": manifest["classifier_arm"],
                "selection_rule": manifest["selection_rule"],
                "selected_publications": manifest["selected_publications"],
                "pmid_nct_associations": manifest["pmid_nct_associations"],
                "unique_candidate_nct_ids": manifest["unique_candidate_nct_ids"],
            }
            if manifest["export_format_version"] == 3:
                summary["manual_verified_corrections"] = manifest[
                    "manual_verified_corrections"
                ]
            _print({**summary, "status": "VERIFIED"})
        elif args.command == "ingest":
            run_dir = ingest_classifier_export(
                export_dir=args.export_dir,
                config=config,
                output_root=args.output_root,
            )
            _print({"run_dir": str(run_dir), **run_status(run_dir)})
        elif args.command == "download":
            selected = [normalize_nct_id(value) for value in args.nct_id]
            _print(
                download_histories(
                    run_dir=args.run_dir,
                    config=config,
                    selected_nct_ids=selected or None,
                )
            )
        elif args.command == "extract":
            _print(extract_run(run_dir=args.run_dir, config=config, replace=args.replace))
        elif args.command == "compare":
            _print(compare_run(run_dir=args.run_dir, config=config, replace=args.replace))
        elif args.command == "summarize":
            _print(summarize_run(run_dir=args.run_dir, config=config, replace=args.replace))
        elif args.command == "status":
            _print(run_status(args.run_dir))
        return 0
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
