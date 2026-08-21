# Data provenance and allowed-use catalog

Release-candidate snapshot: **2026-08-19**. This is an engineering release
gate, not a legal opinion. No bulk dataset was downloaded during this audit.

## Allowed-use decisions

| Asset | Official source and access posture | Patient-data posture | CosmaNeura lane |
|---|---|---|---|
| **Synthea** | [MITRE Synthea](https://synthetichealth.github.io/synthea/) says its generated records are realistic but not real and free from cost, privacy, and security restrictions. The [source license](https://github.com/synthetichealth/synthea/blob/master/LICENSE) is Apache-2.0. | Synthetic; no real patient record. | **Training candidate and evaluation.** Generate locally from a pinned commit, seed, and module manifest; scan outputs for direct-identifier-shaped values before use. Do not silently mix them with real records. |
| **CMS Synthetic Medicare Enrollment, FFS Claims, and PDE** | [CMS collection](https://data.cms.gov/collection/synthetic-medicare-enrollment-fee-for-service-claims-and-prescription-drug-event) calls the files “realistic-but-not-real,” publicly released without the usual privacy/security restrictions, and warns that they cannot provide true answers about beneficiaries. | Synthetic; CMS says records are not tied to real patients. | **Schema, load, and claims-flow evaluation only.** Not a clinical-label source and not evidence for population conclusions. Prefer this current RIF-shaped collection over DE-SynPUF for new plumbing tests. |
| **CMS 2008–2010 DE-SynPUF** | [CMS SynPUF overview](https://www.cms.gov/data-research/statistics-trends-and-reports/medicare-claims-synthetic-public-use-files) provides free public downloads and points users to the CMS public-use disclaimer. [Sample 1](https://www.cms.gov/data-research/statistics-trends-and-reports/medicare-claims-synthetic-public-use-files/cms-2008-2010-data-entrepreneurs-synthetic-public-use-file-de-synpuf/de10-sample-1) shows the beneficiary, carrier, inpatient, outpatient, and PDE layouts. No application DUA is identified on these pages. | Synthetic and privacy-preserving; limited inferential value. | **Legacy schema/load evaluation only.** It is ICD-9-era and statistically distorted, so never use it as a modern coding truth set or clinical training label source. |
| **AHRQ CCSR for ICD-10-CM** | [AHRQ CCSR](https://hcup-us.ahrq.gov/toolssoftware/ccsr/dxccsr.jsp) offers a direct public tool/reference download (v2026.1 at audit time), requests citation, and does not show a dataset-specific DUA or blanket license grant on the source page. | Classification map; no patient rows. | **Runtime retrieval, grouping, and evaluation only.** Do not treat a category mapping as a diagnosis label or train a model to infer diagnoses from it. Retain the AHRQ citation and version. |
| **CMS NCCI PTP and MUE** | CMS publishes [PTP](https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-procedure-procedure-ptp-edits) and [MUE](https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-medically-unlikely-edits-mues) files quarterly. The pages expose policy/edit tables, not patient records. The tracked bundle is practitioner 2026 Q3 and contains code identifiers and numeric edit data, not descriptors. | No PHI; policy tables only. | **Runtime rule retrieval and synthetic-case validation only. Never model training.** NCCI may reject or flag a proposed service; it must never originate a clinical code. Keep quarter/effective date pinned. |
| **CMS Medicare Coverage Database (MCD)** | [MCD Downloads](https://www.cms.gov/medicare-coverage-database/downloads/downloads.aspx) publishes weekly LCD, Article, and NCD extracts. Its acceptance notice states that CPT codes, descriptions, and other data are AMA-copyrighted; the displayed CMS license is personal/non-commercial unless a separate AMA license authorizes more. The tracked NGS subset strips CPT descriptor columns. | No PHI; coverage-policy content. | **Runtime coverage retrieval and evaluation only, under the organization’s applicable entitlement. Never training.** Keep only necessary article IDs, status, code relationships, and version metadata; do not ingest narrative or descriptors into model training or embeddings. |
| **AMA CPT Standard Data File and licensed CPT content** | [AMA CPT](https://www.ama-assn.org/practice-management/cpt) and the MCD acceptance notice reserve CPT rights and direct commercial users to AMA licensing. | No PHI, but proprietary terminology. | **Explicitly excluded from training.** No fine-tuning, prompt corpus, embeddings/vector index, distillation, synthetic-label generation, or redistribution from the CPT Standard Data File or licensed descriptors. A separately licensed runtime lookup may return the minimum authorized descriptor to an entitled user; code identifiers in tests do not authorize descriptor use. |
| **ACI-Bench** | [Repository](https://github.com/wyim/aci-bench) is CC BY 4.0. The [peer-reviewed data descriptor](https://www.nature.com/articles/s41597-023-02487-3) says domain experts created two subsets from experience and studied encounters, while a clinician and volunteer role-played the third; it distinguishes these from the real comparison corpus. | Constructed/role-played encounters, not real patient encounters. Metadata and text can still contain identifier-shaped fictional values. | **Training candidate and evaluation, with attribution**, after pinning the release and running identifier/content screening. It is not part of the bounded release demo pack. |
| **MTS-Dialog** | [Repository](https://github.com/abachaa/MTS-Dialog) is CC BY 4.0 and describes 1,700 conversation/note pairs. The [paper landing page](https://aclanthology.org/2023.eacl-main.168/) reviewed in this audit does not establish whether every source dialogue is wholly synthetic. | **Unresolved for the synthetic-only gate.** A permissive copyright license does not establish absence of real or deidentified patient content. | **Quarantined from release demos and training** until a source-method record proves the patient-data lineage. Existing bundled files may remain for legacy tests but must not be represented as wholly synthetic on this audit alone. |
| **`cosmaneura-demo-rc-v1`** | Authored in-repository solely for this release candidate; validated by `cosmaneura_billing.corpus.demo_release_candidate`. | Wholly synthetic; no real or deidentified record was used or adapted; direct-identifier-shaped values are prohibited. | **Demo and evaluation only; training prohibited.** The expected captures are billable ICD-10-CM codes explicitly written in exact signed-note evidence. No CPT expectation or proprietary descriptor is present. |
| **`cosmaneura-demo-source-extension-v1`** | Authored in-repository as a separately digest-pinned extension; validated by `cosmaneura_billing.corpus.demo_source_extension`. | Wholly synthetic; no real or deidentified record was used or adapted. URL, IP, ZIP, address, long-identifier, control/bidirectional-character, and direct-identifier-shaped values are prohibited. | **Demo and blinded evaluation only; training prohibited.** The packaged artifact contains source encounters only: no coding tokens, expected outputs, labels, CPT/E/M expectations, or submitted codes. The blinded review workflow is kept outside the installed package and image. |
| **`cosmaneura-synthetic-chase-v1`** | Authored in-repository for the insurance follow-up demo; exact canonical digest `fc207ef1…59d23ff` is enforced by `chase.synthetic_demo`. | Wholly synthetic. The four contact destinations are restricted to the NANP fictional `+1 202-555-0101`–`0104` range and are never accepted from a request. | **Demo only; training prohibited.** Covers missing-card, expired-plan, inactive-eligibility, and direct-contract human-review states. Installation is Admin-only and refused unless all outbound latches are dormant and transport is `record_only`. |

## Tracked-repository audit

- `cosmaneura_billing/cosmaneura_billing/data/corpora/acibench_*.csv` contains
  67 train and 20 validation encounters and has a verified CC BY 4.0 /
  constructed-encounter source. Keep attribution and run identifier screening
  before any broader use.
- `cosmaneura_billing/cosmaneura_billing/data/corpora/mts_dialog_train.csv`
  contains 1,201 rows and has a verified CC BY 4.0 license but unresolved
  synthetic-only provenance; it is not approved for release training or demo
  claims.
- `cosmaneura_billing/cosmaneura_billing/data/{icd10cm_ccsr_full.csv.gz,ncci/,mcd/}`
  are knowledge/policy assets. They are never clinical training examples.
- `cosmaneura_billing/cosmaneura_billing/data/synthetic_chase/pack.v1.json`
  is the fixed four-case F9 demo pack. Exact pack/case provenance is persisted
  server-side before a fictional destination can be rendered.
- Root-level `iCliniq.feather`, `GenMedGPT-5k.feather`, `combined.feather`, and
  `med_chat_QA_data.csv` lack a complete tracked source/license/PHI manifest.
  The existing loader itself says commercial terms must be confirmed. They are
  **excluded from training, release evaluation, demos, and deployment images**
  until lineage, license, consent/DUA, and PHI review are complete.
- No installed connector exposed an additional approved clinical-data source.
  The audit used the tracked worktree and the official pages above read-only.

## Acquisition and release controls

Before adding any external rows, record source URL, release/version, retrieval
date, license/terms snapshot, DUA status, patient-data class, permitted lane,
and a content hash. Synthetic records must have synthetic IDs and an explicit
statement that they were generated or role-played—not merely deidentified.

Real or deidentified patient data remains outside this release candidate. It
requires the separate entitlement, minimum-necessary, DUA/BAA, security, and
human-approval gate. “Public,” “deidentified,” and “synthetic” are not
interchangeable.

For the bounded pack, run:

```bash
cd cosmaneura_billing
python -m cosmaneura_billing.corpus.demo_release_candidate
python -m cosmaneura_billing.corpus.demo_source_extension
pytest tests/test_demo_release_candidate_pack.py tests/test_demo_source_extension_pack.py
pytest tests/test_synthetic_chase_demo.py
```
