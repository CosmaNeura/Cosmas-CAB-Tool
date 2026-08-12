#!/usr/bin/env python3
"""
Distill the CMS "Medicare Physician & Other Practitioners - by Provider and
Service" file (multi-GB) into a compact E/M benchmark by specialty.

We stream the CSV straight out of the zip (no full extraction), keep only the
office/outpatient E/M codes (99202-99215), and aggregate by
(specialty, place_of_service, code): provider count, total services, total
beneficiaries, and the services-weighted mean Medicare allowed amount. CPT
descriptors are intentionally dropped -- we keep code numbers and numeric stats
only (the file ships an AMA CPT license precisely because of those descriptors).

    python scripts/build_em_benchmark.py \
        "Medicare_Physician___Other_Practitioners_-_by_Provider_and_Service.zip" \
        cosmaneura_billing/benchmarks/data/em_benchmark_by_specialty.csv

Output is tiny (a few thousand rows) and is what the benchmark module loads.
"""
import csv
import io
import sys
import zipfile

EM_CODES = {f"992{n:02d}" for n in range(2, 6)} | {f"992{n:02d}" for n in range(11, 16)}
# -> {'99202','99203','99204','99205','99211','99212','99213','99214','99215'}


def find_inner_csv(zf: zipfile.ZipFile) -> str:
    for name in zf.namelist():
        if name.lower().endswith(".csv"):
            return name
    raise SystemExit("No CSV found inside the zip.")


def build(zip_path: str, out_path: str) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        inner = find_inner_csv(zf)
        agg: dict[tuple[str, str, str], list[float]] = {}   # (specialty,place,code)->[nprov,svcs,benes,alw*svcs]
        with zf.open(inner, "r") as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8", errors="ignore", newline="")
            header = next(csv.reader([stream.readline()]))
            ix = {col: i for i, col in enumerate(header)}
            c_spec = ix["Rndrng_Prvdr_Type"]; c_code = ix["HCPCS_Cd"]
            c_pos = ix["Place_Of_Srvc"]; c_svc = ix["Tot_Srvcs"]
            c_ben = ix["Tot_Benes"]; c_alw = ix["Avg_Mdcr_Alowd_Amt"]
            seen = 0
            for line in stream:
                # cheap pre-filter: skip the ~99% of lines with no E/M code
                if "9920" not in line and "9921" not in line:
                    continue
                row = next(csv.reader([line]))
                if len(row) <= c_alw:
                    continue
                code = row[c_code].strip()
                if code not in EM_CODES:
                    continue
                try:
                    svcs = float(row[c_svc]); benes = float(row[c_ben]); alw = float(row[c_alw])
                except ValueError:
                    continue
                key = (row[c_spec].strip(), row[c_pos].strip(), code)
                a = agg.get(key)
                if a is None:
                    agg[key] = [1.0, svcs, benes, alw * svcs]
                else:
                    a[0] += 1; a[1] += svcs; a[2] += benes; a[3] += alw * svcs
                seen += 1
                if seen % 200000 == 0:
                    print(f"  ...{seen:,} E/M rows aggregated", file=sys.stderr)

    import os
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(["specialty", "place_of_service", "hcpcs", "n_providers",
                    "total_services", "total_benes", "mean_allowed_amt"])
        for (spec, pos, code), (nprov, svcs, benes, alwsum) in sorted(agg.items()):
            w.writerow([spec, pos, code, int(nprov), int(svcs), int(benes),
                        round(alwsum / svcs, 2) if svcs else 0])
    print(f"wrote {out_path}: {len(agg)} (specialty,place,code) rows", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    build(sys.argv[1], sys.argv[2])
