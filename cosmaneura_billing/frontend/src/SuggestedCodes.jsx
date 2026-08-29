import React, { useState } from "react";
import { AlertTriangle, Copy, FileCheck2, ShieldCheck } from "lucide-react";
import { visibleSuggestedCodes } from "./operationalDemoContract.js";

function Evidence({ span }) {
  return (
    <figure className="rounded-xl border border-emerald-200 bg-emerald-50 p-3">
      <figcaption className="flex flex-wrap items-center gap-1.5 text-xs font-medium text-emerald-800">
        <FileCheck2 size={13} aria-hidden="true" />Exact {span.source.replaceAll("_", " ")} span
        <code>[{span.loc.start}, {span.loc.end})</code>
      </figcaption>
      <blockquote className="mt-1 text-sm italic text-emerald-950">“{span.text}”</blockquote>
    </figure>
  );
}

function CodeCard({ row, copy }) {
  return (
    <article className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <code className="font-mono text-lg font-semibold">{row.code}</code>
            <span className="rounded-md bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-800 ring-1 ring-emerald-200">evidence-backed suggestion</span>
          </div>
          <p className="mt-1 text-sm text-slate-700">{row.description || row.label || "Short generic code label"}</p>
        </div>
        <button type="button" onClick={() => copy(row.code, `${row.code} copied — not sent.`)} aria-label={`Copy suggested code ${row.code}`} className="inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium hover:bg-slate-50">
          <Copy size={15} aria-hidden="true" />Copy
        </button>
      </div>
      {row.why && <p className="mt-3 text-sm text-slate-700"><strong>Why:</strong> {row.why}</p>}
      <div className="mt-3 space-y-2">{row.evidence_spans.map((span, index) => <Evidence key={`${span.source}-${span.loc.start}-${index}`} span={span} />)}</div>
      <p className="mt-3 text-xs text-slate-500">Provider verification is required before persistence or handoff.</p>
    </article>
  );
}

export default function SuggestedCodes({ payload, documents }) {
  const [notice, setNotice] = useState("");
  const rows = visibleSuggestedCodes(payload, documents);
  const total = Array.isArray(payload?.codes) ? payload.codes.length : 0;
  const clientWithheld = total - rows.length;
  const backendCounts = Object.entries(payload?.withheld_counts ?? {})
    .filter(([, count]) => Number.isInteger(count) && count > 0);
  const backendWithheld = backendCounts.reduce((sum, [, count]) => sum + count, 0);
  const all = rows.map((row) => row.code).join(" ");
  const copy = async (text, success) => {
    if (!text || !navigator.clipboard?.writeText) return setNotice("Copy unavailable. Nothing was sent.");
    try { await navigator.clipboard.writeText(text); setNotice(success); }
    catch { setNotice("Copy denied. Nothing was sent."); }
  };

  return (
    <section aria-labelledby="suggested-codes-heading" className="rounded-2xl border border-slate-200 bg-slate-50 p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="suggested-codes-heading" className="flex items-center gap-2 text-lg font-semibold"><ShieldCheck size={20} />Suggested codes</h2>
          <p className="mt-1 text-sm text-slate-600">Advisory billing guidance. Exact source evidence is required for every visible code.</p>
        </div>
        <span className="rounded-md bg-violet-50 px-2 py-0.5 text-xs font-medium text-violet-800 ring-1 ring-violet-200">local synthetic demo · default off</span>
      </div>
      {payload?.degraded ? (
        <p role="alert" className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950">No suggestions are shown because this result is degraded. Route it to manual review.</p>
      ) : rows.length ? (
        <div className="mt-4 grid gap-4 lg:grid-cols-2">{rows.map((row, index) => <CodeCard key={`${row.code}-${index}`} row={row} copy={copy} />)}</div>
      ) : (
        <p className="mt-4 rounded-xl border border-dashed border-slate-300 bg-white p-5 text-sm text-slate-600">No code satisfies the dormant, advisory, human-verification and exact-evidence contract. Nothing is available to copy.</p>
      )}
      {(clientWithheld > 0 || backendWithheld > 0) && (
        <p className="mt-4 flex gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <AlertTriangle size={16} className="shrink-0" />
          {backendWithheld > 0 && <>Backend withheld {backendWithheld} pipeline item{backendWithheld === 1 ? "" : "s"} ({backendCounts.map(([name, count]) => `${name.replaceAll("_", " ")}: ${count}`).join("; ")}). </>}
          {clientWithheld > 0 && <>Browser withheld {clientWithheld} returned code{clientWithheld === 1 ? "" : "s"} that failed the display contract.</>}
        </p>
      )}
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 pt-4">
        <p className="text-xs text-slate-600">Copying is local only; no EHR, claim, or payer write occurs.</p>
        <button type="button" disabled={!all} onClick={() => copy(all, "Evidence-backed codes copied — not sent.")} className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-slate-950 px-3 py-2 text-sm font-semibold text-white disabled:opacity-50"><Copy size={15} />Copy all visible codes</button>
      </div>
      <p className="mt-2 min-h-5 text-sm font-medium text-sky-800" role="status">{notice}</p>
    </section>
  );
}
