# Bundled CMS Medicare Coverage Database — Billing & Coding Articles

Source: CMS MCD full export (`current_article.zip`), downloaded from
https://www.cms.gov/medicare-coverage-database/downloads/downloads.aspx
(export dated 2026-07-20). The MCD acceptance notice applies AMA terms to CPT
codes, descriptions, and other data and does not grant a general commercial
training license. **CPT descriptor columns were stripped before bundling**;
article IDs, the minimum code relationships, titles, and status remain for
entitlement-bound runtime coverage checks only. Do not train, embed, distill,
or redistribute this bundle beyond the organization's applicable authorization.

**Scope: filtered to National Government Services (NGS) — the pilot group's
MAC (Jurisdiction K, which covers New Hampshire).** 109 current articles,
350 HCPCS links, 4,145 covered-diagnosis rows, 11 non-covered rows.

Files (matched by name by `knowledge/coverage.py::CoverageDB.from_bundled()`):

- `article_x_hcpc_code.csv.gz` — article ↔ CPT/HCPCS code links
- `article_x_icd10_covered.csv.gz` — diagnoses that support necessity
- `article_x_icd10_noncovered.csv.gz` — diagnoses that explicitly do not
- `article.csv.gz` — article id, title, status

## Refreshing / changing jurisdiction

CMS refreshes the export weekly. To rebuild (or target a different MAC):
download `current_article.zip`, extract the CSV table set, find the target
contractor's IDs in `contractor.csv` (match on `contractor_bus_name`), select
the `article_id`s for those contractors from `article_x_contractor.csv`, and
write the four tables above filtered to those articles, keeping the same
columns. Serving patients across several jurisdictions? Bundle each MAC's
tables in separate directories and construct `CoverageDB.from_bundled(dir)`
per payer jurisdiction.
