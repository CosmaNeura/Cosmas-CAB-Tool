#!/usr/bin/env python3
"""
Turn a coder-feedback log into (1) a human-readable report, (2) a machine-readable
tuning backlog, and (3) a gold set for the validation regression suite.

    python scripts/analyze_feedback.py coder_feedback.jsonl --out build/

Writes build/tuning_signals.json and build/gold.jsonl. With no --out, prints the
report only. This is the bridge from "coders corrected the engine" to "the engine
changed": the tuning_signals are the prioritized rule/prompt backlog, the gold set
is the regression data.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cosmaneura_billing.coder_feedback import FeedbackStore, report, tuning_signals, export_gold


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", help="path to coder_feedback.jsonl")
    ap.add_argument("--out", help="output directory for tuning_signals.json + gold.jsonl")
    args = ap.parse_args()

    feedbacks = FeedbackStore(args.log).load()
    if not feedbacks:
        print(f"No feedback records found in {args.log}.")
        return 1

    print(report(feedbacks))

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "tuning_signals.json"), "w", encoding="utf-8") as fh:
            json.dump(tuning_signals(feedbacks), fh, indent=2)
        with open(os.path.join(args.out, "gold.jsonl"), "w", encoding="utf-8") as fh:
            for row in export_gold(feedbacks):
                fh.write(json.dumps(row) + "\n")
        print(f"\nwrote {args.out}/tuning_signals.json and {args.out}/gold.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
