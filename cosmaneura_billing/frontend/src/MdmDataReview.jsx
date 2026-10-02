import React from "react";
import { dataActionLabels, readDataReview } from "./mdmDataReview.js";

const statusLabels = {counted: "Counted", duplicate: "Duplicate · not counted again", held: "Held · not counted"};
export default function MdmDataReview({review, encounter, original = false}) {
  let ledger;
  try { ledger = readDataReview(review, encounter); }
  catch { return <p role="alert" className="mt-3 text-sm text-amber-900">Data-review details could not be verified against this source. Review the documentation directly.</p>; }
  if (!ledger) return null;
  return <details className="mt-4 rounded-xl border border-slate-200 bg-slate-50" aria-label="MDM data review">
    <summary className="cursor-pointer px-3 py-2 text-sm font-medium">Data reviewed for E/M{original ? " · original pipeline" : ""} · {ledger.events.length} item{ledger.events.length === 1 ? "" : "s"}</summary>
    <div className="space-y-3 border-t border-slate-200 p-3 text-sm">
      <p className="text-xs text-slate-600">{original ? "Generation-time record; header edits do not change these engine findings. " : ""}Counted means included in the data element; it does not accept a code or determine the entire E/M level.</p>
      {ledger.evidence_complete ? <div className="rounded-lg bg-white p-3">
        <p>Data element: <strong className="capitalize">{ledger.level}</strong> · Category-one units: <strong>{ledger.category_one_units}</strong></p>
        <p className="mt-1 text-xs text-slate-600">The unit count includes a qualifying independent historian, when present. Historian: {ledger.independent_historian ? "counted" : "not counted"}; independent interpretation: {ledger.independent_interpretation ? "counted" : "not counted"}; external discussion: {ledger.external_discussion ? "counted" : "not counted"}.</p>
      </div> : <p role="alert" className="rounded-lg bg-amber-50 p-3 text-amber-900">Data summary unavailable: source evidence or displayed counts are incomplete. No replacement level was calculated.</p>}
      {!ledger.events.length && <p className="text-slate-600">No qualifying data activity was established.</p>}
      <ol className="space-y-3">{ledger.events.map((event) => <li key={event.id} className="rounded-lg border border-slate-200 bg-white p-3">
        <p className="text-xs font-semibold text-slate-600">{statusLabels[event.status]}</p>
        <p className="mt-1 font-medium">{dataActionLabels[event.action]} · {event.subject}</p>
        <p className="mt-1 text-slate-700">{event.reason}</p>
        {!event.evidence_complete && <p className="mt-2 text-xs text-amber-800">Complete source evidence was not located.</p>}
        {event.evidence_spans.map((span, i) => <details key={i} className="mt-2 text-xs text-slate-600">
          <summary className="cursor-pointer">Source evidence · {span.source.replaceAll("_", " ")} · characters {span.loc.start}–{span.loc.end}</summary>
          <blockquote className="mt-2 whitespace-pre-wrap border-l-2 border-slate-300 pl-3 text-sm">{span.text}</blockquote>
        </details>)}
      </li>)}</ol>
      <p className="text-xs text-slate-500">Rule {ledger.rule_version}</p>
    </div>
  </details>;
}
