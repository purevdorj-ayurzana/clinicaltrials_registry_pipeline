"""Rebuild the registry workflow offline; bundled inputs and results remain unchanged."""
import argparse
from study.reproduction import reproduce

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",default="reproduced")
    args=parser.parse_args()
    reproduce(args.output_dir)
