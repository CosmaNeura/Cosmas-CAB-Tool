"""
Load the iCliniq dialogue corpus into the {id, text, metadata} shape the
dialogue RAG indexes.

The prototype's ``iCliniq.feather`` (and the equivalent ``med_chat_QA_data.csv``)
has columns:
    input            -- the patient's question/presentation   (we embed this)
    answer_icliniq   -- the real clinician's reply             (kept for grounding)
    answer_chatgpt, answer_chatdoctor -- model answers (kept as metadata)

We embed the patient ``input`` so that retrieval surfaces similar prior
presentations; the clinician answer rides along in metadata so the leading-
questions prompt can ground on how a real clinician responded.

    from cosmaneura_billing.corpus import load_icliniq
    corpus = load_icliniq("iCliniq.feather", limit=2000)

.feather is read via pandas/pyarrow; .csv is read with the stdlib (no pandas
needed). Licensing note: confirm the iCliniq/ChatDoctor dataset's terms permit
commercial use before production — this loader doesn't redistribute the data.
"""
from __future__ import annotations

import csv as _csv
from pathlib import Path
from typing import Any, Iterator

_TEXT_FIELDS = ("input", "question", "patient", "description", "text")
_ANSWER_PREF = ("answer_icliniq", "answer", "output", "response", "answer_chatdoctor")


def _pick(columns, candidates, contains=False):
    cols = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand in cols:
            return cols[cand]
    if contains:
        for cand in candidates:
            for lc, orig in cols.items():
                if cand in lc:
                    return orig
    return None


def _iter_feather(path: Path) -> Iterator[dict]:
    try:
        import pandas as pd
    except ImportError as e:  # pragma: no cover
        raise ImportError("Reading .feather needs pandas + pyarrow (pip install pandas pyarrow). "
                          "Alternatively point load_icliniq at the .csv.") from e
    df = pd.read_feather(path)
    cols = list(df.columns)
    text_col = _pick(cols, _TEXT_FIELDS, contains=True)
    ans_col = _pick(cols, _ANSWER_PREF, contains=True)
    if text_col is None:
        raise ValueError(f"No patient-text column found in {cols}")
    for i, row in df.iterrows():
        yield {"i": int(i), "text": str(row[text_col]),
               "answer": (str(row[ans_col]) if ans_col else "")}


def _iter_csv(path: Path) -> Iterator[dict]:
    with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
        reader = _csv.DictReader(fh)
        cols = reader.fieldnames or []
        text_col = _pick(cols, _TEXT_FIELDS, contains=True)
        ans_col = _pick(cols, _ANSWER_PREF, contains=True)
        if text_col is None:
            raise ValueError(f"No patient-text column found in {cols}")
        for i, row in enumerate(reader):
            yield {"i": i, "text": str(row.get(text_col, "")),
                   "answer": str(row.get(ans_col, "")) if ans_col else ""}


def load_icliniq(path: str | Path, limit: int | None = None,
                 id_prefix: str = "icliniq") -> list[dict[str, Any]]:
    path = Path(path)
    it = _iter_feather(path) if path.suffix.lower() == ".feather" else _iter_csv(path)
    out: list[dict[str, Any]] = []
    for rec in it:
        text = (rec["text"] or "").strip()
        if not text:
            continue
        out.append({
            "id": f"{id_prefix}_{rec['i']:06d}",
            "text": text,
            "answer": rec["answer"].strip(),
            "source": "icliniq",
        })
        if limit and len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "iCliniq.feather"
    corpus = load_icliniq(p, limit=5)
    print(f"loaded {len(corpus)} sample items from {p}")
    for c in corpus[:2]:
        print(f"\n  id={c['id']}")
        print(f"  text  : {c['text'][:120]}…")
        print(f"  answer: {c['answer'][:120]}…")
