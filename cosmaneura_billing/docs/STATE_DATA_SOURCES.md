# State-Level Health Data Sources for the NH Billing Engine (July 2026)

Ranked by value × downloadability today. Item 1 (PSPS carrier slice) is already
integrated in the engine (data/psps_denial_rates_nh_2024.csv.gz).

## Tier 1 — Downloadable now, direct NH value

### 1. PSPS + CMS carrier/locality mapping → NH-specific Medicare denial rates  ✅ DONE
PSPS rows carry carrier number + pricing locality; NH is a single statewide
PFS locality under NGS J-K (carrier 14312, per the CMS locality key /
GPCI file in the PFS RVU package: https://www.cms.gov/medicare/payment/fee-schedules).
Filtering PSPS 2024 to carrier 14312 yields NH-specific per-code denial rates
(1,622 codes with >=100 NH submissions) — bundled and live in the denial agent.

### 2. NH HealthCost Statewide Rates Report (NHID, built from CHIS APCD)
- https://nhhealthcost.nh.gov/statewide-rates-report (request form:
  https://nhhealthcost.nh.gov/request-statewide-report)
- CPT-code-level tables of provider charges and insurer paid/allowed prices at
  25th/50th/75th percentiles, professional vs institutional, by carrier
  (Ambetter, Anthem, Cigna, Harvard Pilgrim, Tufts) and group vs individual
  market. Min-cell rule >=3 obs. CHIS data updated quarterly. Older editions
  were plain .xlsx; current sits behind a lightweight request form — NOT a DUA.
- **Plug-in:** free NH commercial paid-amount benchmark per CPT per carrier —
  the closest thing to CHIS without the DUA. The site 403s non-browser agents;
  use the report file, never a scraper.

### 3. NH Medicaid fee schedules (NH MMIS / DHHS)
- https://www.nhmmis.nh.gov (Documents and Forms → Fee Schedules); e.g. "2026
  Fee Schedule – Covered Procedures Report with SA Requirements" (01-01-2026,
  04-01-2026 update). Overview:
  https://www.dhhs.nh.gov/programs-services/medicaid/medicaid-fee-service
- Per-CPT/HCPCS NH Medicaid FFS rates, coverage flags, prior-auth (SA)
  requirements. Free direct download, PDF (needs table extraction).
- **Plug-in:** Medicaid floor-price validation + covered/PA-required edits.
- Trap: CPT descriptors in the file are AMA-licensed — key rates to codes only.

### 4. NH hospital price-transparency MRFs (negotiated rates by CPT by payer)
- Discovery via https://<hospital-domain>/cms-hpt.txt; indexes:
  https://www.hospitalpricingfiles.org/ and Gigasheet's MRF link list. CMS
  enforcement dataset:
  https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/hospital-price-transparency-enforcement-activities-and-outcomes
- Every NH hospital must post gross charge, cash price, and payer-specific
  negotiated dollar rates per CPT/HCPCS/DRG per plan (45 CFR 180; 2026
  attestation + dollar-rate rules). Dartmouth-Hitchcock discloses ~37 payers.
- **Plug-in:** payer-specific expected facility payment; cross-check vs
  HealthCost medians. Rates are contract terms, not realized payments.
  DoltHub HPT databases are stale — tooling reference only.

### 5. Medicare FFS Public Provider Enrollment (PECOS) + NPPES — NH provider universe
- https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-fee-for-service-public-provider-enrollment ;
  https://download.cms.gov/nppes/NPI_Files.html
- PECOS: enrollment type, specialty, reassignments, monthly, CSV+API. NPPES:
  all NPIs, taxonomy, monthly full + weekly increments.
- **Plug-in:** provider master + pre-claim enrollment validation; PECOS is
  enrollment truth, NPPES addresses are stale.

### 6. CMS Market Saturation & Utilization (State-County)
- https://data.cms.gov/summary-statistics-on-use-and-payments/program-integrity-market-saturation-by-type-of-service/market-saturation-utilization-state-county
- Provider/beneficiary counts, utilization, payments by county × service type;
  FWA-oriented. **Plug-in:** services CMS watches = higher audit/denial
  scrutiny priors.

### 7. Doctors & Clinicians (QPP/MIPS) public reporting
- https://data.cms.gov/provider-data/ ("Doctors and clinicians"; PY2023 file
  https://data.cms.gov/provider-data/dataset/7d6a-e7a6)
- NPI/group MIPS scores, demographics, facility affiliations; CSV + API;
  lags ~2 years. **Plug-in:** clinician peer benchmarks + affiliation mapping.

### 8. Medicare Physician & Other Practitioners — by Provider and Service (NPI × HCPCS)
- https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners/medicare-physician-other-practitioners-by-provider-and-service
- Per-clinician utilization/allowed/paid per code, API-filterable to NH or the
  pilot's NPIs. **Plug-in:** per-physician E/M mix outlier scoring (we already
  use the geography cut; this is the per-provider cut).

## Tier 2 — Low-friction request (days), NH-specific

### 9. NH CHIS Commercial Claims Public Use Data Set
- https://nhchis.com/DataAndReport/DataSets ; PUF dictionary:
  https://nhchis.com/DocumentDelivery/GetFile/15 ; request logs:
  https://nhchis.com/DataAndReport/PublicUseDataRequests
- The light-tier CHIS release (short request form, historically nominal/no
  cost) — materially easier than the Limited Use DUA. Contact:
  doi.healthcareanalytics@ins.nh.gov.
