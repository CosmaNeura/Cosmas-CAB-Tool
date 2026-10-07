"""Explicit offline build step for public data; default invocation is inert.

No application, Settings instance, environment snapshot, database, or model is
created. Only the existing catalog loader and index function compute meanings.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-script-sha256")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"state": "inert", "generated": False}))
        return
    source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if args.expected_script_sha256 != source_hash:
        raise ValueError("generator source mismatch")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from cosmaneura_billing.knowledge import icd10_index_cache as cache
    from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR, _description_index
    from cosmaneura_billing.knowledge import diagnosis_terms

    if cache.CACHE.exists() or cache.GENERATOR_SHA256 != source_hash:
        raise ValueError("cache exists or generator pin differs")
    if hashlib.sha256(cache.CATALOG.read_bytes()).hexdigest() != cache.CATALOG_SHA256:
        raise ValueError("catalog source mismatch")
    if (hashlib.sha256(Path(diagnosis_terms.__file__).read_bytes()).hexdigest() != cache.TERMS_SHA256
            or diagnosis_terms.VERSION != cache.TERMS_VERSION):
        raise ValueError("terminology source mismatch")
    started = time.monotonic()
    # Reuse the exact effective-catalog loader without composing runtime settings.
    catalog = ICD10CCSR.__new__(ICD10CCSR)
    catalog._current = {}
    catalog._load_current(cache.CATALOG)
    entries = tuple((raw, desc) for raw, (billable, desc) in catalog._current.items() if billable)
    index = _description_index(entries)
    document = {"format": cache.FORMAT, "generatorVersion": cache.GENERATOR_VERSION,
                "generatorSha256": source_hash, "catalogSha256": cache.CATALOG_SHA256,
                "termsSourceSha256": cache.TERMS_SHA256, "termsVersion": cache.TERMS_VERSION,
                "entriesSha256": cache.entries_sha256(entries), "entryCount": len(entries),
                "rows": [[list(positive), sorted(negative), list(raws)]
                         for (positive, negative), raws in index.items()]}
    body = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(body) > cache.MAX_DECOMPRESSED:
        raise ValueError("generated index too large")
    output = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0, compresslevel=9) as stream:
        stream.write(body)
    blob = output.getvalue()
    decoded = cache.decode_index(blob, entries)
    if tuple(decoded.items()) != tuple(index.items()):
        raise ValueError("full index round trip mismatch")
    # Exclusive output: never overwrite a prior generated artifact or runtime data.
    with cache.CACHE.open("xb") as stream:
        stream.write(blob)
    print(json.dumps({"state": "generated_verified_unpinned", "path": str(cache.CACHE),
                      "cacheSha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob),
                      "decompressedBytes": len(body), "entries": len(entries), "keys": len(index),
                      "ambiguousKeys": sum(len(raws) > 1 for raws in index.values()),
                      "entriesSha256": document["entriesSha256"],
                      "indexJsonSha256": hashlib.sha256(body).hexdigest(),
                      "fullOrderedIndexRoundTripEqual": True,
                      "elapsedSeconds": round(time.monotonic() - started, 6)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"state": "generation_failed", "errorType": type(exc).__name__}))
        raise SystemExit(1) from None
