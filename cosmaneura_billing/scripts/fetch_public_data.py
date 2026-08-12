#!/usr/bin/env python3
"""Fetch the public data sources the engine can ingest (see docs/CAC_DATA_SOURCES.md).

Everything here is public data; run it from a network that can reach the hosts
(corporate/cloud sandboxes often block *.cms.gov — the Tuva and GitHub sources
usually remain reachable).

    python scripts/fetch_public_data.py --dest ./public_data [--group all]

Groups:
  terminology  Tuva Project terminology tables (S3): current-FY ICD-10-CM,
               HCPCS Level II, place of service. Apache-2.0 packaging of
               CMS public-domain code sets. Already bundled in the package;
               re-fetch to refresh.
  corpora      ACI-Bench + MTS-Dialog remaining splits (GitHub, CC-BY-4.0).
  cms          CMS NCCI PTP/MUE + MCD coverage download page pointers. The
               NCCI/MCD zips sit behind license click-throughs and rotating
               URLs, so this prints the exact pages + target filenames rather
               than scraping; the knowledge loaders ingest the extracted files
               directly (knowledge/ncci.py, knowledge/coverage.py).

CPT descriptors are NOT public — fetch those with scripts/fetch_cpt_zip.py
using your AMA credentials, then point CPT_DATA_DIR at the extracted files.
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

TUVA = "https://tuva-public-resources.s3.amazonaws.com/versioned_terminology/latest"
TERMINOLOGY = {
    "icd10cm_fy_current.csv.gz": f"{TUVA}/icd_10_cm.csv_0_0_0.csv.gz",
    "hcpcs_level_2.csv.gz": f"{TUVA}/hcpcs_level_2.csv_0_0_0.csv.gz",
    "place_of_service.csv.gz": f"{TUVA}/place_of_service.csv_0_0_0.csv.gz",
}

ACI = "https://raw.githubusercontent.com/wyim/aci-bench/main/data/challenge_data"
MTS = "https://raw.githubusercontent.com/abachaa/MTS-Dialog/main/Main-Dataset"
CORPORA = {
    "acibench_train.csv": f"{ACI}/train.csv",
    "acibench_valid.csv": f"{ACI}/valid.csv",
    "acibench_test1.csv": f"{ACI}/clinicalnlp_taskB_test1.csv",
    "acibench_test2.csv": f"{ACI}/clinicalnlp_taskC_test2.csv",
    "acibench_test3.csv": f"{ACI}/clef_taskC_test3.csv",
    "mts_dialog_train.csv": f"{MTS}/MTS-Dialog-TrainingSet.csv",
    "mts_dialog_valid.csv": f"{MTS}/MTS-Dialog-ValidationSet.csv",
}

CMS_POINTERS = """\
CMS sources (license click-through / rotating URLs -> download by hand or from
a network with cms.gov access):

1. NCCI PTP + MUE (practitioner):
   https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-procedure-procedure-ptp-edits
   https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-medically-unlikely-edits-mues
   -> extract the practitioner TXT/CSV files, then:
      NCCIEdits.from_files(ptp_path=..., mue_path=...)

2. Medicare Coverage Database (weekly full dump; Billing & Coding Articles):
   https://www.cms.gov/medicare-coverage-database/downloads/downloads.aspx
   -> extract the article tables, then:
      CoverageDB.from_mcd_tables(hcpc_file=..., covered_file=..., dont_support_file=..., article_file=...)

3. ICD-10-CM FY release (authoritative annual files, if you prefer CMS-direct
   over the Tuva packaging): https://www.cms.gov/medicare/coding-billing/icd-10-codes
"""


def fetch(url: str, dest: Path) -> bool:
    try:
        print(f"  {dest.name:<28} <- {url}")
        with urllib.request.urlopen(url, timeout=120) as r:
            dest.write_bytes(r.read())
        return True
    except Exception as e:
        print(f"  {dest.name:<28} FAILED: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="public_data")
    ap.add_argument("--group", default="all",
                    choices=["all", "terminology", "corpora", "cms"])
    args = ap.parse_args()
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    failures = 0
    if args.group in ("all", "terminology"):
        print("[terminology] Tuva Project tables (CMS public-domain code sets):")
        for name, url in TERMINOLOGY.items():
            failures += 0 if fetch(url, dest / name) else 1
    if args.group in ("all", "corpora"):
        print("[corpora] ACI-Bench / MTS-Dialog (CC-BY-4.0):")
        for name, url in CORPORA.items():
            failures += 0 if fetch(url, dest / name) else 1
    if args.group in ("all", "cms"):
        print(CMS_POINTERS)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
