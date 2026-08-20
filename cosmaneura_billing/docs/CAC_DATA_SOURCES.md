# Public Data Sources & Open-Source Tooling for the CosmaNeura CAC/Billing Engine

Web survey, July 2026. Items ranked by practical value to a rule-based outpatient
E/M + ICD-10-CM + edits engine for a small physician group. Module tags:
**KB** (knowledge base), **CA** (coding agent/NLP), **VAL** (validation/benchmarks),
**TEST** (pipeline testing), **WF** (coder/physician workflow).

**Top five to integrate first:** CMS NCCI files (#1), MCD downloads (#2), Tuva
Project (#7) to bootstrap the terminology warehouse, ACI-Bench (#18) as the
transcript-to-note test corpus, and MDACE (#13) as the evidence-span
validation benchmark.

## Tier 1 — Core regulatory knowledge base (free, authoritative, maintained)

**1. CMS NCCI PTP & MUE files** — https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-procedure-procedure-ptp-edits and https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-medically-unlikely-edits-mues
Quarterly ZIP/CSV of Procedure-to-Procedure pairs (with modifier indicators) and
Medically Unlikely Edits for *Practitioner Services* (the file set we want, not
hospital). Direct public CMS download; the tables contain CPT/HCPCS identifiers
and remain subject to applicable source/AMA terms. They are runtime policy
inputs, never model-training examples. Note some MUE values are confidential
("MAI" published, a minority of values withheld). **Plugs in:** direct data feed for
`knowledge/ncci.py` (loader already built); add a quarterly ingest job keyed to
effective dates.

**2. CMS Medicare Coverage Database (MCD) downloads** — https://www.cms.gov/medicare-coverage-database/downloads/downloads.aspx
Weekly (Thursdays) full dumps of current + retired LCDs, Articles (incl. billing
& coding articles with covered ICD-10/CPT lists), and NCDs, as CSV + Access .mdb
with data dictionaries. The license/attestation says CPT codes, descriptions,
and other data are AMA-copyrighted and limits the displayed license to
personal/non-commercial use unless a separate AMA license applies. Strip
descriptors and use only the minimum coverage relationships under the
organization's entitlement; never train or embed the licensed content. **Plugs in:**
`knowledge/coverage.py` (loader already built); the "Billing and Coding Articles"
tables are the machine-usable ICD-10↔HCPCS coverage pairs.

**3. ICD-10-CM FY code files (CMS + CDC/NCHS)** — https://www.cms.gov/medicare/coding-billing/icd-10-codes and https://www.cdc.gov/nchs/icd/icd-10-cm/files.html
Annual (Oct 1) full code set: tabular, index, addenda, official guidelines,
POA-exempt list, conversion table of new/deleted codes. Public domain, no
license. **Plugs in:** KB backbone for diagnosis proposals; the yearly conversion
table drives code-versioning logic. Pairs with the AHRQ CCSR already in use
(CCSR files: https://hcup-us.ahrq.gov/toolssoftware/ccsr/ccs_refined.jsp, free
with HCUP click-through).

**4. CMS PFS Relative Value (PPRRVU) files** — https://www.cms.gov/medicare/payment/fee-schedules/physician/pfs-relative-value-files
Quarterly-lettered annual RVU files: work/PE/MP RVUs, status indicators, global
periods, bilateral/assistant/multiple-procedure indicators, PC/TC. **Licensing
trap:** file contains AMA-copyrighted CPT descriptors under a limited license —
internal/claims use OK, but redistributing descriptors in a product requires an
AMA CPT license. The RVU numbers and policy indicators themselves are CMS data.
**Plugs in:** KB for payment indicators + denial-risk flags (status-indicator
"B" bundled codes, global-period conflicts).

**5. HCPCS Level II quarterly files** — https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system/quarterly-update
Quarterly alpha-numeric HCPCS file with descriptors, coverage/pricing indicators,
BETOS/TOS. Free, public domain (HCPCS Level II is CMS-maintained, unlike CPT).
**Plugs in:** KB for drug/supply/G-code handling; G-codes matter for Medicare
E/M-adjacent services (G2211 add-on, AWV codes).

**6. NLM SNOMED CT → ICD-10-CM map (UMLS)** — https://www.nlm.nih.gov/research/umls/mapping_projects/snomedct_to_icd10cm.html
Rule-based map from SNOMED CT clinical findings to ICD-10-CM, refreshed with the
US Edition; distributed via UMLS Terminology Services. **Access hurdle:** free
UMLS license (individual signup, annual report); SNOMED CT US Edition is free
*inside the US* under the NLM affiliate license — international deployment
requires SNOMED International membership/fees. UMLS Metathesaurus has
per-vocabulary restrictions (category 3 vocabularies not redistributable).
**Plugs in:** CA — bridge from NER/entity-linking output (SNOMED concepts) to
billable ICD-10-CM proposals.

**7. The Tuva Project** — https://github.com/tuva-health/tuva (docs: https://thetuvaproject.com)
Apache-2.0 dbt package: pre-cleaned terminology seed tables (ICD-10-CM/PCS,
HCPCS, CCSR, place of service, provider taxonomy, SNOMED-to-ICD-10 map, etc.),
claims data model, data-quality tests. Actively maintained. **Plugs in:**
fastest way to bootstrap a terminology warehouse — the CMS/NLM file wrangling is
already done; also a reference schema for claims/feedback tables.

**8. NUCC Provider Taxonomy + CMS Place of Service codes** — https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40 and https://www.cms.gov/medicare/coding-billing/place-of-service-codes/code-sets
Taxonomy CSV updated twice yearly (free, NUCC copyright with liberal use terms);
POS code set free from CMS. **Plugs in:** KB — POS drives facility/non-facility
RVUs and telehealth edit logic; taxonomy drives specialty-specific NCCI/LCD
applicability.

## Tier 2 — Clinical NLP components (coding agent)

**9. medspaCy** — https://github.com/medspacy/medspacy
MIT. Clinical-text spaCy toolkit: ConText (negation/hypothetical/family-history/
historical), sectionizer, clinical tokenizer, TargetMatcher rules. Maintained.
**Plugs in:** CA — the assertion/negation and section detection needed before
proposing a diagnosis with an evidence span ("denies chest pain" must not yield
R07.9); the sectionizer also feeds MDM leveling (data-reviewed vs.
problems-addressed live in different note sections).

**10. scispaCy** — https://github.com/allenai/scispacy
Apache-2.0 (Allen AI). Biomedical spaCy models: NER, abbreviation resolution,
UMLS entity linker. Models trained on literature, not clinical notes — pair with
medspaCy. **Plugs in:** CA — candidate entity extraction + UMLS CUI linking
feeding the SNOMED→ICD-10-CM map (#6).

**11. MedCAT (CogStack)** — https://github.com/CogStack/MedCAT
NER+linking to full UMLS/SNOMED with self-supervised training, MetaCAT for
negation/experiencer; v2 released, actively maintained. **Licensing trap:**
Elastic License 2.0 since Aug 2022 — fine to use internally in a product, but
MedCAT itself cannot be offered as a managed service; pretrained model packs
additionally require UMLS/SNOMED credentials and some were trained on MIMIC
(research-use taint — retrain or use their non-MIMIC packs for commercial use).
**Plugs in:** CA — strongest open entity-linker beyond scispaCy;
**MedCATtrainer** (https://github.com/CogStack/MedCATtrainer) is a web UI for
clinician/coder annotation feedback — the closest open analog to the coder-review
loop, worth mining for UX and active-learning patterns.

**12. QuickUMLS** — https://github.com/Georgetown-IR-Lab/QuickUMLS
MIT code; fast approximate UMLS concept matcher (simstring-based). Lightly
maintained but stable and simple. Requires your own UMLS files (license per #6).
**Plugs in:** CA — cheap high-recall candidate generator for evidence-span →
concept mapping; good fallback where transformer inference is too heavy.

## Tier 3 — Benchmarks & evidence-span validation

**13. MDACE** — https://github.com/solventum-oss/MDACE (moved from 3mcloud; old URL 404s)
The only public evidence-span-annotated coding dataset: 302 inpatient + 52
**professional-fee** charts from MIMIC-III, ~9.5k evidence spans with character
offsets, ICD codes, plus baseline models and eval scripts. Repo is MIT and
maintained, but annotations are stand-off — requires MIMIC-III credentialing on
PhysioNet (free: identity verification + CITI training + DUA; **non-commercial
research use only**). **Plugs in:** VAL — the natural benchmark for
evidence-span extraction; the Profee subset is the closest to our outpatient use
case. Strictly for internal evaluation, never training a shipped model, given
the MIMIC DUA.

**14. explainable-medical-coding (Edin et al., EMNLP 2024)** — https://github.com/JoakimEdin/explainable-medical-coding
MIT code: PLM-ICD variants + feature-attribution methods evaluated against MDACE
spans; HuggingFace dataset builders for MIMIC-III/IV/MDACE. Model weights are
MIMIC-derived (non-commercial). Companion repo
https://github.com/JoakimEdin/medical-coding-reproducibility is the
best-engineered harness for CAML/PLM-ICD/LAAT baselines. **Plugs in:** VAL —
reuse their span-scoring metrics and eval harness; treat the models as internal
baselines only.

**15. PLM-ICD / CAML** — https://github.com/MiuLab/PLM-ICD and https://github.com/jamesmullenbach/caml-mimic
The canonical automated-ICD-coding models. Honest assessment: research code,
MIMIC-trained (ICD-9-heavy for CAML), effectively abandonware as products, and
non-commercial by data lineage. Given our architecture is rule-based +
evidence-grounded, their value is **VAL/benchmark context only** — don't build
on them. HuggingFace ICD-10 checkpoints (e.g.
`AkshatSurolia/ICD-10-Code-Prediction`) are similarly research-grade with
undocumented training data; avoid in production.

**16. MIMIC-IV + MIMIC-IV-Note (PhysioNet)** — https://physionet.org/content/mimiciv/ and https://physionet.org/content/mimic-iv-note/
The de facto clinical-notes corpus with billed ICD-10-CM/PCS codes. Free but
credentialed (CITI training + DUA), **non-commercial research only**, no cloud
LLM APIs without the PhysioNet-approved route (Azure/AWS opt-in program).
Inpatient-skewed, so imperfect for an outpatient tool. **Plugs in:** VAL —
internal accuracy studies and error analysis, not training data for the shipped
product.

## Tier 4 — Synthetic data for pipeline testing (no PHI)

**17. Synthea + chatty-notes** — https://github.com/synthetichealth/synthea and https://github.com/synthetichealth/chatty-notes
Apache-2.0, actively maintained synthetic patient generator (FHIR/C-CDA/CSV,
incl. ambulatory encounters, conditions coded in SNOMED, claims-ish output).
Native "clinical note" export is templated and thin; chatty-notes adds
LLM-generated narrative notes from Synthea FHIR bundles. **Plugs in:** TEST —
end-to-end pipeline fixtures (intake→note→codes) with zero PHI risk; control the
diagnosis mix to stress specific NCCI/LCD rules.

**18. ACI-Bench / MTS-Dialog** — https://github.com/wyim/aci-bench and
https://github.com/abachaa/MTS-Dialog
Both repositories publish data under CC-BY-4.0, but copyright permission is not
the same as patient-data provenance. ACI-Bench's peer-reviewed descriptor
documents expert-constructed and clinician/volunteer role-played encounters,
so it is a training/evaluation candidate after identifier screening and
attribution. The reviewed MTS-Dialog repository and paper landing page do not
establish that every source dialogue is wholly synthetic; quarantine it from
release demos and training until that lineage is documented. **Plugs in:** TEST
— ACI-Bench is the cleared public stand-in for transcript + signed-note input.

**19. PriMock57** — https://github.com/babylonhealth/primock57
57 mock primary-care consultations with **audio**, transcripts, and notes
(Babylon Health; CC-BY-4.0). Small but the only public set with
audio→transcript→note, and primary-care outpatient like our users. **Plugs in:**
TEST — validating transcript-noise robustness of evidence extraction.

**20. CMS DE-SynPUF** — https://www.cms.gov/data-research/statistics-trends-and-reports/medicare-claims-synthetic-public-use-files
Synthetic Medicare claims (2008–2010): beneficiary, carrier (Part B professional
claims with HCPCS/ICD-9), inpatient/outpatient. Free public download without
ordinary beneficiary-privacy restrictions, but old (ICD-9 era) and statistically
distorted. **Plugs in:** TEST — volume/load
testing of the claims-side plumbing (837 generation, edit throughput), not
clinical realism, modern coding truth, or clinical-label training. Follow the
CMS public-use disclaimer linked by the source page.

## Tier 5 — Claims plumbing & denial intelligence

**21. X12 parsers: pyx12 and Databricks x12-edi-parser** — https://github.com/azoner/pyx12 and https://github.com/databricks-industry-solutions/x12-edi-parser
pyx12 (BSD-ish, on PyPI): battle-tested 837/835 parser/validator against
implementation-guide maps — dormant but stable; map coverage is the known
bottleneck. Databricks x12-edi-parser ("Ember", Apache-2.0, actively developed):
Spark-native structured parsing of 837/835/834 — overkill for a small group,
but its schema design is worth borrowing. Lighter option: `edi-835-parser` on
PyPI for remit ingestion. **Plugs in:** WF/feedback loop — parse 835 remits to
attach real CARC/RARC denial outcomes to encounters, closing the denial-risk
training loop.

**22. CARC/RARC code lists (X12) + CAQH CORE code combinations** — https://x12.org/codes/claim-adjustment-reason-codes , https://x12.org/codes/remittance-advice-remark-codes , https://www.caqh.org/core/caqh-core-code-combinations
**Licensing trap:** X12 holds copyright; lists are freely *viewable* on x12.org
but bulk machine-readable download/redistribution requires an X12 license tier.
CAQH CORE publishes the CORE-required CARC/RARC/CAGC combination scenarios
(e.g., "denial due to coverage," "missing information") as a downloadable
spreadsheet — a free, structured way to map remit codes to denial *categories*.
**Plugs in:** KB for the denial-risk module's taxonomy; check whether Tuva's
seeds already carry what's needed before licensing from X12.

**23. Medicare "Physician & Other Practitioners" PUF + CERT improper-payment data** — https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners and https://www.cms.gov/data-research/monitoring-programs/medicare-fee-service-compliance-programs/improper-payment-measurement-programs/comprehensive-error-rate-testing-cert
Free, no restrictions. The PUF gives per-provider, per-HCPCS utilization — the
national distribution of 99213 vs 99214 by specialty, the exact baseline
auditors use to flag E/M-level outliers (already partially used by
`benchmarks/em_benchmark.py`). CERT reports quantify improper-payment rates by
service type (E/M consistently top-listed, usually "insufficient
documentation"). **Plugs in:** denial-risk flags — benchmark a physician's
proposed E/M distribution against specialty norms and weight risk scoring by
CERT error categories.

## Tier 6 — E/M, HCC, and workflow findings

**24. 2021 AMA E/M MDM: no viable open-source implementation exists.** Verified
via GitHub/web search: only proprietary web calculators (AAPC, PatientNotes.ai,
PCC's free tool). The authoritative free artifact is the AMA MDM grid PDF
(https://www.ama-assn.org/system/files/2019-06/cpt-revised-mdm-grid.pdf) and the
E/M guidelines (https://www.ama-assn.org/practice-management/cpt/cpt-evaluation-and-management).
**Implication:** our rule-based MDM grid is genuinely differentiated IP — and
note the **CPT trap**: the grid's logic (problems/data/risk definitions) comes
from AMA-copyrighted CPT; using it internally to level visits is standard
practice, but redistributing CPT descriptor text in the product requires an AMA
license.

**25. hccpy → hccinfhir** — https://github.com/yubin-park/hccpy (Apache-2.0, now
dormant) succeeded by https://github.com/mimilabs/hccinfhir (Apache-2.0,
maintained; CMS-HCC V22–V28, ESRD, HHS-HCC; works from FHIR and 837 inputs).
**Plugs in:** KB/side-feature — surface "this documented condition risk-adjusts
(V28 HCC)" nudges to physicians; cheap add-on since our ICD proposals are the
only input needed.

**26. Coder-review / CDI workflow: thin open-source landscape.** Nothing open
exists that replicates a coder-review queue or CDI platform. Best raw material:
**MedCATtrainer** (#11) for clinician-in-the-loop annotation UX; **Label
Studio** (https://github.com/HumanSignal/label-studio, Apache-2.0 community
edition) as a generic span-annotation UI that could be bent into a coder
evidence-review screen for building gold datasets. For physician documentation
nudges, the research anchor is the ACL/EMNLP explainability line (#13/#14) — no
packaged OSS CDI tool to adopt.

## Cross-cutting licensing traps (summary)

- **CPT (AMA):** E/M codes, descriptors, and MDM definitions are copyrighted;
  embedded descriptors in CMS RVU/MCD files carry limited-use terms. A
  commercial product displaying CPT descriptors needs an AMA license. HCPCS
  Level II and ICD-10-CM are free.
- **UMLS/SNOMED:** UMLS license is free but individual, non-transferable, with
  annual reporting; SNOMED CT free only in member countries (US via NLM) —
  international customers change the calculus; per-source-vocabulary
  restrictions inside UMLS.
- **MIMIC/PhysioNet (incl. MDACE spans):** credentialed, non-commercial research
  DUA — usable for validation/benchmarking, radioactive as training data for a
  commercial model.
- **X12:** CARC/RARC and 837/835 implementation guides are copyrighted; budget
  for a license or use CAQH CORE / viewable lists.
- **MedCAT:** Elastic License 2.0 — internal use OK, cannot resell as a service.
- **GEMs are dead:** last CMS GEMs release was FY2018 (archive:
  https://www.cms.gov/medicare/coding-billing/icd-10-codes/icd-10-cm-icd-10-pcs-gem-archive)
  — do not build ICD-9↔10 logic on them; only relevant for parsing legacy data
  like DE-SynPUF.
