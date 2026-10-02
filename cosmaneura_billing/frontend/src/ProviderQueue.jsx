import React from "react";
import { ArrowRight, ClipboardList } from "lucide-react";

import { sourceLabel, visitTypeLabel } from "./providerBilling.js";

const DEFAULT_STATUS = Object.freeze({
  reviewState: "Not reviewed this session",
  completenessState: "Not checked",
  coderReview: "Required (demo policy)",
  processingStatus: "Ready to open",
});

function QueueValue({ children }) {
  return <span className="text-xs text-slate-700">{children}</span>;
}

export default function ProviderQueue({
  encounters, selectedId, statuses, onSelect,
  title = "Encounter work queue",
  description = "Choose a visit to check documentation and generate evidence-linked billing guidance.",
  selectionDisabled = false,
}) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white shadow-sm" aria-labelledby="encounter-queue-heading">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 px-5 py-4">
        <div>
          <h2 id="encounter-queue-heading" className="flex items-center gap-2 text-base font-semibold text-slate-950">
            <ClipboardList size={18} aria-hidden="true" />{title}
          </h2>
          <p className="mt-1 text-sm text-slate-600">{description}</p>
        </div>
        <p className="max-w-sm text-xs leading-relaxed text-slate-500">
          Review and processing labels describe this browser session; they do not claim that a review was saved or transmitted.
        </p>
      </div>
      <div
        className="overflow-x-auto focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700"
        role="region"
        aria-label="Scrollable encounter work queue"
        tabIndex={0}
      >
        <table className="min-w-[940px] w-full border-collapse text-left">
          <caption className="sr-only">Synthetic encounters and their current in-session review status</caption>
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="px-4 py-3 font-medium">Encounter</th>
              <th scope="col" className="px-3 py-3 font-medium">Visit type</th>
              <th scope="col" className="px-3 py-3 font-medium">Review state</th>
              <th scope="col" className="px-3 py-3 font-medium">Completeness</th>
              <th scope="col" className="px-3 py-3 font-medium">Coder review policy</th>
              <th scope="col" className="px-3 py-3 font-medium">Provenance</th>
              <th scope="col" className="px-3 py-3 font-medium">Processing</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {encounters.map((encounter) => {
              const state = { ...DEFAULT_STATUS, ...(statuses[encounter.id] || {}) };
              const selected = encounter.id === selectedId;
              return (
                <tr key={encounter.id} className={selected ? "bg-sky-50" : "hover:bg-slate-50"}>
                  <th scope="row" className="px-4 py-3 font-normal">
                    <button
                      type="button"
                      onClick={() => onSelect(encounter.id)}
                      disabled={selectionDisabled}
                      aria-current={selected ? "true" : undefined}
                      className="inline-flex min-h-10 items-center gap-2 rounded-lg px-2 py-1.5 text-left font-mono text-sm font-semibold text-slate-950 hover:bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {encounter.id}<ArrowRight size={14} aria-hidden="true" />
                    </button>
                  </th>
                  <td className="px-3 py-3"><QueueValue>{visitTypeLabel(encounter.encounterType)}</QueueValue></td>
                  <td className="px-3 py-3"><QueueValue>{state.reviewState}</QueueValue></td>
                  <td className="px-3 py-3"><QueueValue>{state.completenessState}</QueueValue></td>
                  <td className="px-3 py-3"><QueueValue>{state.coderReview}</QueueValue></td>
                  <td className="max-w-44 px-3 py-3"><QueueValue>{sourceLabel(encounter.dataClass)}</QueueValue></td>
                  <td className="px-3 py-3"><QueueValue>{state.processingStatus}</QueueValue></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
