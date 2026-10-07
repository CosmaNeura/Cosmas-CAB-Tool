# Bundled CMS NCCI edits — practitioner services

Source: CMS National Correct Coding Initiative, downloaded from cms.gov.
The bundle contains CPT/HCPCS identifiers but no CPT descriptors. Treat the
identifiers and edit relationships as entitlement-bound runtime policy data
under the applicable CMS/AMA source terms; they are not public-domain training
examples and must not be used for model training, embeddings, distillation, or
redistribution beyond the organization's authorization.

- `ptp_practitioner_2026q3_v322.tsv.gz` — Procedure-to-Procedure edits,
  practitioner services, **2026 Q3, version v32.2r0**, merged from the four
  CMS distribution files and trimmed to active edits only (rows carrying a
  real deletion date — 904,804 of 2,633,389 — were dropped) and to the columns
  the engine uses: column 1, column 2, effective date, deletion date, modifier
  indicator. 1,728,585 active pairs.
- `mue_practitioner_2026q3.tsv.gz` — Medically Unlikely Edits (units/day),
  practitioner services, effective 2026-07-01, converted from the CMS XLSX
  with the title preamble removed. 15,161 codes.

`knowledge/ncci.py::NCCIEdits.from_bundled()` uses the checksum-pinned
`catalog_practitioner_2026q3.v1.json.gz` derivative when both original source
files and the cache match their reviewed checksums. The gzip JSON contains the
same directed modifiers and MUE limits, with source, generator, normalization,
and row-count metadata. Its size is bounded to 16 MiB compressed and 64 MiB
decoded; it contains no executable serialization or model inputs.

The cache checksum covers every key/value. Generation validates normalized
codes and modifiers; loading checks the closed header, outer mapping shape,
and counts without repeating a Python loop across 1.7 million pairs. Missing,
altered, malformed, or differently sourced caches use the existing CSV/gzip/XLSX
readers. Custom `from_files()` imports always use those readers. Results and
source checks remain memoized per directory for the life of each process.
The derivative retains the same CMS/AMA source-use restrictions as its inputs.

The reviewed 2026 Q3 derivative contains 1,728,585 directed pairs and 15,162
MUE entries, including the source reader's empty-code entry. It is 468,529 bytes
compressed and 21,129,135 bytes decoded. Its SHA-256 is
`5e2ebb3880e4d54554f6287b23291d264eb5a2bbfb9c5a570df4276d40a8dc49`.
The build metadata records the reviewed generation-input loader checksum;
updating final loader pins does not rewrite that historical checksum.

## Refreshing (quarterly)

CMS posts new editions quarterly:
https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-procedure-procedure-ptp-edits
https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-medically-unlikely-edits-mues

Download the *practitioner* PTP zips (4 parts) and the practitioner MUE
table, then rebuild with the same trim (see scripts/fetch_public_data.py for
pointers). Replace the files here — the loader picks up any matching name.
A changed edition intentionally falls back to the source readers until a new
derivative is independently generated and reviewed. The compiler at
`scripts/compile_ncci_catalog.py` is inert by default, refuses an existing output,
requires the exact loader/source checksums, and uses deterministic gzip metadata
and sorted JSON. Rebuilding also requires updating the cache/build pins in the
loader and verifying every PTP query in both orientations and every MUE result
against the source reader before landing the new artifact. The compiled filename
must contain neither `ptp` nor `mue`, to preserve fallback source selection.
