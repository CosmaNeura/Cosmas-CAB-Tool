import React, { useEffect, useState } from "react";
import { ShieldCheck, Stethoscope, ClipboardCheck, CreditCard, Info, Wifi, WifiOff } from "lucide-react";
import { api } from "./api.js";
import BillingReview from "./BillingReview.jsx";
import CardChase from "./CardChase.jsx";
import CoderReview from "./CoderReview.jsx";

function Pill({ tone, title, children }) {
  return (
    <span title={title} className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>
      {children}
    </span>
  );
}

const tab = (active) =>
  `inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition ${
    active ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100"}`;

export default function App() {
  const [view, setView] = useState(null);               // provider | coder | chase
  const [views, setViews] = useState([]);               // server-owned capabilities
  const [health, setHealth] = useState(null);           // null = probing, false = offline
  const [authStatus, setAuthStatus] = useState(null);   // 401 = sign in, 403 = no assigned role
  const [serviceError, setServiceError] = useState(null);
  const [encounters, setEncounters] = useState([]);
  const [encountersLoaded, setEncountersLoaded] = useState(false);
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const [h, session] = await Promise.all([
          api("/api/health"),
          api("/api/session"),
        ]);
        setHealth(h);
        setViews(session.views);
        setView((current) => (
          session.views.includes(current) ? current : session.defaultView
        ));
        if (session.views.includes("provider") || session.views.includes("coder")) {
          const encs = await api("/api/encounters");
          setEncounters(encs);
          setSelected((current) => (
            encs.some((e) => e.id === current) ? current : (encs[0]?.id || null)
          ));
        }
      } catch (error) {
        if (error?.status === 401 || error?.status === 403) {
          setAuthStatus(error.status);
        } else if (error?.status) {
          setServiceError(error.status);
        } else {
          setHealth(false);
        }
      } finally {
        setEncountersLoaded(true);
      }
    })();
  }, []);

  const encounterView = view === "provider" || view === "coder";

  // UI SEAM (P2): Container Apps owns the Entra session. Keep this state
  // deliberately minimal so the separate UI work can replace its presentation
  // without changing the authentication boundary or reintroducing mock claims.
  if (authStatus) {
    return (
      <main>
        <p>{authStatus === 401 ? "Sign in is required." : "Your account is not assigned access to this pilot."}</p>
        {authStatus === 401 && <a href="/.auth/login/aad?post_login_redirect_uri=/">Sign in with Microsoft</a>}
      </main>
    );
  }

  if (serviceError) {
    return (
      <main>
        <p>Billing service unavailable (HTTP {serviceError}). No mock claim was loaded.</p>
      </main>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50 px-4 py-6 font-sans text-slate-900">
      <div className="mx-auto max-w-5xl">
        <header className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-slate-900 text-white">
              <ShieldCheck size={20} />
            </span>
            <div>
              <h1 className="text-lg font-medium tracking-tight">CosmaNeura · Billing review</h1>
              <p className="text-xs text-slate-500">Evidence-grounded coding for independent practices</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {health === null && <Pill tone="bg-slate-100 text-slate-500 ring-slate-300">connecting…</Pill>}
            {health === false && (
              <Pill tone="bg-amber-50 text-amber-700 ring-amber-200">
                <WifiOff size={12} className="mr-1" />billing service offline · no mock claim loaded
              </Pill>
            )}
            {health && (
              <Pill tone="bg-emerald-50 text-emerald-700 ring-emerald-200"
                    title={health.cptStatus}>
                <Wifi size={12} className="mr-1" />
                live engine · NCCI {health.ncciLoaded ? "✓" : "–"} · coverage {health.coverageLoaded ? "✓" : "–"} ·
                CPT {health.cptDescriptors > 0 ? "✓" : "–"}
              </Pill>
            )}
          </div>
        </header>

        <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-1 rounded-xl bg-white p-1 shadow-sm ring-1 ring-slate-200">
            {views.includes("provider") && (
              <button className={tab(view === "provider")} onClick={() => setView("provider")}>
                <Stethoscope size={15} />Provider
              </button>
            )}
            {views.includes("coder") && (
              <button className={tab(view === "coder")} onClick={() => setView("coder")}>
                <ClipboardCheck size={15} />Coder review
              </button>
            )}
            {views.includes("chase") && (
              <button className={tab(view === "chase")} onClick={() => setView("chase")}>
                <CreditCard size={15} />Card chase
              </button>
            )}
          </div>
          {encounterView && encounters.length > 0 && (
            <label className="flex items-center gap-2 text-sm text-slate-600">
              Encounter
              <select value={selected || ""} onChange={(e) => setSelected(e.target.value)}
                      className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-sm shadow-sm">
                {encounters.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.id}{e.submittedEm ? ` · submitted ${e.submittedEm}` : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        {health === null ? (
          <p>Connecting to the billing service…</p>
        ) : health === false && view === null ? (
          <p>The billing service is offline. No mock claim or patient task was loaded.</p>
        ) : view === null ? (
          <p>Loading your assigned workspace…</p>
        ) : view === "chase" ? (
          <CardChase live={Boolean(health)} />
        ) : !encountersLoaded ? (
          <p>Loading available encounters…</p>
        ) : !selected ? (
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-amber-900 shadow-sm">
            <p className="text-sm font-medium">No synthetic demo encounter is loaded</p>
            <p className="mt-1 text-sm text-amber-800">
              Load the approved synthetic release-candidate fixture before starting a review.
              No mock claim or expected code has been substituted.
            </p>
          </div>
        ) : view === "provider" ? (
          <BillingReview encounterId={selected} live={Boolean(health)} />
        ) : (
          <CoderReview encounterId={selected} live={Boolean(health)} />
        )}

        <p className="mt-6 flex items-center justify-center gap-1 text-center text-xs text-slate-400">
          <Info size={12} />
          Decision support only — a certified coder or the provider approves every claim.
        </p>
      </div>
    </div>
  );
}
