"""Build one deterministic public-data derivative; inert unless explicitly run."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import resource
import sys

PACKAGE = Path(__file__).resolve().parents[1] / "cosmaneura_billing"
SOURCE = PACKAGE / "knowledge" / "ncci.py"
DATA = PACKAGE / "data" / "ncci"
OUTPUT = DATA / "catalog_practitioner_2026q3.v1.json.gz"
GENERATOR = Path(__file__).resolve()
GENERATOR_VERSION = "cosmaneura.ncci-compiler.v1"
SOURCES = {
    "ptp_practitioner_2026q3_v322.tsv.gz": "6073decceb4fbe0ea0dde36e5306c738faa7f34d3f9be5c1672fc3f38c9a8dc0",
    "mue_practitioner_2026q3.tsv.gz": "61fafdf8ae348d9a32cb5c13eaf556119e6840772e536b6696c28c4f7271175a",
}

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def offline(event, args):
    if event.startswith("socket.") or event.startswith("subprocess.") or event in {
            "os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.posix_spawnp"}:
        raise RuntimeError("catalog_generation_offline_boundary")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--source-sha256")
    options = parser.parse_args()
    if not options.execute:
        print("inert; no catalog import, generation or external request")
        return
    assert options.source_sha256 and sha(SOURCE) == options.source_sha256
    assert not OUTPUT.exists(), "fresh generated artifact required; never overwrite"
    assert {name: sha(DATA / name) for name in SOURCES} == SOURCES
    # The separately reviewed parent wrapper also enforces a 180-second wall
    # deadline and kills/reaps this owned process on timeout.
    resource.setrlimit(resource.RLIMIT_CPU, (110, 115))
    sys.addaudithook(offline)
    spec = importlib.util.spec_from_file_location("compiled_ncci_source", SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    edits = module.NCCIEdits()
    ptp_rows = edits.load_ptp(DATA / "ptp_practitioner_2026q3_v322.tsv.gz")
    mue_rows = edits.load_mue(DATA / "mue_practitioner_2026q3.tsv.gz")
    assert ptp_rows == edits._pair_count == 1728585
    assert mue_rows == len(edits._mue) == 15162
    codes = set(edits._codes)
    assert all(type(code) is str and code == module._norm(code) for code in codes)
    assert all(a in codes and type(values) is dict and values and all(
        b in codes and type(modifier) is str and len(modifier) == 1
        for b, modifier in values.items()) for a, values in edits._ptp.items())
    assert all(code in codes and type(value) is int for code, value in edits._mue.items())
    data = {"schemaVersion": "cosmaneura.ncci-practitioner.v1",
        "normalizationVersion": "remove-dot-strip-upper.v1",
        "build": {"generatorVersion": GENERATOR_VERSION,
            "generatorSha256": sha(GENERATOR), "loaderSha256": sha(SOURCE)}, "sources": SOURCES,
        "counts": {"ptpLoadedRows": ptp_rows, "ptpPairs": edits._pair_count,
            "mueLoadedRows": mue_rows, "mueCodes": len(edits._mue), "codeStrings": len(codes)},
        "codes": sorted(codes), "ptp": edits._ptp, "mue": edits._mue}
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    assert len(raw) <= 64 * 1024 * 1024
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=9) as stream:
        stream.write(raw)
    encoded = output.getvalue()
    assert len(encoded) <= 16 * 1024 * 1024
    with OUTPUT.open("xb") as stream:
        stream.write(encoded)
    print(json.dumps({"schemaVersion": "cab.ncci.compiled-generation.v1", "output": str(OUTPUT),
        "sourceSha256": sha(SOURCE), "sourceDataSha256": SOURCES, "build": data["build"],
        "cacheSha256": hashlib.sha256(encoded).hexdigest(), "compressedBytes": len(encoded),
        "jsonBytes": len(raw), "counts": data["counts"], "vendorRequests": 0,
        "applicationImports": False, "deterministicGzipMtime": 0}, sort_keys=True))

if __name__ == "__main__":
    main()
