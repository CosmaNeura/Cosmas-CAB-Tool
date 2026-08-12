# Bundled test corpora — attribution & licenses

These files are synthetic (actor/clinician-created) doctor-patient encounters
containing **no PHI**, bundled for pipeline testing and evaluation.

## acibench_train.csv / acibench_valid.csv — ACI-Bench (CC-BY-4.0)

Yim, W., Fu, Y., Ben Abacha, A., Snider, N., Lin, T., Yetisgen, M.
"ACI-BENCH: a novel ambient clinical intelligence dataset for benchmarking
automatic visit note generation." *Scientific Data* 10, 586 (2023).
Source: https://github.com/wyim/aci-bench
License: Creative Commons Attribution 4.0 (CC-BY-4.0).
Columns: dataset, encounter_id, dialogue, note.

## mts_dialog_train.csv — MTS-Dialog (CC-BY-4.0)

Ben Abacha, A., Yim, W., Adams, G., Snider, N., Yetisgen, M.
"An Empirical Study of Clinical Note Generation from Doctor-Patient
Encounters." EACL 2023.
Source: https://github.com/abachaa/MTS-Dialog
License: Creative Commons Attribution 4.0 (CC-BY-4.0).
Columns: ID, section_header, section_text, dialogue.

Other splits (ACI-Bench test sets 1-3, MTS-Dialog validation/test and
augmented sets) can be fetched with `scripts/fetch_public_data.py`.
