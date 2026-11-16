# Running

Use Python 3.12, with `python` available on PATH. No additional packages are
required. Run commands from this repository's root.

The README commands rebuild the archived registry workflow offline. Output must
be a new or empty directory. Reconstructed registry, cohort, and descriptive
tables are checked against the frozen outputs. Archives, identifiers, raw
versions, semantic inputs, and saved responses are verified. Several gigabytes
of working space are required. Endpoint-association tables are retained as saved
study outputs.

## Optional registry acquisition

The original acquisition stages remain available for new cohorts:

```text
python src/pipeline.py verify-export --export-dir data/classifier
python src/pipeline.py ingest --export-dir data/classifier
```

Use the run directory printed by `ingest` for `download`, `extract`, `compare`,
`summarize`, or `status`, each with `--run-dir`. Live history endpoints are
undocumented and may reject requests. Archived study reproduction is unaffected.
Derived outputs require `--replace` for regeneration; raw responses are preserved.

## LLM-assisted semantic assessment module

Preparation reconstructs the frozen study inputs and verifies the saved labels:

```text
python src/semantic_assessment.py prepare --run-dir reproduced/registry --output-dir reproduced/prepared_assessments
```

New inference is separate and potentially chargeable. It requires caller-configured
API credentials, `run --execute`, and the exact pending `--expected-calls` count.
The full frozen study requires 1,367 initial calls. Requests are sequential and
checkpointed. An uncertain in-flight request blocks automatic retry. New model
responses may differ from the saved study assessments.
