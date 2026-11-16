"""LLM-assisted semantic assessment module."""
import argparse
from study.semantic import execute_assessments, prepare_assessments

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    prepare=commands.add_parser("prepare",help="Reconstruct and validate frozen semantic inputs offline")
    prepare.add_argument("--run-dir",required=True)
    prepare.add_argument("--output-dir",required=True)
    run=commands.add_parser("run",help="Explicitly execute new, potentially chargeable model requests")
    run.add_argument("--prepared-dir",required=True)
    run.add_argument("--output-dir",required=True)
    run.add_argument("--execute",action="store_true")
    run.add_argument("--expected-calls",required=True,type=int)
    args=parser.parse_args()
    if args.command=="prepare":prepare_assessments(args.run_dir,args.output_dir)
    else:execute_assessments(args.prepared_dir,args.output_dir,execute=args.execute,expected_calls=args.expected_calls)
