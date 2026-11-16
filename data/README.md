# Study inputs

- [classifier](classifier): the curated frozen publication–registry handoff, its
  two-record correction ledger, and publication-level endpoint assessments.
- [registry](registry): compressed ClinicalTrials.gov API responses, including
  current records, history indexes, all 54,631 historical versions, retrieval
  manifests, and source-to-canonical identifier resolutions.
- [semantic_assessment](semantic_assessment): one saved assessment per canonical
  trial and the frozen model request settings.

Release copies remove local paths, private operational metadata, and opaque API
response identifiers. Scientific cells, row identities, selection rules, labels,
and registry responses are preserved. The classifier manifest records the
metadata repackaging; dependent file hashes have been updated. Prepared-input
hashes permit independent verification of semantic input reconstruction.

ClinicalTrials.gov records originate from public registry responses. Publication
text originates from PubMed. These materials retain their existing source rights;
the code license does not grant additional rights over third-party content.
