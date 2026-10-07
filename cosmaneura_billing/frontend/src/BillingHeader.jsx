import React, { useEffect, useRef, useState } from "react";
import SourceVisitContext from "./SourceVisitContext.jsx";
import MdmDataReview from "./MdmDataReview.jsx";
import MdmClinicalReview from "./MdmClinicalReview.jsx";
import DiagnosisMapping from "./DiagnosisMapping.jsx";
import {mappingReviewText} from "./diagnosisMapping.js";
import { api } from "./api.js";
import { beginHeaderRequest, cancelHeaderRequest, exportBillingHeader, fieldAuthor, finishHeaderRequest, headerDraft, headerSourceKey, ownsHeaderRequest, readBillingHeader, acceptedDiagnosisOrder, diagnosisOrderDecisionId, diagnosisOrderReviewText, moveHeaderDiagnosis, proposedDiagnosisOrder, replaceHeaderCodes } from "./billingHeader.js";

const button = "rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium disabled:opacity-50";
const input = "mt-1 w-full rounded-lg border border-slate-300 bg-white p-2 text-sm";
const BASE = "/api/encounters/header";
const NOOP = () => {};
const changeText = (value) => typeof value === "string" ? value || "Unspecified"
  : value === null ? "None" : typeof value.confirmed === "boolean"
    ? `${value.confirmed ? "Confirmed proposal" : "Unconfirmed"}: ${(value.codes || []).join(" → ") || "No diagnoses"}`
    : `${value.kind} ${value.code}: ${value.label} (${value.evidenceIds.length} evidence references)`;

function Evidence({ids, header}) {
  return <details className="mt-2 text-xs text-slate-600"><summary className="cursor-pointer">Source evidence</summary>
    {ids.map((id) => { const e = header.evidence.find((row) => row.id === id); return <blockquote key={id} className="mt-2 border-l-2 border-slate-300 pl-3">
      <span className="font-medium">{e.source} · version {e.sourceVersion}</span><p className="mt-1 whitespace-pre-wrap">{e.text}</p>
    </blockquote>; })}
  </details>;
}

export default function BillingHeader(props) {
  // A provenance-only source change must also clear pending requests and drafts.
  return <HeaderContents key={headerSourceKey(props.encounter)} {...props} />;
}

