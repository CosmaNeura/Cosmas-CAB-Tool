import React, { useEffect, useRef, useState } from "react";
import { FileText, ShieldCheck } from "lucide-react";
import { api } from "./api.js";
import BillingHeader from "./BillingHeader.jsx";
import { sourceLabel, visitTypeLabel } from "./providerBilling.js";
import { canLaunchHeader, canPreviewHandoff, handoffAttempt, handoffSignIn, headerLaunchSignIn,
  readHandoffFixtures, readHandoffReceipt, readLaunchEncounter } from "./providerHeaderLaunch.js";

const button = "rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50";

export default function ProviderHeaderLaunch({ launch }) {
  if (launch.handoff) return <HandoffPreview key={launch.error || "handoff"} launch={launch} />;
  return <LaunchContents key={launch.encounterId || "invalid"} launch={launch} />;
}

function HandoffPreview({ launch }) {
  const [catalogue, setCatalogue] = useState(null), [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(false), [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(launch.error || ""), [signIn, setSignIn] = useState(false);
  const [receipt, setReceipt] = useState(null), [pending, setPending] = useState(false);
  const request = useRef({ sequence: 0, controller: null, busy: false }), attempt = useRef(null);
  const received = useRef(false);

  useEffect(() => {
    if (!launch.error) load();
    return () => { request.current.sequence++; request.current.busy = false; request.current.controller?.abort(); };
  }, []);
  useEffect(() => {
    if (!pending || receipt) return;
    const warn = (event) => { if (!received.current) { event.preventDefault(); event.returnValue = true; } };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [pending, receipt]);

  async function load() {
    if (request.current.busy || launch.error || attempt.current) return;
    const sequence = ++request.current.sequence, controller = new AbortController();
    request.current = { sequence, controller, busy: true };
    const deadline = window.setTimeout(() => controller.abort(), 10_000);
    setLoading(true); setError(""); setSignIn(false);
    try {
      const session = await api("/api/session", { signal: controller.signal });
      if (!canPreviewHandoff(session)) throw new Error("handoff-unavailable");
      const value = readHandoffFixtures(await api("/api/provider/encounters/handoff/fixtures", { signal: controller.signal }));
      if (sequence !== request.current.sequence) return;
      if (controller.signal.aborted) throw new Error("handoff-timed-out");
      setCatalogue(value);
    } catch (failure) {
      if (sequence !== request.current.sequence) return;
      setSignIn(failure.status === 401);
      setError(failure.status === 401 ? "Sign in to open the synthetic handoff preview."
        : failure.status === 403 || failure.message === "handoff-unavailable" ? "The synthetic handoff preview is unavailable for your account or practice."
        : "The synthetic fixtures could not be verified or loaded. Try loading them again.");
    } finally {
      window.clearTimeout(deadline);
      if (sequence === request.current.sequence) { request.current.busy = false; setLoading(false); }
    }
  }

  async function submit() {
    if (request.current.busy || !catalogue || !selected || receipt) return;
    const sequence = ++request.current.sequence, controller = new AbortController();
    request.current = { sequence, controller, busy: true };
    const deadline = window.setTimeout(() => controller.abort(), 15_000);
    setSubmitting(true); setError("");
    try {
      attempt.current = handoffAttempt(catalogue.fixtures, selected, attempt.current);
      setPending(true);
      const { operationId, fixtureKey } = attempt.current;
      const value = readHandoffReceipt(await api("/api/provider/encounters/handoff/fixture", {
        method: "POST", body: { operationId, fixtureKey }, signal: controller.signal,
      }), attempt.current);
      if (sequence !== request.current.sequence) return;
      if (controller.signal.aborted) throw new Error("handoff-timed-out");
      received.current = true; setReceipt(value);
      if (value.current) window.location.assign(value.launchPath);
    } catch (failure) {
      if (sequence !== request.current.sequence) return;
      setError(failure.status === 409 ? "The handoff conflicts with the server record. Keep this page open; retry will use the same operation."
        : failure.status === 401 || failure.status === 403 ? "The handoff could not be authorized. No new operation will be created by retry."
        : "The handoff outcome could not be verified. Keep this page open and retry the same handoff.");
    } finally {
      window.clearTimeout(deadline);
      if (sequence === request.current.sequence) { request.current.busy = false; setSubmitting(false); }
    }
  }

  return <div className="min-h-screen bg-slate-50 px-4 py-10 font-sans text-slate-900">
    <main aria-label="Synthetic provider handoff preview" className="mx-auto max-w-2xl space-y-5">
      <header><p className="text-xs font-medium text-slate-500">Cosmas · CAB</p><h1 className="mt-1 text-xl font-semibold">Synthetic completed-note handoff</h1></header>
      <section className="rounded-xl border border-violet-200 bg-violet-50 p-5 text-sm text-violet-950">
        <p>Preview with a server-owned synthetic fixture.</p>
        {catalogue && <p className="mt-2">{catalogue.completionMeaning}</p>}
        <p className="mt-2">The handoff opens the billing header. Use Generate there to request billing analysis.</p>
      </section>
      {loading && <p role="status">Loading synthetic fixtures…</p>}
      {error && <section role="alert" className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950"><p>{error}</p>
        {signIn ? <a className="mt-3 inline-block font-semibold underline" href={handoffSignIn()}>Sign in with Microsoft</a>
          : !catalogue && !launch.error && <button type="button" className={`${button} mt-3`} disabled={loading} onClick={load}>Reload fixtures</button>}
      </section>}
      {catalogue && <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-5">
        <label className="block text-sm font-medium">Synthetic encounter
          <select className="mt-2 block w-full rounded-lg border border-slate-300 bg-white p-2" value={selected}
            disabled={loading || submitting || pending || !!receipt} onChange={(event) => setSelected(event.target.value)}>
            <option value="">Choose a fixture</option>{catalogue.fixtures.map((fixture) => <option key={fixture.key} value={fixture.key}>{fixture.label}</option>)}
          </select>
        </label>
        {!catalogue.fixtures.length && <p className="text-sm">No synthetic fixtures are currently available.</p>}
        {!receipt && <button type="button" className={button} disabled={!selected || loading || submitting} onClick={submit}>
          {submitting ? "Sending handoff…" : pending ? "Retry same handoff" : "Hand off selected fixture"}
        </button>}
        {submitting && <p role="status" className="text-sm">Waiting for the handoff receipt…</p>}
        {receipt && <p role="status" className="text-sm">{receipt.current ? "Handoff received. Opening the billing header…"
          : "This receipt is superseded. Its billing header cannot be opened from this handoff."}</p>}
      </section>}
    </main>
  </div>;
}

function LaunchContents({ launch }) {
  const [encounter, setEncounter] = useState(null);
  const [headerAsync, setHeaderAsync] = useState(false);
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
      setHeaderAsync(session.capabilities?.billingHeaderAsync === true);
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
            <p className="mt-1 text-xs text-violet-800">{encounter.headerSource.ingestionSource === "athena_staff_review_operational" ? "Reviewed Athena practice note" : encounter.headerSource.ingestionSource === "provider_completed_note" ? "Provider-confirmed completed note" : sourceLabel(encounter.dataClass)} · Human review required</p>
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
        <BillingHeader asyncEnabled={headerAsync} encounter={encounter} onStateChange={setHeaderState} generationAllowed={encounter.inferencePolicy?.allowed !== false} />
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
