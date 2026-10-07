import React, { useCallback, useEffect, useRef, useState } from "react";
import { ShieldCheck, Stethoscope, ClipboardCheck, CreditCard, Info, Wifi, WifiOff } from "lucide-react";
import { api } from "./api.js";
import ProviderHeaderLaunch from "./ProviderHeaderLaunch.jsx";
import { canLaunchHeader, canPreviewHandoff, headerLaunchRequest } from "./providerHeaderLaunch.js";
import BillingReview from "./BillingReview.jsx";
import CardChase from "./CardChase.jsx";
import CoderReview from "./CoderReview.jsx";
import { isWorkspaceEncounterEntry } from "./workspaceSource.js";

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
      aria-label={compact ? "CardChase workflow status" : "Billing review status"}
      className={`${compact ? "mb-4 rounded-lg py-2" : "mb-5 rounded-2xl py-3 shadow-sm"} border border-violet-200 bg-violet-50 px-4 text-violet-950`}
    >
      <div className="flex items-start gap-3">
        <ShieldCheck size={20} className="mt-0.5 shrink-0" aria-hidden="true" />
        <div>
          {!compact && <p className="text-sm font-semibold">Billing review workspace</p>}
          <ul className={`${compact ? "" : "mt-1 "}flex flex-wrap gap-x-4 gap-y-1 text-xs leading-relaxed`}>
            {!compact && <li>Server-stored documentation</li>}
            <li>Decision support only</li>
            <li>Human review required</li>
            <li>No EHR or claim transmission</li>
            <li>{compact ? "Contact work requires staff review" : "Original clinical note unchanged"}</li>
          </ul>
          {!compact && <p className="mt-1 text-xs text-violet-800">The queue includes authorized training cases and registered sources. Coding runs only with current source permission.</p>}
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
  const [chaseNavigationLocked, setChaseNavigationLocked] = useState(false);
  const [headerNavigation, setHeaderNavigation] = useState({ dirty: false, busy: false });
  const [pendingNavigation, setPendingNavigation] = useState(null);
  const headerNavigationRef = useRef({ dirty: false, busy: false });
  const coderReviewLockedRef = useRef(false);
  const chaseNavigationLockedRef = useRef(false);

  const updateChaseNavigationLock = useCallback((locked) => {
    chaseNavigationLockedRef.current = locked === true;
    setChaseNavigationLocked(locked === true);
    if (locked) setPendingNavigation(null);
  }, []);

  const updateHeaderNavigation = useCallback((state) => {
    const next = { dirty: state?.dirty === true, busy: state?.navigationBlocked === true };
    headerNavigationRef.current = next;
    setHeaderNavigation((current) => current.dirty === next.dirty && current.busy === next.busy ? current : next);
    if (!next.dirty || next.busy) setPendingNavigation(null);
  }, []);
  const updateCoderNavigationLock = useCallback((locked) => {
    coderReviewLockedRef.current = locked === true;
    setCoderReviewLocked(locked === true);
    if (locked) setPendingNavigation(null);
  }, []);

  function navigate(target) {
    // Recheck at confirmation time: a save may have started after the prompt opened.
    if (headerNavigationRef.current.busy || coderReviewLockedRef.current || chaseNavigationLockedRef.current) return;
    setPendingNavigation(null);
    updateHeaderNavigation({ dirty: false, busy: false });
    if (target.kind === "encounter") setSelected(target.value);
    else if (target.kind === "workspace") setView(target.value);
    else if (target.kind === "handoff") window.location.assign("/?view=handoff");
  }
  function requestNavigation(target) {
    if ((target.kind === "encounter" && target.value === selected)
      || (target.kind === "workspace" && target.value === view)) return;
    if (headerNavigationRef.current.busy || coderReviewLockedRef.current || chaseNavigationLockedRef.current) return;
    if (headerNavigationRef.current.dirty) setPendingNavigation(target);
    else navigate(target);
  }
  const navigationLocked = headerNavigation.busy || coderReviewLocked || chaseNavigationLocked;

  useEffect(() => {
    const warnBeforeUnload = (event) => {
      if (!headerNavigationRef.current.dirty && !headerNavigationRef.current.busy
        && !coderReviewLockedRef.current && !chaseNavigationLockedRef.current) return;
      event.preventDefault();
      event.returnValue = true;
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, []);

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
          const encs = available.filter((entry) => isWorkspaceEncounterEntry(entry, canLaunchHeader(session)));
          setEncounters(encs);
          setSelected((current) => (
            encs.some((e) => e.id === current) ? current : null
          ));
          setEncounterStatuses(Object.fromEntries(encs.map((encounter) => [encounter.id, {
            reviewState: "Not reviewed this session",
            completenessState: "Not checked",
            coderReview: "Required",
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
                billing service connected
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
              <button type="button" disabled={navigationLocked && view !== "provider"} aria-current={view === "provider" ? "page" : undefined} className={`${tab(view === "provider")} disabled:cursor-not-allowed disabled:opacity-50`} onClick={() => requestNavigation({kind: "workspace", value: "provider"})}>
                <Stethoscope size={15} />Provider
              </button>
            )}
            {views.includes("coder") && (
              <button type="button" disabled={navigationLocked && view !== "coder"} aria-current={view === "coder" ? "page" : undefined} className={`${tab(view === "coder")} disabled:cursor-not-allowed disabled:opacity-50`} onClick={() => requestNavigation({kind: "workspace", value: "coder"})}>
                <ClipboardCheck size={15} />Coder review
              </button>
            )}
            {views.includes("chase") && (
              <button type="button" disabled={navigationLocked && view !== "chase"} aria-current={view === "chase" ? "page" : undefined} className={`${tab(view === "chase")} disabled:cursor-not-allowed disabled:opacity-50`} onClick={() => requestNavigation({kind: "workspace", value: "chase"})}>
                <CreditCard size={15} />Card chase
              </button>
            )}
          </nav>
          {handoffAvailable && view === "provider" && <a role="link" href={navigationLocked ? undefined : "/?view=handoff"} aria-disabled={navigationLocked} onClick={(event) => { event.preventDefault(); requestNavigation({kind: "handoff"}); }} className="text-sm font-medium text-slate-600 underline">Try a synthetic note handoff</a>}
          {headerNavigation.busy && <p role="status" className="text-xs font-medium text-amber-800">A header request is pending. Finish or retry it before changing visits or workspaces.</p>}
          {coderReviewLocked && view === "coder" && <p role="status" className="text-xs font-medium text-amber-800">Resolve this frozen coder review before leaving. Unknown outcomes must use the exact retry.</p>}
          {chaseNavigationLocked && view === "chase" && <p role="status" className="text-xs font-medium text-amber-800">A communications action is pending. Finish or retry that exact action before changing cases or workspaces.</p>}
        </div>

        {pendingNavigation && <section role="group" aria-label="Confirm discarding header edits" className="mb-5 rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
          <p className="font-semibold">This header has unsaved edits.</p>
          <p className="mt-1">Discard them and {pendingNavigation.kind === "encounter" ? `open visit ${pendingNavigation.value}` : pendingNavigation.kind === "handoff" ? "open the note handoff" : `open the ${pendingNavigation.value === "chase" ? "Card chase" : pendingNavigation.value} workspace`}?</p>
          <div className="mt-3 flex gap-2">
            <button type="button" onClick={() => setPendingNavigation(null)} className="rounded-lg border border-amber-300 bg-white px-3 py-2 font-medium">Keep editing</button>
            <button type="button" disabled={navigationLocked} onClick={() => navigate(pendingNavigation)} className="rounded-lg border border-amber-400 px-3 py-2 font-medium disabled:opacity-50">Discard edits and continue</button>
          </div>
        </section>}

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
            onNavigationLockChange={updateChaseNavigationLock}
          />
        ) : !encountersLoaded ? (
          <p>Loading available encounters…</p>
        ) : encounters.length === 0 ? (
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-amber-900 shadow-sm">
            <p className="text-sm font-medium">No reviewable encounter is available</p>
            <p className="mt-1 text-sm text-amber-800">
              Ask your administrator to load training cases or connect an authorized completed note.
              No mock claim or expected code has been substituted.
            </p>
          </div>
        ) : view === "provider" ? (
          <BillingReview
            encounters={encounters}
            selectedId={selected}
            live={Boolean(health)}
            statuses={encounterStatuses}
            onSelect={(value) => requestNavigation({kind: "encounter", value})}
            onStatusChange={updateEncounterStatus}
            onHeaderNavigationChange={updateHeaderNavigation}
            navigationLocked={navigationLocked}
          />
        ) : (
          <CoderReview
            encounterId={selected}
            encounters={encounters}
            live={Boolean(health)}
            statuses={encounterStatuses}
            onSelect={(value) => requestNavigation({kind: "encounter", value})}
            onStatusChange={updateEncounterStatus}
            onNavigationLockChange={updateCoderNavigationLock}
            onHeaderNavigationChange={updateHeaderNavigation}
            navigationLocked={navigationLocked}
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
