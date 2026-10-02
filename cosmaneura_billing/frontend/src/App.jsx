import React, { useCallback, useEffect, useState } from "react";
import { ShieldCheck, Stethoscope, ClipboardCheck, CreditCard, Info, Wifi, WifiOff } from "lucide-react";
import { api } from "./api.js";
import ProviderHeaderLaunch from "./ProviderHeaderLaunch.jsx";
import { canLaunchHeader, canPreviewHandoff, headerLaunchRequest } from "./providerHeaderLaunch.js";
import BillingReview from "./BillingReview.jsx";
import CardChase from "./CardChase.jsx";
import CoderReview from "./CoderReview.jsx";
import { isSyntheticDemoEncounter } from "./providerBilling.js";

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

function BrandLockup() {
  return (
    <div className="flex items-center gap-3">
      <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-slate-900 text-white"
            aria-hidden="true">
        <ShieldCheck size={20} />
      </span>
      <div>
        <h1 className="text-lg font-medium tracking-tight">Cosmas · Billing review</h1>
        <p className="text-xs text-slate-500">
          Evidence-grounded coding for independent practices · by CosmaNeura
        </p>
      </div>
    </div>
  );
}

function BrandedMessage({ children }) {
  return (
    <div className="min-h-screen bg-slate-50 px-4 py-6 font-sans text-slate-900">
      <main className="mx-auto max-w-5xl">
        <header className="mb-6">
          <BrandLockup />
        </header>
        <section className="rounded-2xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
          {children}
        </section>
      </main>
    </div>
  );
}

function SafetyBanner({ compact = false }) {
  return (
    <section
      aria-label={compact ? "CardChase workflow status" : "Synthetic pilot safety status"}
      className={`${compact ? "mb-4 rounded-lg py-2" : "mb-5 rounded-2xl py-3 shadow-sm"} border border-violet-200 bg-violet-50 px-4 text-violet-950`}
    >
      <div className="flex items-start gap-3">
        <ShieldCheck size={20} className="mt-0.5 shrink-0" aria-hidden="true" />
        <div>
          {!compact && <p className="text-sm font-semibold">Synthetic CAB demo boundary</p>}
          <ul className={`${compact ? "" : "mt-1 "}flex flex-wrap gap-x-4 gap-y-1 text-xs leading-relaxed`}>
            {!compact && <li>Synthetic data</li>}
            <li>Decision support only</li>
            <li>Human review required</li>
            <li>No EHR or claim transmission</li>
            <li>{compact ? "Contact work requires staff review" : "Patient outreach disabled by demo policy"}</li>
          </ul>
          {!compact && <p className="mt-1 text-xs text-violet-800">Only server-verified, read-only synthetic encounters are listed in this workspace.</p>}
        </div>
      </div>
    </section>
  );
}

export default function App() {
  const launch = headerLaunchRequest(window.location.search);
  return launch ? <ProviderHeaderLaunch launch={launch} /> : <Workspace />;
}