- **Plug-in:** bridge dataset for NH commercial benchmarks.

### 10. NH Uniform Healthcare Facility Discharge Data (UHFDDS) public-use file
- https://www.dhhs.nh.gov/programs-services/population-health/health-statistics-informatics/hospital-discharge-data
- Inpatient AND outpatient facility encounters; outpatient rows carry
  CPT/HCPCS + up to 4 modifiers, diagnoses, payer category, total charges (not
  paid). Nominal fee, standard release. Gap: no ED indicator.
- **Plug-in:** NH facility-side volume/charge benchmarks by CPT.

### 11. NHID annual hearing + insurance reports (CHIS-derived)
- 2025 report: https://www.insurance.nh.gov/sites/g/files/ehbemt861/files/inline-documents/sonh/nhid-annual-hearing-report-final-november-2025.pdf ;
  THCE: https://nhhealthcost.nh.gov/sites/default/files/media/2025-02/2024-final-nh-thce-report.pdf
- Premium/claims cost drivers, MLR by carrier, utilization trend, and
  external-review (appeal) statistics — CY2025: 119 external reviews, $503K
  recovered; earlier detail: ~24% overturn. PDFs only, hand-extraction.
- **Plug-in:** NH trend factors + the only public NH appeals numbers
  (small-n, directional).

### 12. Payer Transparency-in-Coverage MRFs (Anthem NH, Cigna, Point32, UHC)
- e.g. https://www.anthem.com/machine-readable-file/search/ ; aggregators:
  Serif Health, Turquoise ($).
- Negotiated in-network rates for every CPT for every NH provider NPI, per
  plan, monthly. Multi-TB — realistic path is targeted extraction of the
  pilot's NPIs for their top ~200 codes, or paying an aggregator.
- **Plug-in:** payer-specific expected allowed per CPT per contract — the core
  of underpayment detection. Engineering cost is the real price.

## Tier 3 — Expansion states (northern New England)

### 13. Maine: CompareMaine + MHDO dashboards
- https://www.comparemaine.org ; https://mhdo.maine.gov/tableau/data.cshtml ;
  https://www.maine.gov/oahc/hospital-payments-utilization-dashboards
- Median commercial total payments by procedure by named facility by payer
  (MHDO APCD; claims Jul 2024–Jun 2025, 17 payers — fresher than NH
  HealthCost). Tableau crosstab exports now; full files via MHDO request
  (rule 90-590 CMR 243 governs redistribution).
- Also the best episode-bundling methodology template.

### 14. Vermont: GMCB — VHCURES PUF, APCD Snapshot, Act 53 reports, VUHDDS PUF
- https://gmcboard.vermont.gov/DATA-AND-ANALYTICS/DATA-COLLECTION/vhcures-vermonts-all-payer-claims-database
- VHCURES PUF free via emailed pre-application (gmcb.data@vermont.gov,
  ~10 business days); Act 53 hospital report cards are real spreadsheets;
  VUHDDS PUF is a simple signed form. PUFs carry use agreements — read before
  embedding in product.

### 15. Massachusetts: CHIA public datasets (yes) / CompareCare (no)
- Relative Price (CY2023, Aug 2025, Excel databooks):
  https://www.chiamass.gov/relative-price-and-provider-price-variation/ ;
  TME/THCE: https://www.chiamass.gov/databases/payer-expenditure-utilization-metrics/
- CompareCare: display-only and found non-compliant/stale by the State Auditor
  (Jan 2025) — do not build on it.

### 16. Colorado CIVHC / Washington HealthCareCompare — methodology references
- https://civhc.org/get-data/public-data/ ; https://www.wahealthcarecompare.com
- Not expansion states; value is as product/methodology benchmarks.

## Tier 4 — Federal per-state slices

### 17. data.medicaid.gov State Drug Utilization — pharmacy only (per state × NDC ×
quarter, CSV/API). The medical-service equivalent (T-MSIS TAF OT with CPT) is
ResDAC/DUA-gated, not downloadable — for NH Medicaid medical rates the MMIS
fee schedule (item 3) is the practical source.

## Legal/terms traps (cross-cutting)
- **AMA CPT:** fee schedules, MRFs, and APCD outputs embed CPT descriptors —
  keep everything keyed to code numbers; our license covers our use.
- **Scraping:** nhhealthcost.nh.gov and nhchis.com 403 non-browser agents —
  use published files/forms. Tableau crosstab exports OK; check MHDO reuse
  terms for commercial products.
- **VHCURES/VUHDDS PUFs:** use agreements (no re-identification, citation,
  sometimes no commercial redistribution).
- **Hospital MRFs:** public by design (45 CFR 180) — the rule preempts access
  restrictions on the MRF itself; rates ≠ realized payment.

## Recommended actions
1. ✅ NH denial priors (done, live in the engine).
2. Engineering next: NH Medicaid fee schedule ingestion; PECOS/NPPES NH
   provider master; per-provider E/M mix from the by-Provider-and-Service PUF;
   1-2 NH hospital MRFs for the pilot's referral facilities.
3. Founder actions: submit the NH HealthCost Statewide Rates Report request
   form; the CHIS Public Use request; consider the UHFDDS request.
4. Later/budget: payer TiC extraction (or aggregator) for underpayment
   detection; expansion-state pulls when ME/VT/MA enter the roadmap.
