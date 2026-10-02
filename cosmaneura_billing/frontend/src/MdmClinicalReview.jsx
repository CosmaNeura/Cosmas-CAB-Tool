import React from "react";
import { clinicalCategoryLabels, clinicalSections, clinicalSectionSummary, readClinicalReview } from "./mdmClinicalReview.js";

const statuses = {counted: "Counted", duplicate: "Duplicate · not counted again", held: "Held · not counted"};
export default function MdmClinicalReview({review, encounter, original = false}) {
  let ledger;
  try { ledger = readClinicalReview(review, encounter); }
  catch { return <p role="alert" className="mt-3 text-sm text-amber-900">Clinical-review details could not be verified against this source. Review the documentation directly.</p>; }
  if (!ledger) return null;
  const count = ledger.problems.events.length + ledger.risk.events.length;
  return <details className="mt-4 rounded-xl border border-slate-200 bg-slate-50" aria-label="MDM clinical review">
    <summary className="cursor-pointer px-3 py-2 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-slate-700">Problems and management risk{original ? " · original pipeline" : ""} · {count} item{count === 1 ? "" : "s"}</summary>
    <div className="space-y-4 border-t border-slate-200 p-3 text-sm">
      <p className="text-xs text-slate-600">{original ? "Generation-time record; header edits do not change these engine findings. " : ""}Counted means included in one MDM element. It does not accept a code or establish the entire E/M level. Held complexity is not counted. Separately verified current clinical work may support medical necessity when documented total time supports the visit level.</p>
      {Object.entries(clinicalSections).map(([name, label]) => <section key={name} aria-label={label}>
        <h4 className="font-semibold">{label}</h4>
        <p className={`mt-1 text-xs ${ledger[name].evidence_complete ? "text-slate-600" : "text-amber-900"}`} role={!ledger[name].evidence_complete ? "alert" : undefined}>{clinicalSectionSummary(ledger[name])}</p>
        <ol className="mt-2 space-y-2">{ledger[name].events.map((event) => <li key={event.id} className="rounded-lg border border-slate-200 bg-white p-3">
          <p className="text-xs font-semibold text-slate-600">{statuses[event.status]}</p>
          <p className="mt-1 font-medium">{event.subject}</p>
          {event.category && <p className="mt-1 text-xs text-slate-600">{clinicalCategoryLabels[event.category]}</p>}
          <p className="mt-1 text-slate-700">{event.reason}</p>
          {!event.evidence_complete && <p className="mt-2 text-xs text-amber-800">Complete source evidence was not located.</p>}
          {event.evidence_spans.map((span, i) => <details key={i} className="mt-2 text-xs text-slate-600">
            <summary className="cursor-pointer focus-visible:outline focus-visible:outline-2 focus-visible:outline-slate-700">Source evidence · {span.source.replaceAll("_", " ")} · characters {span.loc.start}–{span.loc.end}</summary>
            <blockquote className="mt-2 whitespace-pre-wrap border-l-2 border-slate-300 pl-3 text-sm">{span.text}</blockquote>
          </details>)}
        </li>)}</ol>
        <p className="mt-2 text-xs text-slate-500">Rule {ledger[name].rule_version}</p>
      </section>)}
    </div>
  </details>;
}
