# Bundled CMS NCCI edits — practitioner services

Source: CMS National Correct Coding Initiative, downloaded from cms.gov.
Public domain (the underlying CPT code *numbers* appear as data; no CPT
descriptors are included).

- `ptp_practitioner_2026q3_v322.tsv.gz` — Procedure-to-Procedure edits,
  practitioner services, **2026 Q3, version v32.2r0**, merged from the four
  CMS distribution files and trimmed to active edits only (rows carrying a
  real deletion date — 904,804 of 2,633,389 — were dropped) and to the columns
  the engine uses: column 1, column 2, effective date, deletion date, modifier
  indicator. 1,728,585 active pairs.
- `mue_practitioner_2026q3.tsv.gz` — Medically Unlikely Edits (units/day),
  practitioner services, effective 2026-07-01, converted from the CMS XLSX
  with the title preamble removed. 15,161 codes.

`knowledge/ncci.py::NCCIEdits.from_bundled()` auto-loads whatever is in this
directory (filename must contain "ptp" / "mue"; .gz is fine). Loading the full
PTP set takes ~18s and ~1.2 GB RAM; the result is memoized per process.

## Refreshing (quarterly)

CMS posts new editions quarterly:
https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-procedure-procedure-ptp-edits
https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-medically-unlikely-edits-mues

Download the *practitioner* PTP zips (4 parts) and the practitioner MUE
table, then rebuild with the same trim (see scripts/fetch_public_data.py for
pointers). Replace the files here — the loader picks up any matching name.
