#!/usr/bin/env python3
"""Run the billing pipeline over a corpus of encounters (ACI-Bench).

This is the multi-encounter harness: it exercises extraction -> coding ->
evidence verification -> integrity -> guardrail -> denial-risk on every
encounter and prints a per-encounter line plus an aggregate summary.

    python scripts/run_corpus.py                    # offline plumbing check
    ANTHROPIC_API_KEY=... python scripts/run_corpus.py --limit 10   # real inference

Offline (no ANTHROPIC_API_KEY) the MockLLM only knows the demo encounter, so
extraction returns nothing and every visit levels at the floor with no
diagnoses — that run validates the plumbing, not the coding. With a key, this
is the engine's first real multi-encounter shakedown; expect it to surface
extraction and mapping issues (that's the point).
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cosmaneura_billing.corpus.acibench import load_acibench
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, Disposition


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="train", choices=["train", "valid"])
    ap.add_argument("--limit", type=int, default=5, help="encounters to run (0 = all)")
    ap.add_argument("--offset", type=int, default=0)
    args = ap.parse_args()

    live = bool(os.environ.get("ANTHROPIC_API_KEY"))
    print(f"inference: {'AnthropicLLM (live)' if live else 'MockLLM (offline plumbing check)'}")
    if not live:
        print("  -> offline mode validates the pipeline mechanics only; set "
              "ANTHROPIC_API_KEY for a real extraction/coding run.\n")

    bundles = load_acibench(args.split)
    if args.offset:
        bundles = bundles[args.offset:]
    if args.limit:
        bundles = bundles[:args.limit]

    orch = BillingOrchestrator()
    em_dist: Counter = Counter()
    n_dx = n_flags = n_evidence_removed = failures = 0

    for b in bundles:
        try:
            result = orch.process(b)
        except Exception as e:  # a corpus run should report, not die
            failures += 1
            print(f"{b.encounter_id:<28} ERROR: {type(e).__name__}: {e}")
            continue
        ws = result.biller_worksheet
        em = ws.em_code.code if ws.em_code else "-"
        em_dist[em] += 1
        dx = [d.code for d in ws.diagnoses]
        n_dx += len(dx)
        n_flags += len(ws.denial_flags)
        removed = sum(1 for line in ws.audit_trail if line.startswith("EVIDENCE-REMOVED"))
        n_evidence_removed += removed
        held = len(result.provider_card.declined_codes)
        print(f"{b.encounter_id:<28} EM={em:<7} dx={','.join(dx) or '-':<24} "
              f"flags={len(ws.denial_flags)} held={held} ev_removed={removed}")

    print("\n---- aggregate ----")
    print(f"encounters run     : {len(bundles)}  (errors: {failures})")
    print(f"E/M distribution   : {dict(sorted(em_dist.items()))}")
    print(f"captured dx total  : {n_dx}")
    print(f"denial flags total : {n_flags}")
    print(f"evidence removed   : {n_evidence_removed} (hallucinated/paraphrased quotes caught)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
