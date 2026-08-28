# CPT-Bearing Claims Datasets & Tooling — Research Report (July 2026)

> Scope note (2026-08-27): references to NPI-bearing public datasets below are
> for benchmarks and research. They do not authorize NPI-gated onboarding,
> provider authentication/credentialing by NPI, 837P generation, or claim
> submission. See `NPI_AND_CLAIM_SUBMISSION_SCOPE.md`.

**Bottom line first:** True claim-level adjudicated data with denial outcomes
(CARC/RARC) is essentially **not publicly available anywhere** — payers treat
remittance data as proprietary, and APCDs deliberately exclude denied claims.
The closest public substitutes are (a) NH CHIS / state APCD limited-use files
(paid claims with CPT + allowed amounts — calibrates fees, not denials),
(b) Medicare PSPS files (aggregate **denied service counts by HCPCS code** —
the only public per-CPT denial signal), and (c) CMS Marketplace TC-PUF
(issuer-level denial rates). Plan denial-risk calibration around those three
plus the pilot practice's own 835 remits.

---

## TIER 1 — Adjudicated claim-level data (paid/allowed outcomes)

### 1. NH CHIS — Commercial Limited Use Data Set (pilot home state — top priority)
- **URL:** https://nhchis.com/ ; datasets: https://nhchis.com/DataAndReport/DataSets ;
  LUDS data dictionary v5: https://nhchis.com/DocumentDelivery/GetFile/25
  (site blocks automated fetchers; browse manually)