function Workspace() {
  const [view, setView] = useState(null);               // provider | coder | chase
  const [views, setViews] = useState([]);               // server-owned capabilities
  const [chaseDemoSeed, setChaseDemoSeed] = useState(null); // Admin-only server capability
  const [billingHeaderAvailable, setBillingHeaderAvailable] = useState(false);
  const [handoffAvailable, setHandoffAvailable] = useState(false);
  const [manageChase, setManageChase] = useState(false); // Server-owned staff mutation capability
  const [health, setHealth] = useState(null);           // null = probing, false = offline
  const [authStatus, setAuthStatus] = useState(null);   // 401 = sign in, 403 = no assigned role
  const [serviceError, setServiceError] = useState(null);
  const [encounters, setEncounters] = useState([]);
  const [encountersLoaded, setEncountersLoaded] = useState(false);
  const [selected, setSelected] = useState(null);
  const [encounterStatuses, setEncounterStatuses] = useState({});
  const [coderReviewLocked, setCoderReviewLocked] = useState(false);

  const updateEncounterStatus = useCallback((encounterId, patch) => {
    setEncounterStatuses((current) => ({
      ...current,
      [encounterId]: { ...(current[encounterId] || {}), ...patch },
    }));
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const [h, session] = await Promise.all([
          api("/api/health"),
          api("/api/session"),
        ]);
        setHealth(h);
        setViews(session.views);
        setChaseDemoSeed(session.actions?.seedSyntheticChase || null);
        setManageChase(session.capabilities?.manageChase === true);
        setBillingHeaderAvailable(canLaunchHeader(session));
        setHandoffAvailable(canPreviewHandoff(session));
        setView((current) => (
          session.views.includes(current) ? current : session.defaultView
        ));
        if (session.views.includes("provider") || session.views.includes("coder")) {
          const available = await api("/api/encounters");
          const encs = available.filter(isSyntheticDemoEncounter);
          setEncounters(encs);
          setSelected((current) => (
            encs.some((e) => e.id === current) ? current : null
          ));
          setEncounterStatuses(Object.fromEntries(encs.map((encounter) => [encounter.id, {
            reviewState: "Not reviewed this session",
            completenessState: "Not checked",
            coderReview: "Required (demo policy)",
            processingStatus: "Ready to open",
          }])));
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

  // UI SEAM (P2): Container Apps owns the Entra session. Keep this state
  // deliberately minimal so the separate UI work can replace its presentation
  // without changing the authentication boundary or reintroducing mock claims.
  if (authStatus) {
    return (
      <BrandedMessage>
        <p>{authStatus === 401 ? "Sign in is required." : "Your account is not assigned access to this pilot."}</p>
        {authStatus === 401 && (
          <a className="mt-3 inline-block font-medium text-slate-900 underline"
             href="/.auth/login/aad?post_login_redirect_uri=/">
            Sign in with Microsoft
          </a>
        )}
      </BrandedMessage>
    );
  }

  if (serviceError) {
    return (
      <BrandedMessage>
        <p>Billing service unavailable (HTTP {serviceError}). No mock claim was loaded.</p>
      </BrandedMessage>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50 px-4 py-6 font-sans text-slate-900">
      <div className={`mx-auto ${view === "chase" ? "max-w-[1440px]" : "max-w-5xl"}`}>
        <header className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <BrandLockup />
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
                synthetic pilot service connected
              </Pill>
            )}
          </div>
        </header>

        <SafetyBanner compact={view === "chase"} />

        <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
          <nav
            className="flex items-center gap-1 rounded-xl bg-white p-1 shadow-sm ring-1 ring-slate-200"
            aria-label="Workspace"
          >
            {views.includes("provider") && (
              <button type="button" disabled={coderReviewLocked && view === "coder"} aria-current={view === "provider" ? "page" : undefined} className={`${tab(view === "provider")} disabled:cursor-not-allowed disabled:opacity-50`} onClick={() => setView("provider")}>
                <Stethoscope size={15} />Provider
              </button>
            )}
            {views.includes("coder") && (
              <button type="button" aria-current={view === "coder" ? "page" : undefined} className={tab(view === "coder")} onClick={() => setView("coder")}>
                <ClipboardCheck size={15} />Coder review
              </button>
            )}
            {views.includes("chase") && (
              <button type="button" disabled={coderReviewLocked && view === "coder"} aria-current={view === "chase" ? "page" : undefined} className={`${tab(view === "chase")} disabled:cursor-not-allowed disabled:opacity-50`} onClick={() => setView("chase")}>
                <CreditCard size={15} />Card chase
              </button>
            )}
          </nav>
          {handoffAvailable && view === "provider" && <a href="/?view=handoff" className="text-sm font-medium text-slate-600 underline">Try a synthetic note handoff</a>}
          {coderReviewLocked && view === "coder" && <p role="status" className="text-xs font-medium text-amber-800">Resolve this frozen coder review before leaving. Unknown outcomes must use the exact retry.</p>}
        </div>

        {health === null ? (
          <p>Connecting to the billing service…</p>
        ) : health === false && view === null ? (
          <p>The billing service is offline. No mock claim or patient task was loaded.</p>
        ) : view === null ? (
          <p>Loading your assigned workspace…</p>
        ) : view === "chase" ? (
          <CardChase
            live={Boolean(health)}
            demoSeed={chaseDemoSeed}
            manageChase={manageChase}
            billingHeaderAvailable={billingHeaderAvailable}
          />
        ) : !encountersLoaded ? (
          <p>Loading available encounters…</p>
        ) : encounters.length === 0 ? (
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-amber-900 shadow-sm">
            <p className="text-sm font-medium">No synthetic demo encounter is loaded</p>
            <p className="mt-1 text-sm text-amber-800">
              Load the approved synthetic release-candidate fixture before starting a review.
              No mock claim or expected code has been substituted.
            </p>
          </div>
        ) : view === "provider" ? (
          <BillingReview
            encounters={encounters}
            selectedId={selected}
            live={Boolean(health)}
            statuses={encounterStatuses}
            onSelect={setSelected}
            onStatusChange={updateEncounterStatus}
          />
        ) : (
          <CoderReview
            encounterId={selected}
            encounters={encounters}
            live={Boolean(health)}
            statuses={encounterStatuses}
            onSelect={setSelected}
            onStatusChange={updateEncounterStatus}
            onNavigationLockChange={setCoderReviewLocked}
          />
        )}

        <p className="mt-6 flex items-center justify-center gap-1 text-center text-xs text-slate-500">
          <Info size={12} />
          {view === "chase" ? "Staff review governs outreach. Check each message's delivery status in Communications."
            : "Prepared billing guidance only — no EHR, Athena, clearinghouse, payer, or patient transmission occurred."}
        </p>
      </div>
    </div>
  );
}
