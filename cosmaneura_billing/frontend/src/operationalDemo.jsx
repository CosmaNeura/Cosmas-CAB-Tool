import React from "react";
import { createRoot } from "react-dom/client";
import { FlaskConical, ShieldCheck } from "lucide-react";

import McdAdminChase from "./McdAdminChase.jsx";
import SuggestedCodes from "./SuggestedCodes.jsx";
import "./index.css";
import syntheticMcdWorklist from "./operationalMcdWorklist.golden.json";
import suggestedCodesGolden from "./operationalSuggestedCodes.golden.json";

function OperationalDemo() {
  return (
    <div className="min-h-screen bg-slate-100 px-4 py-6 font-sans text-slate-900">
      <main className="mx-auto max-w-6xl">
        <header className="rounded-2xl bg-slate-950 p-5 text-white shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-violet-200">
                <FlaskConical size={15} aria-hidden="true" />Local operational demonstration
              </p>
              <h1 className="mt-2 text-2xl font-semibold tracking-tight">Cosmas CAB · two default-off workflows</h1>
              <p className="mt-2 max-w-3xl text-sm leading-relaxed text-slate-300">
                Synthetic data only. Suggested codes require exact evidence. Card Chase records staff phone-task work for insurance-card collection only.
              </p>
            </div>
            <span className="inline-flex items-center gap-1.5 rounded-lg bg-rose-500/15 px-3 py-1.5 text-xs font-semibold text-rose-100 ring-1 ring-inset ring-rose-300/40">
              <ShieldCheck size={14} aria-hidden="true" />Not production · no outbound transport
            </span>
          </div>
        </header>

        <div className="mt-5 space-y-5">
          <SuggestedCodes payload={suggestedCodesGolden.payload} documents={suggestedCodesGolden.documents} />
          <McdAdminChase worklist={syntheticMcdWorklist} />
        </div>

        <footer className="py-6 text-center text-xs text-slate-500">
          This isolated page is not mounted by the production application and performs no API requests.
        </footer>
      </main>
    </div>
  );
}

const root = document.getElementById("operational-demo-root");
if (!root) throw new Error("Missing #operational-demo-root mount point");
createRoot(root).render(
  <React.StrictMode>
    <OperationalDemo />
  </React.StrictMode>,
);