- **What it is:** New Hampshire's APCD, created under RSA 420-G:11-a, jointly
  run by **NH DHHS and the NH Insurance Department**. **Milliman** has been the
  contracted data manager since 2012 (MedInsight warehouse; NHpreprocessor does
  intake/de-identification and hosts nhchis.com). UNH's Institute for Health
  Policy & Practice (https://chhs.unh.edu/institute-health-policy-practice/focal-areas/health-analytics-informatics)
  is the state's analytics partner and a good ally/intermediary.
- **Contents:** Claim-level de-identified commercial (plus Medicaid/Medicare in
  some releases) medical, pharmacy, dental claims + eligibility back to
  **2005**. Medical claims include **CPT/HCPCS procedure codes** (Procedure
  Code links to REF_PROC_CODE via CPT code), ICD diagnoses, payer, and
  **paid/allowed/charged amount fields**. **No denied claims** — APCD intake is
  paid/final-action claims only.
- **Access tiers (Admin Rule He-W 950):** (1) Commercial Claims **Public Use**
  Data Set (request form, low barrier — de-identified claim-level extract;
  IHME/GHDx has redistributed 2005-2010 vintages:
  https://ghdx.healthdata.org/record/united-states-new-hampshire-comprehensive-health-care-information-system-commercial-claims-4);
  (2) Commercial **Limited Use** Data Set (application + DUA, requester
  justifies each data element); (3) **Confidential Research** Data Set
  (IRB-level review). Fulfilled-request logs are public:
  https://nhchis.com/DataAndReport/LimitedUseDataRequests and
  .../PublicUseDataRequests — review these to see what commercial requestors
  have gotten approved.
- **Cost/timeline:** Fees are per-request cost recovery, not a published
  schedule (NH's separate hospital discharge PUF is $325/yr for scale).
  Realistic timeline: weeks (public use) to 3-6 months (limited use with DUA
  negotiation). Who qualifies: purpose-based, not academic-only — NH statute
  explicitly lists insurers, employers, providers, purchasers as intended
  users, which is favorable for a physician-group pilot vendor.
- **Gobeille effect:** Post-*Gobeille v. Liberty Mutual* (2016), self-funded
  ERISA plan submission is voluntary; NHID amended its submission rules.
  Commercial data now skews toward fully-insured plans — expect a coverage gap
  for large-employer self-funded lives
  (background: https://nashp.org/gobeille-v-liberty-mutual-everything-you-need-to-know/).
- **Plug-in:** NH-specific fee benchmarks (allowed amounts by CPT by payer) and
  payer-mix-aware E/M distribution baselines for the exact pilot market.
- **Trap:** He-W 950 DUA bars re-release and use of data "for their own benefit
  or advantage" beyond the approved purpose — get the commercial use case
  approved explicitly in the application; no re-identification attempts.

### 2. Medicare Carrier LDS (Physician/Supplier SAF, 5% sample) — via CMS/ResDAC
- **URLs:** https://www.cms.gov/data-research/cms-data/data-available-researchers/limited-data-set-lds-files ;
  https://resdac.org/cms-data/files/carrier-ffs ;
  pricing: https://www.cms.gov/files/document/2027-lds-file-pricing-update.pdf
- **Contents:** Claim-line-level Medicare Part B professional claims:
  **HCPCS/CPT + modifiers, ICD-10 diagnoses, submitted/allowed/paid amounts**,
  provider specialty, place of service. Beneficiary-level (encrypted IDs), real
  adjudicated claims.
- **Cost/DUA:** **~$2,040/year of data for the 5% Carrier SAF**; LDS DUA +
  research application. Non-academic requestors CAN get LDS files, but the
  stated purpose must be *research*; for product development, CMS routes
  for-profits to the **Innovator/VRDC track** (budget tens of thousands/year).
  Timeline ~1-3 months for LDS. Ask ResDAC (resdac@umn.edu) about the innovator
  pathway before ordering.
- **Plug-in:** Best affordable real-claims corpus for fee benchmarks, code-pair
  co-occurrence, and E/M level distributions by specialty; Medicare-only, no
  denials.
- **Trap:** research-purpose DUA, no re-identification, minimum cell size 11 in
  outputs; building a commercial scoring model directly on LDS under a
  "research" DUA is a compliance gray zone.

### 3. Colorado APCD (CIVHC) — most startup-friendly full APCD
- **URLs:** https://civhc.org/access-fees/ ; contact ColoradoAPCD@civhc.org
- Claim-level commercial + Medicaid + Medicare Advantage with **CPT/HCPCS and
  allowed amounts**. Published tiered fees (for-profit pricing exists); custom
  claim-level extracts historically five figures; ~3-6 months. Known to work
  with vendors. No denials.

### 4. Massachusetts APCD (CHIA)
- **URLs:** https://www.chiamass.gov/request-ma-apcd-data
- Deep claim-level commercial/Medicaid with CPT, payer, allowed/paid amounts.
  Requests must show benefit to Commonwealth residents — harder sell for a
  NH-focused engine; relevant because many NH patients cross into MA systems.
  Timeline 4-9 months. Other researcher-usable APCDs: VA (VHI), ME
  (MHDO/Onpoint), UT — directory: https://www.apcdcouncil.org/

---

## TIER 1.5 — The only public *denial* signals

### 5. Medicare PSPS — aggregate denials by CPT code (highest value-to-cost)
- **URLs:** free PUF: https://data.cms.gov/summary-statistics-on-use-and-payments/physiciansupplier-procedure-summary ;
  unsuppressed LDS: https://www.cms.gov/data-research/cms-data/data-available-researchers/limited-data-set-lds-files/physician-supplier-procedure-summary-psps-limited-data-set-lds
- **Contents:** Annual summary of ALL Part B carrier claims by **HCPCS code ×
  modifier × specialty × place of service**, with submitted services/charges,
  allowed services/charges, **denied services and denied charges**, and payment
  amounts. Aggregate, not claim-level; free (PUF) or cheap (LDS).
- **Plug-in:** the denial-risk prior — per-CPT × specialty × POS Medicare
  denial rates to seed the model before practice-specific 835 data accumulates.

### 6. CMS Marketplace Transparency in Coverage PUF (TC-PUF)
- **URLs:** https://www.cms.gov/marketplace/resources/data/public-use-files ;
  PY2026 dictionary: https://www.cms.gov/files/document/transparency-coverage-puf-datadictionary-py26.pdf
- Free annual PUF: claims received/denied, denial reasons by broad category,
  appeals and overturn rates, **per issuer per state**. No CPT, no claim level.
- **Plug-in:** payer-level denial-propensity feature (some issuers deny 2%,
  some 40%+).

### 7. Honest gap statement
Claim-level public data with CARC/RARC outcomes **does not exist**; T-MSIS TAF
explicitly excludes denied final-action claims
(https://resdac.org/cms-data/files/taf-ot), and APCDs exclude denials by
design. The real training data will be the pilot group's own **835
remittances** (see X12 parsers below). GitHub "claim denial prediction" repos
are toy projects on fabricated data — pipeline scaffolding only. CARC/RARC
lists are freely viewable at https://x12.org/codes.

---

## TIER 2 — Price/rate data by CPT (free) and non-outcome claims

### 8. Transparency in Coverage payer MRFs — negotiated rates by CPT, free
- Spec: https://github.com/CMSgov/price-transparency-guide ; every major payer
  posts monthly JSON (tens of TB per payer). **Negotiated rates and OON allowed
  amounts by CPT/HCPCS × NPI × payer** — exact contracted prices, no outcomes.
- **Plug-in:** payer-specific expected-allowed-amount benchmarks for NH
  providers (underpayment flagging).

### 9. CMS Hospital Price Transparency MRFs
- ~6,000 hospital standard-charge files; community pipeline:
  https://github.com/jakegussler/hospital-price-transparency-analytics (active
  2026); DoltHub hosts assembled databases. Facility-side — secondary for
  professional billing.

### 10. Medicare Physician & Other Practitioners PUF (by Provider and Service)
- https://data.cms.gov/ — free, provider-NPI-level annual utilization and
  average allowed/paid per HCPCS code, incl. every E/M code. Adds peer-group
  E/M distribution benchmarks by specialty and state (NH) to the benchmarks
  module already in the engine.

### 11. HCUP SASD / SEDD (AHRQ)
- https://hcup-us.ahrq.gov/databases.jsp — encounter-level state ambulatory
  surgery and ED records with **CPT/HCPCS**; charges only, no paid/allowed, no
  denials. ~$200-$3,500 per state-year; online DUA training; weeks. NH
  participates. **Plug-in:** CPT/ICD co-occurrence patterns for testing.

### 12. CMS Synthetic Medicare RIF files (modern SynPUF replacement)
- https://data.cms.gov/collection/synthetic-medicare-enrollment-fee-for-service-claims-and-prescription-drug-event
- Synthea-generated claims for 8,671 synthetic beneficiaries in real RIF layout
  incl. carrier claims with HCPCS/CPT-format codes and payment fields; free, no
  DUA. Also DE-SynPUF (2008-2010) and BCDA sandbox synthetic FHIR EOBs
  (https://bcda.cms.gov/). **Plug-in:** pipeline/schema testing that mirrors
  real LDS layout — de-risks the LDS purchase.

### 13. Synthea (MITRE) — https://github.com/synthetichealth/synthea
- Apache-2.0, active. Synthetic encounters/claims (FHIR/CSV/RIF exporters);
  coding largely SNOMED, CPT thin — test-harness generator, not CPT source.

### 14. MIMIC-IV `hcpcsevents` — https://physionet.org/content/mimiciv/
- Real hospital data with an HCPCS/CPT-coded events table; credentialed DUA;
  inpatient-only, no payer/paid amounts — marginal, listed for completeness.

---

## TIER 3 — GitHub CPT tooling (maintenance/license assessed)

### 15. TiC MRF parsers
- **dolthub mrfutils** — https://github.com/dolthub/data-analysis/tree/main/transparency-in-coverage/python/mrfutils —
  Python, filter/flatten payer MRFs by billing code + NPI; de facto reference.
- **danielchalef/mrfparse** — https://github.com/danielchalef/mrfparse — Go,
  fast; **archived 2023** — fork if used.
- **CMSgov/price-transparency-guide** — official schema, actively maintained.
  Serif Health's MRF primer:
  https://www.serifhealth.com/blog/a-primer-on-mrf-structure-how-to-find-a-rate

### 16. X12 837/835 parsers (for the practice's own remits — the real denial data)
- **azoner/pyx12** — BSD, long-standing Python HIPAA X12 validator/parser.
- **keiron-stoddart/edi-835-parser** — simple Python 835 parser — extracts CPT,
  paid amounts, and CARC adjustment segments.
- **databricks-industry-solutions/x12-edi-parser** — Spark-scale, active 2026
  (check Databricks license terms).
- **LinuxForHealth/x12** — Pydantic-based 837/835 (Apache-2.0), low activity.
- **BerryWorksSoftware/edireader** — Java, mature. Niche: **Query-farm/vgi-x12**
  (X12 in DuckDB SQL, Rust, June 2026).

### 17. NCCI edit implementations — thin; use CMS files directly (already bundled
in this engine). GitHub implementations (scrubcheck, Policy2Rule) are
hobby-grade — design ideas only. Medicaid NCCI files:
https://www.cms.gov/medicare/coding-billing/ncci-medicaid/medicaid-ncci-edit-files

### 18. LCD / coverage tooling
- **rohanpande001/cms-lcd-service** — FastAPI wrapper over the official CMS
  Coverage API (https://api.coverage.cms.gov, free) returning CPT + ICD-10
  medical-necessity pairs — maps exactly to our coverage module; tiny/new.

### 19. Code-system utilities and RVU/fee data
- CMS PFS RVU files (PPRRVU + GPCI + CF) free at
  https://www.cms.gov/medicare/payment/fee-schedules — CPT descriptors inside
  carry the AMA limited license; our license covers use. Open-source RVU
  calculators essentially don't exist — build in-house.
- **evanperez55/dbt-health-codes** — MIT dbt seeds for ICD-10/HCPCS/
  **CARC-RARC**/POS/DRG, deliberately CPT-free; **archived** — fork the
  CARC/RARC seed approach.
- NLM Clinical Table Search serves HCPCS Level II via free API; it does NOT
  serve CPT — that absence is the licensing signal.

---

## Legal traps — summary
1. **APCD DUAs prohibit commercial re-use beyond the approved purpose**
   (NH He-W 950: no re-release, no use "for their own benefit or advantage";
   MA requires Commonwealth-resident benefit). Declare the billing-engine use
   case in the application; don't launder research extracts into product
   features.
2. **CPT copyright:** avoid dependencies that redistribute CPT descriptors —
   flagged: MyMedi-AI/mymedi-ai-mcp-server, abarrington8/AAPC-Scrape, and most
   "CPT lookup" repos. Our AMA license covers *our* use, not theirs. HCPCS
   Level II, ICD-10, CARC/RARC, NCCI tables are safe (public/government works).
3. **CMS LDS:** research-purpose DUA, no re-identification, min cell size 11;
   commercial product development belongs in the Innovator/VRDC track — ask
   ResDAC first.
4. **Re-identification prohibitions** apply to all Tier 1-2 items; NH is a
   small state — small-cell risk is real in NH CHIS outputs.
5. **Gobeille gap:** APCD commercial data under-represents self-funded ERISA
   plans post-2016 — bias commercial fee benchmarks accordingly.

## Recommended sequence for the pilot
1. **Free, this week:** PSPS PUF (denial priors by CPT), Marketplace TC-PUF
   (payer denial rates), Medicare Physician & Other Practitioners PUF (NH E/M
   distributions), Medicaid NCCI files, CMS Synthetic RIFs (pipeline tests).
2. **~$2-6k, 1-3 months:** Medicare Carrier LDS 5% (2-3 years) via ResDAC —
   clarify innovator-track question up front.
3. **3-6 months, in parallel:** NH CHIS Limited Use application (start with the
   fulfilled-request logs + a UNH IHPP conversation); CIVHC quote as the
   commercial-payer fallback.
4. **Ongoing:** parse the pilot group's own 835s (pyx12 / edi-835-parser) —
   the only CARC/RARC-labeled training data available anywhere.