function HeaderContents({encounter, onStateChange = NOOP, disabled: hostDisabled = false, refreshKey = 0, generationAllowed = true}) {
  const [view, setView] = useState(null), [draft, setDraft] = useState(null);
  const [reason, setReason] = useState(""), [error, setError] = useState("");
  const [busy, setBusy] = useState(false), [loaded, setLoaded] = useState(false);
  const [retry, setRetry] = useState(null), [stale, setStale] = useState(false);
  const [notice, setNotice] = useState("");
  const pending = useRef(null), unresolvedCommand = useRef(null);
  const pin = encounter.headerSource;

  useEffect(() => run(null, true), [refreshKey]);
  useEffect(() => () => cancelHeaderRequest(pending), []);

  useEffect(() => {
    onStateChange({view, dirty: !!draft, busy: busy || !!retry,
      navigationBlocked: !!retry || !!pending.current?.command, stale});
  }, [view, draft, busy, retry, stale, onStateChange]);
  useEffect(() => () => onStateChange({view: null, dirty: false, busy: false, stale: true}), [onStateChange]);

  function run(command = null, refresh = false) {
    const request = beginHeaderRequest(pending, command, refresh, unresolvedCommand.current);
    if (!request) return NOOP;
    const {controller} = request;
    // Mutations and exact retries cannot be abandoned. A read may be canceled
    // by changing visits, even if the server never answers.
    onStateChange({view, dirty: !!draft, busy: true, navigationBlocked: !!command, stale});
    setBusy(true); setError(""); setNotice("");
    void (async () => {
      try {
        const response = await api(command?.path || BASE, {method: "POST", signal: controller.signal,
          body: command?.body || {encounterId: encounter.encounterId, expectedSource: pin}});
        if (!ownsHeaderRequest(pending, request)) return;
        const received = readBillingHeader(response.view, encounter);
        if (command && (!received || typeof response.created !== "boolean")) throw new Error("invalid-header-mutation-response");
        unresolvedCommand.current = null;
        setView(received); setLoaded(true); setRetry(null); setStale(false); setDraft(null); setReason("");
        if (command) setNotice(command.path.endsWith("save") ? "Header edits saved for review. The clinical note is unchanged."
          : "Header generated for review. No code has been accepted.");
      } catch (failure) {
        if (!ownsHeaderRequest(pending, request) || failure.name === "AbortError") return;
        const ambiguous = command && (!failure.status || failure.status >= 500 || failure.status === 429);
        unresolvedCommand.current = ambiguous ? command : null;
        setRetry(unresolvedCommand.current);
        if ([403, 404, 409].includes(failure.status)) { setStale(true); setView(null); }
        setError(failure.status === 409
          ? "The source or header changed. Refresh the encounter before editing again. Your unsaved inputs are shown below."
          : failure.status === 422 ? "The header edit could not be validated. Check the code, evidence, and required reason."
          : "The header could not be loaded or saved. No replacement result was substituted.");
      } finally {
        if (finishHeaderRequest(pending, request)) setBusy(false);
      }
    })();
    return () => cancelHeaderRequest(pending, request);
  }

  function command(path, body = {}) {
    if (path === "/generate" && !generationAllowed) return;
    run({path: BASE + path, body: {encounterId: encounter.encounterId, expectedSource: pin,
      operationId: crypto.randomUUID(), ...body}});
  }
  function startEdit() { setDraft(headerDraft(view)); setReason(""); setNotice(""); }
  function editCode(index, patch) {
    setDraft((current) => replaceHeaderCodes(current, current.codes.map((row, i) => i === index ? {...row, ...patch} : row)));
  }
  async function copyHeader() {
    try { await navigator.clipboard.writeText(exportBillingHeader(view, encounter)); setNotice("Billing header and source citations copied."); }
    catch { setNotice("Copy unavailable. Verify the current header and source before retrying."); }
  }
  function downloadHeader() {
    let content;
    try { content = exportBillingHeader(view, encounter); }
    catch { setNotice("Download unavailable. Verify the current header and source before retrying."); return; }
    const url = URL.createObjectURL(new Blob([content], {type: "text/plain;charset=utf-8"}));
    const link = document.createElement("a");
    link.href = url; link.download = `billing-header-revision-${view.revisionNumber}.txt`; link.click();
    URL.revokeObjectURL(url);
    setNotice("Billing header text downloaded with source citations.");
  }
  const disabled = hostDisabled || busy || !!retry || stale;
  const h = view?.header;
  const currentDraft = view ? headerDraft(view) : null;
  const proposedOrder = view ? proposedDiagnosisOrder(view) : [];
  const acceptedOrder = view ? acceptedDiagnosisOrder(view) : [];
  const draftDiagnoses = draft?.codes.filter((row) => row.kind === "ICD10") || [];

  return <section aria-label="Billing header" aria-busy={busy} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="font-semibold text-slate-950">Billing header</h2><p className="mt-1 text-xs text-slate-600">Visit facts and code proposals for first review · clinical note unchanged</p></div>
      {h && !draft && <div className="flex flex-wrap gap-2"><button className={button} disabled={disabled} onClick={startEdit}>Edit header</button>
        <button className={button} disabled={disabled} onClick={copyHeader}>Copy header with evidence</button>
        <button className={button} disabled={disabled} onClick={downloadHeader}>Download header text</button></div>}
      {!h && loaded && !stale && <button className={button} disabled={disabled || !generationAllowed} onClick={() => command("/generate")}>Generate billing header</button>}
    </div>
    <SourceVisitContext value={encounter.sourceVisitContext} expectedSource={pin} />
    {!generationAllowed && <div role="status" className="mt-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-950">
      <p className="font-semibold">New coding generation awaits approval</p>
      <p className="mt-1">The clinical note and any saved header remain available for review and editing. New generation requires approval to send the note to the coding model.</p>
    </div>}
    {busy && <p role="status" className="mt-3 text-sm text-slate-600">{loaded ? "Saving or generating header…" : "Loading header…"}</p>}
    {notice && <p role="status" className="mt-3 text-sm text-emerald-800">{notice}</p>}
    {error && <div role="alert" className="mt-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-900"><p>{error}</p>
      {retry && <button className={`${button} mt-2`} disabled={busy} onClick={() => run(retry)}>Retry same request</button>}
      {!retry && !stale && <button className={`${button} mt-2`} disabled={busy} onClick={() => run()}>Reload header</button>}
    </div>}
    {h && <>
      <p className="mt-4 text-xs font-medium text-slate-600">{view.signoff ? "Coder review recorded" : "Coder review pending"} · {h.generation.inferenceMode === "mock" ? "Offline synthetic engine" : "Billing engine"} · revision {view.revisionNumber}</p>
      {view.previousSignoff && <p role="status" className="mt-2 rounded-lg bg-amber-50 p-3 text-sm text-amber-900">Header edits require a fresh coder review. The prior receipt covered revision {view.previousSignoff.headerRevision}.</p>}
      {view.signoff && <div className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-950">
        <p className="font-semibold">Revision {view.signoff.headerRevision} reviewed by {view.signoff.actor.name}</p>
        <p className="mt-1 text-xs">{new Date(view.signoff.createdAt).toLocaleString()} · receipt {view.signoff.reviewId}</p>
        <p className="mt-1 text-xs">This receipt binds the saved coder decisions to this header and source. Editing the header requires a fresh review.</p>
        <details className="mt-2 text-xs"><summary className="cursor-pointer font-medium">Recorded code decisions</summary>
          <ul className="mt-2 space-y-1">{view.signoff.codeDecisions.map((d, i) => <li key={i}><code>{d.engine_code || "Human addition"}</code>: {d.action} → <code>{d.final_code || "Removed"}</code>{d.note && <p>{d.note}</p>}</li>)}</ul>
          {view.signoff.humanProposalDecisions.map((d) => <p key={d.id} className="mt-2">{d.id === diagnosisOrderDecisionId ? "Diagnosis order" : `Header proposal ${d.id}`}: {d.decision} · {d.reason}</p>)}
          {(view.signoff.baselineDecisions || []).map((d) => <p key={d.id} className="mt-2">Earlier suggestion {h.suggestions.find((row) => row.id === d.id).code}: {d.decision} · {d.reason}</p>)}
        </details>
      </div>}
      <div className="mt-3 rounded-lg border border-violet-200 bg-violet-50 p-3 text-sm" aria-label="Human diagnosis order">
        <h3 className="font-semibold">Human diagnosis order</h3>
        {proposedOrder.length ? <>
          <p className="mt-1">Proposed primary: <code>{proposedOrder[0].code}</code> · {proposedOrder.map((row) => row.code).join(" → ")}</p>
          <p className="mt-1 text-xs">Saved by {fieldAuthor(view, "diagnosisOrder")?.name || view.revision.actor.name}. {diagnosisOrderReviewText(view)}</p>
          <p className="mt-1 font-medium">{acceptedOrder.length ? `Coder-confirmed primary: ${acceptedOrder[0].code} · Order: ${acceptedOrder.map((row) => row.code).join(" → ")}` : "Coder-confirmed primary: unresolved."}</p>
        </> : <p className="mt-1">Unconfirmed. Edit the header to choose and confirm a primary and diagnosis order.</p>}
        <p className="mt-1 text-xs">Original inferred sequencing and held reasons remain unchanged below.</p>
      </div>
      <dl className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {h.facts.map((fact) => {
          const value = currentDraft.facts[fact.key], author = fieldAuthor(view, `facts.${fact.key}`);
          return <div key={fact.key} className="rounded-lg bg-slate-50 p-3">
            <dt className="text-xs font-medium text-slate-500">{fact.label}</dt>
            <dd className="mt-1 whitespace-pre-wrap text-sm">{value || "Not documented / unspecified"}</dd>
            {author ? <p className="mt-1 text-xs text-violet-800">Human entry · {author.name} ({author.role})<br/>Original: {fact.value || "Not documented"}</p>
              : <p className="mt-1 text-xs text-slate-500">{fact.status.replaceAll("_", " ")}</p>}
            {fact.evidenceIds.length > 0 && <Evidence ids={fact.evidenceIds} header={h} />}
          </div>;
        })}
      </dl>
      <h3 className="mt-5 text-sm font-semibold">Original pipeline suggestions</h3>
      {!h.suggestions.length && <p className="mt-2 text-sm text-slate-600">No evidence-supported codes were suggested.</p>}
      <ul className="mt-2 grid gap-2 sm:grid-cols-2">{h.suggestions.map((code) => <li key={code.id} className="rounded-lg border border-slate-200 p-3 text-sm">
        <strong className="font-mono">{code.code}</strong> · {code.label}<p className="mt-1 text-xs text-slate-600">{code.basis}</p><Evidence ids={code.evidenceIds} header={h} />
      </li>)}</ul>
      <MdmDataReview review={h.dataReview} encounter={encounter} original />
      <MdmClinicalReview review={h.clinicalReview} encounter={encounter} original />
      {view.revision && <div className="mt-4 rounded-lg border border-violet-200 bg-violet-50 p-3 text-sm text-violet-950">
        <h3 className="font-semibold">Current code proposals · {view.signoff ? "review outcomes recorded above" : "require review"}</h3>
        <p className="mt-1 text-xs">Edited codes remain separate from engine suggestions. Saving does not accept a code or complete coder signoff.</p>
        <ul className="mt-2 space-y-2">{currentDraft.codes.map((code) => {const author = fieldAuthor(view, `codes.${code.id}`); return <li key={code.id}><code>{code.code}</code> · {code.label}
          {code.mapping && <p className="text-xs">{mappingReviewText(view, code)} Linked to {code.mapping.candidateId}; original hold retained.</p>}
          <p className="text-xs">{author ? `Human proposal · ${author.name} (${author.role})` : "Original pipeline suggestion retained"}</p><Evidence ids={code.evidenceIds} header={h} /></li>; })}</ul>
        {h.suggestions.filter((s) => !currentDraft.codes.some((c) => c.id === s.id)).map((s) => <p key={s.id} className="mt-2 text-xs">Removal proposed: <code>{s.code}</code> · {s.label}</p>)}
        {!currentDraft.codes.length && <p className="mt-2">No codes proposed in this revision.</p>}
        {currentDraft.note && <p className="mt-2 whitespace-pre-wrap">Reviewer note: {currentDraft.note}</p>}
      </div>}
      {h.flags.length > 0 && <ul className="mt-4 list-disc space-y-1 rounded-lg bg-amber-50 py-3 pl-7 pr-3 text-xs text-amber-900">{h.flags.map((f) => <li key={f.id}>{f.message}
        {f.evidenceIds?.length > 0 && <Evidence ids={f.evidenceIds} header={h} />}</li>)}</ul>}
      <p className="mt-3 text-xs text-slate-500">{h.policy.message}</p>
    </>}
    {draft && <form className="mt-5 border-t border-slate-200 pt-4" onSubmit={(event) => {event.preventDefault(); if (!disabled && reason.trim()) command("/save", {
      headerId: h.headerId, expectedRevision: view.revisionNumber, draft, reason,
    });}}>
      <h3 className="font-semibold">Edit review header</h3><p className="mt-1 text-xs text-slate-600">Blank fields stay unspecified. An edit does not establish clinical evidence or clear a documentation gap.</p>
      <fieldset disabled={disabled} className="mt-3 grid gap-3 sm:grid-cols-2">
        {Object.entries(draft.facts).map(([key, value]) => <label key={key} className="text-xs font-medium text-slate-700">{h?.facts.find((f) => f.key === key)?.label || key.replaceAll("_", " ")}
          <input className={input} maxLength={600} value={value} onChange={(e) => setDraft({...draft, facts: {...draft.facts, [key]: e.target.value}})} /></label>)}
      </fieldset>
      <fieldset disabled={disabled} className="mt-4 space-y-3"><legend className="text-sm font-semibold">Code proposals</legend>
        {draft.codes.map((code, index) => <div key={code.id} className="rounded-lg border border-slate-200 p-3">
          <div className="grid gap-2 sm:grid-cols-[6rem_7rem_1fr_auto]">
            <label className="text-xs">Kind<select disabled={!!code.mapping} className={input} value={code.kind} onChange={(e) => editCode(index, {kind: e.target.value})}><option>EM</option><option>ICD10</option></select></label>
            <label className="text-xs">Code<input readOnly={!!code.mapping} className={input} maxLength={15} required value={code.code} onChange={(e) => editCode(index, {code: e.target.value.toUpperCase()})} /></label>
            <label className="text-xs">Short label<input readOnly={!!code.mapping} className={input} maxLength={150} required value={code.label} onChange={(e) => editCode(index, {label: e.target.value})} /></label>
            <button type="button" className={`${button} self-end`} onClick={() => setDraft(replaceHeaderCodes(draft, draft.codes.filter((_, i) => i !== index)))}>Remove proposal</button>
          </div>
          {code.kind === "ICD10" && <div className="mt-2 flex flex-wrap items-center gap-2 text-xs" role="group" aria-label={`Order ${code.code || "new diagnosis"}`}>
            <span>{draftDiagnoses[0]?.id === code.id ? "Proposed primary" : `Diagnosis ${draftDiagnoses.findIndex((row) => row.id === code.id) + 1}`} · {draft.diagnosisOrderConfirmed ? "confirmed in editor" : "unconfirmed"}</span>
            <button type="button" className={button} disabled={draftDiagnoses[0]?.id === code.id} onClick={() => setDraft(moveHeaderDiagnosis(draft, code.id, 0))}>Make primary</button>
            <button type="button" className={button} disabled={draftDiagnoses[0]?.id === code.id} onClick={() => setDraft(moveHeaderDiagnosis(draft, code.id, draftDiagnoses.findIndex((row) => row.id === code.id) - 1))}>Move up</button>
            <button type="button" className={button} disabled={draftDiagnoses.at(-1)?.id === code.id} onClick={() => setDraft(moveHeaderDiagnosis(draft, code.id, draftDiagnoses.findIndex((row) => row.id === code.id) + 1))}>Move down</button>
          </div>}
          <details className="mt-3 text-xs"><summary className="cursor-pointer">Source evidence ({code.evidenceIds.length} selected; required)</summary>
            {code.mapping && <p>Evidence is bound to the original held diagnosis. Use the catalog below to replace its mapping.</p>}
            {h?.evidence.map((e) => <label key={e.id} className="mt-2 flex items-start gap-2"><input type="checkbox" disabled={!!code.mapping} checked={code.evidenceIds.includes(e.id)} onChange={(event) => editCode(index, {evidenceIds: event.target.checked ? [...code.evidenceIds, e.id] : code.evidenceIds.filter((id) => id !== e.id)})} /><span>{e.source}: {e.text}</span></label>)}
          </details>
        </div>)}
        <button type="button" className={button} disabled={draft.codes.length >= 20} onClick={() => setDraft(replaceHeaderCodes(draft, [...draft.codes, {id: `human-${crypto.randomUUID()}`, kind: "ICD10", code: "", label: "", evidenceIds: []}]))}>Add code proposal</button>
      </fieldset>
      <DiagnosisMapping {...{view, draft, disabled}} onChoose={(row) => setDraft((current) => replaceHeaderCodes(current,
        current.codes.some((c) => c.id === row.id) ? current.codes.map((c) => c.id === row.id ? row : c) : [...current.codes, row]))} />
      <label className="mt-4 flex items-start gap-2 text-sm"><input type="checkbox" disabled={disabled || !draftDiagnoses.length} checked={draft.diagnosisOrderConfirmed === true} onChange={(event) => setDraft({...draft, diagnosisOrderConfirmed: event.target.checked})} />
        <span>I confirm {draftDiagnoses[0]?.code || "the first diagnosis"} as my proposed primary and the displayed diagnosis order. Saving sends this proposal for coder review. Code, order, or evidence edits clear this confirmation.</span>
      </label>
      <label className="mt-4 block text-xs font-medium">Reviewer note (optional)<textarea className={input} maxLength={2000} disabled={disabled} value={draft.note} onChange={(e) => setDraft({...draft, note: e.target.value})} /></label>
      <label className="mt-3 block text-xs font-medium">Reason for edit (required)<input className={input} required maxLength={600} disabled={disabled} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
      <div className="mt-3 flex flex-wrap gap-2"><button type="submit" className={`${button} bg-slate-950 text-white`} disabled={disabled || !reason.trim()}>Save header edits</button>
        <button type="button" className={button} disabled={hostDisabled || busy || !!retry} onClick={() => {setDraft(null); setReason("");}}>Cancel edits</button>
        <button type="button" className={button} disabled={disabled} onClick={() => setDraft(headerDraft({...view, revision: null}))}>Reset editor to generated values</button>
      </div>
    </form>}
    {view?.revision && <details className="mt-5 border-t border-slate-200 pt-3 text-xs"><summary className="cursor-pointer font-medium">Revision history ({view.revisionNumber})</summary>
      <ol className="mt-3 space-y-3">{[...view.revision.history].reverse().map((revision) => <li key={revision.id} className="rounded-lg bg-slate-50 p-3">
        <p className="font-medium">Revision {revision.number} · {revision.actor.name} ({revision.actor.role}) · {new Date(revision.createdAt).toLocaleString()}</p>
        <p className="mt-1">Reason: {revision.reason}</p>
        <ul className="mt-2 space-y-1">{revision.changes.map((change) => <li key={change.path}><strong>{change.label}</strong>: <span className="whitespace-pre-wrap">{changeText(change.before)} → {changeText(change.after)}</span></li>)}</ul>
      </li>)}</ol>
    </details>}
  </section>;
}
