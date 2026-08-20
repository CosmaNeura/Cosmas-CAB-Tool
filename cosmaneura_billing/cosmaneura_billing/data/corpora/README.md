# Bundled test corpora — attribution, licenses, and release posture

Copyright permission and patient-data provenance are separate gates. See
`cosmaneura_billing/docs/DATA_PROVENANCE_CATALOG.md` for the controlling
allowed-use decision.

## acibench_train.csv / acibench_valid.csv — ACI-Bench (CC-BY-4.0)

Yim, W., Fu, Y., Ben Abacha, A., Snider, N., Lin, T., Yetisgen, M.
"ACI-BENCH: a novel ambient clinical intelligence dataset for benchmarking
automatic visit note generation." *Scientific Data* 10, 586 (2023).
Source: https://github.com/wyim/aci-bench
License: Creative Commons Attribution 4.0 (CC-BY-4.0).
Columns: dataset, encounter_id, dialogue, note.

The peer-reviewed data descriptor says domain experts constructed the
virtassist/virtscribe subsets and a clinician and volunteer role-played the ACI
subset. It distinguishes these from the real comparison corpus. Treat this as
constructed/role-played data, retain attribution, and screen identifier-shaped
fictional values before broader training or evaluation.

## mts_dialog_train.csv — MTS-Dialog (CC-BY-4.0)

Ben Abacha, A., Yim, W., Fan, Y., Lin, T.
"An Empirical Study of Clinical Note Generation from Doctor-Patient
Encounters." EACL 2023.
Source: https://github.com/abachaa/MTS-Dialog
License: Creative Commons Attribution 4.0 (CC-BY-4.0).
Columns: ID, section_header, section_text, dialogue.

The official repository and paper landing page reviewed for the 2026-08-19
release gate do not establish that every source dialogue is wholly synthetic.
Until the source-method record is completed, this bundled file is retained for
legacy tests but is quarantined from release demos and training. Do not present
deidentified or unresolved-origin data as synthetic.

Other splits (ACI-Bench test sets 1-3, MTS-Dialog validation/test and
augmented sets) can be fetched with `scripts/fetch_public_data.py`.
