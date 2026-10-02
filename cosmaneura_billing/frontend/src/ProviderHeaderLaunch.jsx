import React, { useEffect, useRef, useState } from "react";
import { FileText, ShieldCheck } from "lucide-react";
import { api } from "./api.js";
import BillingHeader from "./BillingHeader.jsx";
import { sourceLabel, visitTypeLabel } from "./providerBilling.js";
import { canLaunchHeader, headerLaunchSignIn, readLaunchEncounter } from "./providerHeaderLaunch.js";

const button = "rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50";

export default function ProviderHeaderLaunch({ launch }) {
  return <LaunchContents key={launch.encounterId || "invalid"} launch={launch} />;
}

function LaunchContents({ launch }) {
  const [encounter, setEncounter] = useState(null);
  const [persona, setPersona] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(launch.error || "");
  const [signIn, setSignIn] = useState(false);
  const [headerState, setHeaderState] = useState(null);
  const [confirmRefresh, setConfirmRefresh] = useState(false);
  const request = useRef({ sequence: 0, controller: null, busy: false });

  useEffect(() => {
    if (!launch.error) load();
    return () => { request.current.sequence++; request.current.controller?.abort(); };
  }, []);
  useEffect(() => {
    if (!headerState?.dirty && !headerState?.busy) return;
    const warn = (event) => { event.preventDefault(); event.returnValue = true; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [headerState?.dirty, headerState?.busy]);

  useEffect(() => { if (!headerState?.dirty) setConfirmRefresh(false); }, [headerState?.dirty]);

  async function load() {
    if (request.current.busy || launch.error || headerState?.busy) return;
    const sequence = ++request.current.sequence;
    const controller = new AbortController();
    request.current = { sequence, controller, busy: true };
    const deadline = window.setTimeout(() => controller.abort(), 10_000);
    setEncounter(null); setLoading(true); setError(""); setSignIn(false); setConfirmRefresh(false);
    try {
      const session = await api("/api/session", { signal: controller.signal });
      if (!canLaunchHeader(session)) throw new Error("header-access-unavailable");
      const detail = await api("/api/encounters/detail", {
        method: "POST", body: { encounterId: launch.encounterId }, signal: controller.signal,
      });
      const verified = await readLaunchEncounter(detail, launch.encounterId);
      if (sequence !== request.current.sequence) return;
      if (controller.signal.aborted) throw new Error("launch-timed-out");
      setPersona(typeof session.persona === "string" && session.persona.trim() ? session.persona.trim() : "Assigned pilot user"); setEncounter(verified);
    } catch (failure) {
      if (sequence !== request.current.sequence) return;
      setSignIn(failure.status === 401);
      setError(failure.status === 401 ? "Sign in to open this encounter’s billing header."
        : failure.status === 403 || failure.message === "header-access-unavailable" ? "Billing headers are not available for your current account or practice."
        : failure.status === 404 ? "This encounter is unavailable. Open the header from its current encounter link."
        : "This encounter could not be verified or loaded. Refresh to try again.");
    } finally {
      window.clearTimeout(deadline);
      if (sequence === request.current.sequence) { request.current.busy = false; setLoading(false); }
    }
  }

  return <div className="min-h-screen bg-slate-50 px-4 py-6 font-sans text-slate-900 sm:py-10">
    <main aria-label="Provider header launch" className="mx-auto max-w-4xl space-y-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3"><span className="rounded-xl bg-slate-900 p-2.5 text-white"><ShieldCheck size={22} aria-hidden="true" /></span>
          <div><p className="text-xs font-medium text-slate-500">Cosmas · CAB</p><h1 className="text-xl font-semibold tracking-tight">Visit billing header</h1></div>
        </div>
        {persona && <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">{persona}</span>}
      </header>
      {loading && <p role="status" className="rounded-xl border border-slate-200 bg-white p-5 text-sm text-slate-600">Opening the selected encounter…</p>}
      {error && <section role="alert" className="rounded-xl border border-amber-200 bg-amber-50 p-5 text-sm text-amber-950"><p>{error}</p>
        {signIn ? <a className="mt-3 inline-block font-semibold underline" href={headerLaunchSignIn(launch.encounterId)}>Sign in with Microsoft</a>
          : !launch.error && <button type="button" className={`${button} mt-3`} disabled={loading} onClick={load}>Refresh encounter</button>}
      </section>}
      {encounter && <>
        <section aria-label="Selected encounter" className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white p-4">
          <div className="min-w-0 flex-1">
            <h2 className="font-semibold text-slate-900">{encounter.encounterType === "office_established" ? "Established patient office visit"
              : encounter.encounterType === "office_new" ? "New patient office visit" : "Selected visit"}</h2>
            <p className="mt-1 text-xs text-slate-600">Source version {encounter.encounterVersion}</p>
            <p className="mt-1 text-xs text-violet-800">{encounter.headerSource.ingestionSource === "athena_staff_review_operational" ? "Reviewed Athena practice note" : sourceLabel(encounter.dataClass)} · Human review required</p>
            <details className="mt-3 text-xs text-slate-500">
              <summary className="w-fit cursor-pointer rounded-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-500">Source details</summary>
              <dl className="mt-2 space-y-2">
                <div><dt className="font-medium">Visit type</dt><dd>{visitTypeLabel(encounter.encounterType)}</dd></div>
                <div><dt className="font-medium">Technical encounter reference</dt><dd className="mt-1 break-all font-mono">{encounter.encounterId}</dd></div>
              </dl>
            </details>
          </div>
          <button type="button" className={button} disabled={loading || headerState?.busy} onClick={() => headerState?.dirty ? setConfirmRefresh(true) : load()}>Refresh encounter</button>
          {confirmRefresh && <div role="group" aria-label="Confirm encounter refresh" className="w-full rounded-lg bg-amber-50 p-3 text-sm text-amber-950"><p>Refreshing will discard your unsaved header edits.</p><div className="mt-2 flex gap-2"><button type="button" className={button} disabled={loading || headerState?.busy} onClick={load}>Discard edits and refresh</button><button type="button" className={button} onClick={() => setConfirmRefresh(false)}>Keep editing</button></div></div>}
        </section>
        <BillingHeader encounter={encounter} onStateChange={setHeaderState} generationAllowed={encounter.inferencePolicy?.allowed !== false} />
        <section aria-label="Encounter source documentation" className="space-y-2">
          {[['Clinical note', encounter.signedNote], ['Transcript', encounter.transcript], ['Intake', encounter.intake]].map(([label, text]) => <details key={label} className="rounded-xl border border-slate-200 bg-white">
            <summary className="flex cursor-pointer items-center gap-2 px-4 py-3 text-sm font-medium text-slate-700"><FileText size={15} aria-hidden="true" />{label}</summary>
            <div className="max-h-80 overflow-auto whitespace-pre-wrap border-t border-slate-100 px-4 py-3 text-sm leading-relaxed text-slate-700">{text || "No source text is available."}</div>
          </details>)}
        </section>
      </>}
      <p className="text-center text-xs leading-relaxed text-slate-500">Advisory billing information. The clinical note stays unchanged; no claim or EHR update is sent.</p>
    </main>
  </div>;
}
