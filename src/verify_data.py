"""Verify the bundled study data, including every archived registry response."""
import json
from study.release import verify_data

if __name__ == "__main__":
    print(json.dumps(verify_data(), indent=2))
