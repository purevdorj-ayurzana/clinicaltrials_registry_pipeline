# Study results

CSV files contain the frozen cohort flow and exclusions, trial-level covariates,
registry-change frequencies, registration timing, and endpoint-association results.
The Python command rebuilds and checks the cohort, covariate, registry-change,
registration-timing, and semantic-comparison tables. Endpoint-association tables
are retained as saved study outputs.

Semantic change types describe one decisive comparison; promotion/demotion
labels refer to their presence anywhere in the supplied history. Neither
algorithm is a human reference standard. Associations are exploratory and
non-causal.
