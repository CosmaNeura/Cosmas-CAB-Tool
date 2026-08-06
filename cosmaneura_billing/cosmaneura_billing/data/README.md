# Bundled data — sources and provenance

All files here are public/government works or Apache-2.0-packaged public data;
no AMA CPT descriptors are bundled anywhere (code numbers + numeric stats only).

| File | Source | Contents |
|---|---|---|
| `icd10_ccsr_subset.csv`, `icd10cm_ccsr_full.csv.gz` | AHRQ DXCCSR (via prototype) | ICD-10-CM ↔ CCSR map |
| `icd10cm_fy_current.csv.gz` | CMS ICD-10-CM FY files via Tuva Project | current-FY code set + billable flags |
| `hcpcs_level_2.csv.gz` | CMS HCPCS quarterly via Tuva Project | HCPCS Level II descriptors (CMS-maintained, public) |
| `place_of_service.csv` | CMS POS code set via Tuva Project | POS codes incl. telehealth 02/10 |
| `psps_denial_rates_2024.csv.gz` | CMS Physician/Supplier Procedure Summary 2024 (data.cms.gov, public) | per-HCPCS national Medicare submitted/denied services and denial rate, aggregated from 14.4M summary rows; codes with ≥100 submissions |
| `tcpuf_issuer_denials_2026.csv.gz` | CMS Marketplace Transparency in Coverage PUF PY2026 (2024 claims) | issuer-level claims received/denied, denial rate, top denial-reason category; issuers with ≥1,000 claims |
| `ncci/` | CMS NCCI 2026 Q3 (see ncci/README.md) | PTP edits + MUE limits, practitioner |
| `mcd/` | CMS Medicare Coverage Database (see mcd/README.md) | NGS-jurisdiction coverage articles |
| `corpora/` | ACI-Bench, MTS-Dialog (see corpora/README.md) | synthetic encounters, CC-BY-4.0 |

Also: `benchmarks/data/em_benchmark_by_specialty.csv` (national) and
`benchmarks/data/em_nh_2024.csv` (New Hampshire, from the CMS Medicare
Physician & Other Practitioners by-Provider-and-Service 2024 data via the
data.cms.gov API) — E/M utilization and allowed amounts; population benchmarks
only, never an input to coding an individual encounter.

Refresh cadence: PSPS and the practitioner PUF are annual; TC-PUF is annual
(plan-year); NCCI quarterly; MCD weekly; ICD-10-CM each Oct 1. See
`scripts/fetch_public_data.py` and docs/CLAIMS_DATA_SOURCES.md.
