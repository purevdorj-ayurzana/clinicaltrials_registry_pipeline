# ClinicalTrials Registry Pipeline

Data and code associated with the manuscript **“An LLM-assisted Pipeline for
Identifying ClinicalTrials.gov Registry Changes and Their Association with
Primary Endpoint Positivity.”**

The pipeline compares historical ClinicalTrials.gov registration records,
identifies changes using deterministic review rules, as well as an additional LLM-assisted semantic module, and prepares trial-level
data for comparison with publication-reported primary-endpoint positivity.

## Data

The export contains **1,759 NCT identifiers**, representing 1,758 unique trials, because two identifiers referred to the same trial.
[Data](data/README.md) includes the saved output from the separate RCT abstract classifier, along with documented NCT identifier corrections, and archived ClinicalTrials.gov records. All trial histories are included, so the workflow can be reproduced offline without downloading them again.

Abstract classification is described in the
[companion repository](https://github.com/p-ayurzana/rct_abstract_classifier).

## LLM-assisted semantic assessment module

The module assesses meaningful primary-outcome changes across registry histories.
The exact [prompt](prompts/semantic_assessment.txt), response schema, preparation
code, model settings, and saved assessments are included. The study used
**gpt-5.6-sol with medium reasoning**: 1,367 trials received LLM assessments;
391 had no comparison requiring assessment and were classified deterministically.
There is no human validation dataset for this registry-change assessment.

## Results and use

[Results](results/README.md) provides machine-readable numerical tables.
The principal association cohort contains **1,400 trials: 967 positive and
433 negative**. Publication figures and formatted manuscript tables are omitted.

Use Python 3.12. The standard library is sufficient.
From the repository root:

```text
python src/verify_data.py
python -m unittest discover -s tests
python src/reproduce.py --output-dir reproduced
```

The command rebuilds the registry and cohort workflow offline, checks its
reconstructed tables, and verifies the saved semantic assessments. It writes to
a separate directory and requires several gigabytes of temporary disk space.
Endpoint-association tables remain available as saved study outputs.
No API key or paid model call is needed. Optional acquisition and assessment
commands are described in [Running](RUNNING.md).

Review flags concern changes to registry records; they do not establish formal
protocol amendments or misconduct. Endpoint associations are exploratory and
non-causal. Code is released under the [MIT license](LICENSE); source data
retain their existing rights.
