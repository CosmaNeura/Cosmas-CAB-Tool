import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, Check, ChevronDown, Clock, FileCheck2, Info,
  Pencil, Plus, RefreshCw, RotateCcw, Save, ShieldCheck, X,
} from "lucide-react";

import { api } from "./api.js";
import {
  buildFeedbackPayload, canPrepareCoderPreview, capturedRecommendations, coderErrorMessage,
  coderQueueState, excludedRecommendations, initialCoderDraft,
  reviewableCoderView, UNREVIEWED, validateCoderDraft,
} from "./coderReview.js";
import ProviderQueue from "./ProviderQueue.jsx";
import BillingHeader from "./BillingHeader.jsx";
import DiagnosisSequencingNotice from "./DiagnosisSequencingNotice.jsx";
import { primaryBadge } from "./diagnosisSequencing.js";
import { diagnosisOrderDecisionId, headerDecisionIds, headerProposals, headerReviewRequest,
  missingHeaderBaseline, proposedDiagnosisOrder, readBillingHeader } from "./billingHeader.js";
import {
  completenessLabel, isSyntheticDemoEncounter, sourceLabel,
} from "./providerBilling.js";

const REASONS = Object.freeze({
  EM: [
    ["em_doc_supports_higher_level", "Documentation supports a higher level"],
    ["em_doc_supports_lower_level", "Documentation supports a lower level"],
    ["em_mdm_element_disagreement", "Disagree on an MDM element"],
    ["em_time_based_leveling", "Should be time-based"],
    ["other", "Other"],
  ],
  ICD10: [
    ["dx_not_supported_by_doc", "Not supported by documentation"],
    ["dx_contradicted_by_doc", "Contradicted by documentation"],
    ["dx_needs_more_specific_code", "Needs a more specific code"],
    ["dx_primary_secondary_swap", "Wrong primary / secondary order"],
    ["other", "Other"],
  ],
  PROCEDURE: [
    ["procedure_bundling_ncci", "Procedure bundling concern"],
    ["medical_necessity_coverage", "Medical necessity or coverage concern"],
    ["other", "Other"],
  ],
  MODIFIER: [
    ["modifier_required", "Modifier correction required"],
    ["procedure_bundling_ncci", "Procedure bundling concern"],
    ["other", "Other"],
  ],
});

const MANUAL_KINDS = Object.freeze([
  ["ICD10", "ICD-10-CM"],
  ["PROCEDURE", "Procedure"],
  ["MODIFIER", "Modifier"],
]);
const NOOP = () => {};

function Pill({ tone = "bg-slate-100 text-slate-700 ring-slate-200", children }) {
  return <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>{children}</span>;
}

function ErrorNotice({ children }) {
  return <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-900">{children}</div>;
}

function Why({ row }) {
  const spans = (row.evidenceSpans || [
    { source: "documentation", text: row.evidence, loc: null },
  ]).filter((span) => span?.text?.trim());
  return (
    <details className="group mt-2 rounded-lg border border-slate-200 bg-slate-50">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-xs font-medium text-sky-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700">
        <Info size={13} aria-hidden="true" />Why this code
        <ChevronDown size={13} className="transition group-open:rotate-180" aria-hidden="true" />
      </summary>
      <div className="space-y-2 border-t border-slate-200 px-3 py-2 text-xs leading-relaxed text-slate-700">
        <p>{row.why || "No additional engine rationale was returned."}</p>
        {spans.map((span, index) => (
          <blockquote key={`${span.source}-${span.loc?.start ?? index}`} className="border-l-2 border-sky-200 pl-2 italic">
            “{span.text}” <span className="not-italic text-slate-500">— {String(span.source || "documentation").replaceAll("_", " ")}{Number.isInteger(span.loc?.start) ? `, characters ${span.loc.start}–${span.loc.end}` : ""}</span>
          </blockquote>
        ))}
        {row.history && <p className="rounded-md bg-violet-50 px-2 py-1 text-violet-800">Prior reviewer pattern: {row.history}</p>}
      </div>
    </details>
  );
}

function DecisionButton({ active, tone, disabled, onClick, children }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
      className={`inline-flex min-h-10 items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-50 ${active ? tone : "text-slate-600 hover:bg-slate-100"}`}
    >
      {children}
    </button>
  );
}

function ReasonSelect({ id, kind, value, disabled, invalid, describedBy, onChange, label }) {
  return (
    <label htmlFor={id} className="grid gap-1 text-xs font-medium text-slate-700">
      {label}
      <select id={id} required disabled={disabled} aria-invalid={invalid || undefined} aria-describedby={describedBy} value={value || ""} onChange={(event) => onChange(event.target.value)} className="min-h-10 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm text-slate-800 disabled:bg-slate-100">
        <option value="" disabled>Choose a reason</option>
        {(REASONS[kind] || REASONS.ICD10).map(([reason, text]) => <option key={reason} value={reason}>{text}</option>)}
      </select>
    </label>
  );
}

function RowIssues({ id, issues }) {
  if (!issues.length) return null;
  return <ul id={id} className="mt-2 list-disc space-y-1 pl-5 text-xs text-rose-800">{issues.map((message) => <li key={message}>{message}</li>)}</ul>;
}

function CodeRow({ item, decision, disabled, issues, onChange, primaryLabel }) {
  const { key, kind, row } = item;
  const prefix = `coder-${key.replaceAll(":", "-")}`;
  const errorId = `${prefix}-errors`;
  const invalid = issues.length > 0;
  return (
    <fieldset aria-describedby={invalid ? errorId : undefined} className="border-t border-slate-200 py-4">
      <legend className="sr-only">Review {row.code}</legend>
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <code className="font-mono text-sm font-semibold text-slate-950">{row.code}</code>
            {row.primary && primaryLabel && <Pill tone="bg-sky-50 text-sky-800 ring-sky-200">{primaryLabel}</Pill>}
            <Pill tone={decision.action === UNREVIEWED ? "bg-amber-50 text-amber-800 ring-amber-200" : "bg-slate-100 text-slate-700 ring-slate-200"}>{decision.action}</Pill>
          </div>
          <p className="mt-1 text-sm text-slate-700">{row.label}</p>
          <Why row={row} />
        </div>
        <div role="group" aria-label={`Decision for ${row.code}`} aria-invalid={invalid || undefined} className="flex flex-wrap items-center gap-1 rounded-lg bg-slate-50 p-1">
          <DecisionButton active={decision.action === "accept"} tone="bg-emerald-100 text-emerald-800" disabled={disabled} onClick={() => onChange({ action: "accept", finalCode: row.code, reasonCode: "", rationale: "" })}><Check size={13} aria-hidden="true" />Accept</DecisionButton>
          <DecisionButton active={decision.action === "modify"} tone="bg-amber-100 text-amber-900" disabled={disabled} onClick={() => onChange({ action: "modify", finalCode: "", reasonCode: "", rationale: "" })}><Pencil size={13} aria-hidden="true" />Edit</DecisionButton>
          <DecisionButton active={decision.action === "reject"} tone="bg-rose-100 text-rose-900" disabled={disabled} onClick={() => onChange({ action: "reject", finalCode: "", reasonCode: "", rationale: "" })}><X size={13} aria-hidden="true" />Reject</DecisionButton>
        </div>
      </div>
      {decision.action === "modify" && (
        <div className="mt-3 grid gap-3 rounded-xl bg-amber-50 p-3 md:grid-cols-2">
          <label htmlFor={`${prefix}-code`} className="grid gap-1 text-xs font-medium text-amber-950">Corrected code
            <input id={`${prefix}-code`} required maxLength={32} disabled={disabled} aria-invalid={invalid || undefined} aria-describedby={invalid ? errorId : undefined} value={decision.finalCode} onChange={(event) => onChange({ ...decision, finalCode: event.target.value })} className="min-h-10 rounded-md border border-amber-300 bg-white px-2 py-1 font-mono text-sm text-slate-900" />
          </label>
          <ReasonSelect id={`${prefix}-reason`} kind={kind} value={decision.reasonCode} disabled={disabled} invalid={invalid} describedBy={invalid ? errorId : undefined} onChange={(reasonCode) => onChange({ ...decision, reasonCode })} label="Edit reason" />
          <label htmlFor={`${prefix}-rationale`} className="grid gap-1 text-xs font-medium text-amber-950 md:col-span-2">Coder rationale
            <textarea id={`${prefix}-rationale`} required maxLength={3900} disabled={disabled} aria-invalid={invalid || undefined} aria-describedby={invalid ? errorId : undefined} value={decision.rationale} onChange={(event) => onChange({ ...decision, rationale: event.target.value })} rows={2} className="rounded-md border border-amber-300 bg-white px-2 py-1 text-sm text-slate-900" />
          </label>
        </div>
      )}
      {decision.action === "reject" && (
        <div className="mt-3 grid gap-3 rounded-xl bg-rose-50 p-3 md:grid-cols-2">
          <ReasonSelect id={`${prefix}-reason`} kind={kind} value={decision.reasonCode} disabled={disabled} invalid={invalid} describedBy={invalid ? errorId : undefined} onChange={(reasonCode) => onChange({ ...decision, reasonCode })} label="Rejection reason" />
          <label htmlFor={`${prefix}-rationale`} className="grid gap-1 text-xs font-medium text-rose-950">Coder rationale
            <textarea id={`${prefix}-rationale`} required maxLength={3900} disabled={disabled} aria-invalid={invalid || undefined} aria-describedby={invalid ? errorId : undefined} value={decision.rationale} onChange={(event) => onChange({ ...decision, rationale: event.target.value })} rows={2} className="rounded-md border border-rose-300 bg-white px-2 py-1 text-sm text-slate-900" />
          </label>
        </div>
      )}
      <RowIssues id={errorId} issues={issues} />
    </fieldset>
  );
}

function ExcludedRow({ item, disabled, added, onStartManualAddition }) {
  const { row } = item;
  const canAdd = row.kind !== "EM";
  return (
    <article className="border-t border-slate-200 py-4">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2"><code className="font-mono text-sm font-semibold text-slate-950">{row.code}</code><Pill tone={row.status === "held" ? "bg-amber-50 text-amber-800 ring-amber-200" : "bg-rose-50 text-rose-800 ring-rose-200"}>engine {row.status}</Pill><Pill>read-only exclusion</Pill></div>
          <p className="mt-1 text-sm text-slate-700">{row.label}</p>
          <Why row={row} />
        </div>
        {canAdd && <button type="button" disabled={disabled || added} onClick={onStartManualAddition} className="inline-flex min-h-10 items-center gap-1 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-50"><Plus size={14} aria-hidden="true" />{added ? "Manual review started" : "Review as manual addition"}</button>}
      </div>
      <p className="mt-2 text-xs text-slate-600">This held/removed row is not converted into feedback automatically. Adding it requires a separate human-authored rationale and evidence record below.</p>
    </article>
  );
}

function PreparedPreview({ payload, engine, receipt, onRetry, saving }) {
  if (!payload) return null;
  const prepared = canPrepareCoderPreview(engine);
  return (
    <section className="rounded-2xl border border-violet-200 bg-violet-50 p-5 shadow-sm" aria-labelledby="prepared-preview-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 id="prepared-preview-heading" className="flex items-center gap-2 font-semibold text-violet-950"><FileCheck2 size={18} aria-hidden="true" />{prepared ? "Prepared draft — not transmitted" : "Coder decisions — completeness unresolved"}</h2><p className="mt-1 text-sm text-violet-900">{prepared ? "This human-readable preview is assembled from the coder’s decisions." : "These decisions can be recorded for review. Documentation completeness has not passed, so no prepared draft is produced."}</p></div>
        <Pill tone="bg-white text-violet-900 ring-violet-300">{prepared ? "prepared_not_transmitted" : "Completeness unresolved"}</Pill>
      </div>
      <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
        <div><dt className="text-xs uppercase tracking-wide text-violet-700">Encounter</dt><dd className="mt-1 font-mono">{payload.encounter_id}</dd></div>
        <div><dt className="text-xs uppercase tracking-wide text-violet-700">Encounter version</dt><dd className="mt-1">{payload.encounter_version}</dd></div>
        <div><dt className="text-xs uppercase tracking-wide text-violet-700">Engine</dt><dd className="mt-1 break-words font-mono text-xs">{engine.engineVersion || "Not reported"}</dd></div>
        <div><dt className="text-xs uppercase tracking-wide text-violet-700">Review ID</dt><dd className="mt-1 break-all font-mono text-xs">{payload.review_id}</dd></div>
        <div><dt className="text-xs uppercase tracking-wide text-violet-700">Review time</dt><dd className="mt-1">{payload.time_spent_seconds}s</dd></div>
        <div><dt className="text-xs uppercase tracking-wide text-violet-700">Completeness</dt><dd className="mt-1">{completenessLabel(engine.completeness)}</dd></div>
      </dl>
      <h3 className="mt-4 text-xs font-semibold uppercase tracking-wide text-violet-800">Human decisions</h3>
      <ul className="mt-2 space-y-2 text-sm text-violet-950">
        {payload.decisions.map((decision, index) => (
          <li key={`${decision.kind}-${decision.engine_code || "added"}-${decision.final_code || "removed"}-${index}`} className="rounded-lg bg-white/70 px-3 py-2">
            <strong>{decision.engine_code == null ? "Human-added" : "Engine suggestion"}</strong> · {decision.kind} · <code className="font-mono">{decision.engine_code || decision.final_code}</code> · {decision.action}{decision.final_code && decision.final_code !== decision.engine_code ? <> → <code className="font-mono">{decision.final_code}</code></> : null}
          </li>
        ))}
      </ul>
      <p className="mt-4 text-sm font-semibold text-violet-950">No claim, EHR update, Athena write, clearinghouse request, or payer transmission occurred.</p>
      {receipt && (
        <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-emerald-950" role="status" aria-live="polite">
          <p className="flex items-center gap-2 font-semibold"><Check size={17} aria-hidden="true" />Review recorded · {receipt.replayed ? "exact replay confirmed" : "first write confirmed"}</p>
          <p className="mt-1 text-sm">The server acknowledged review <code className="font-mono text-xs">{receipt.reviewId}</code>. {receipt.headerSignoff ? `These coder decisions are bound to header revision ${receipt.headerSignoff.headerRevision} and its exact source.` : "This receipt confirms coder feedback."} No claim was submitted.</p>
        </div>
      )}
      {receipt && <button type="button" disabled={saving} onClick={onRetry} className="mt-3 inline-flex min-h-10 items-center gap-2 rounded-lg border border-violet-300 bg-white px-3 py-2 text-sm font-medium text-violet-950 hover:bg-violet-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-800 disabled:opacity-60"><RotateCcw size={15} aria-hidden="true" />{saving ? "Verifying…" : "Verify exact replay"}</button>}
    </section>
  );
}

export default function CoderReview({
  encounterId, encounters = [], live, statuses = {}, onSelect = NOOP,
  onStatusChange = NOOP, onNavigationLockChange = NOOP,
}) {
  const [engine, setEngine] = useState(null);
  const [decisions, setDecisions] = useState({});
  const [additions, setAdditions] = useState([]);
  const [seconds, setSeconds] = useState(0);
  const [receipt, setReceipt] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [saveError, setSaveError] = useState(null);
  const [saveErrorStatus, setSaveErrorStatus] = useState(null);
  const [saving, setSaving] = useState(false);
  const [showValidation, setShowValidation] = useState(false);
  const [frozen, setFrozen] = useState(false);
  const [headerEncounter, setHeaderEncounter] = useState(null);
  const [headerState, setHeaderState] = useState(null);
  const [headerChoices, setHeaderChoices] = useState({});
  const [baselineChoices, setBaselineChoices] = useState({});
  const [headerAcknowledged, setHeaderAcknowledged] = useState(false);
  const [headerRefresh, setHeaderRefresh] = useState(0);
  const onHeaderState = useCallback((state) => setHeaderState(state), []);
  const headerIdentity = useRef(null);
  const loadRequest = useRef(0);
  const saveRequest = useRef(0);
  const saveAbort = useRef(null);
  const savingRef = useRef(false);
  const reviewId = useRef(null);
  const pendingPayload = useRef(null);
  const pendingHeaderRequest = useRef(null);
  const validationRef = useRef(null);
  const receiptRef = useRef(null);
  const activeLoadEncounter = useRef(null);

  useEffect(() => {
    const requestId = ++loadRequest.current;
    ++saveRequest.current;
    saveAbort.current?.abort();
    const controller = new AbortController();
    setEngine(null); setDecisions({}); setAdditions([]); setSeconds(0);
    setReceipt(null); setLoadError(null); setSaveError(null); setSaveErrorStatus(null);
    setSaving(false); setShowValidation(false); setFrozen(false);
    setHeaderEncounter(null); setHeaderState(null); setHeaderChoices({}); setBaselineChoices({});
    setHeaderAcknowledged(false); setHeaderRefresh(0); headerIdentity.current = null;
    onNavigationLockChange(false);
    savingRef.current = false;
    pendingPayload.current = null; pendingHeaderRequest.current = null; reviewId.current = null;
    const cancelRequests = () => {
      if (loadRequest.current === requestId) ++loadRequest.current;
      controller.abort();
      saveAbort.current?.abort();
      ++saveRequest.current;
      if (activeLoadEncounter.current === encounterId) {
        onStatusChange(encounterId, { processingStatus: "Coder review canceled" });
        activeLoadEncounter.current = null;
      }
    };
    if (!encounterId) return cancelRequests;
    if (!live) {
      setLoadError({ encounterId, message: "The coder service is unavailable. No browser fallback or canned result was loaded." });
      onStatusChange(encounterId, { processingStatus: "Service unavailable", reviewState: "Needs review" });
      return cancelRequests;
    }
    activeLoadEncounter.current = encounterId;
    onStatusChange(encounterId, { processingStatus: "Loading coder review", reviewState: "Needs review" });
    (async () => {
      try {
        const [response, detail] = await Promise.all([
          api("/api/encounters/coder-view", { method: "POST", body: { encounterId }, signal: controller.signal }),
          api("/api/encounters/detail", { method: "POST", body: { encounterId }, signal: controller.signal }),
        ]);
        if (requestId !== loadRequest.current) return;
        const listed = encounters.find((item) => item.id === encounterId);
        if (response.encounterId !== encounterId) throw new Error("encounter-mismatch-response");
        if (!listed || !isSyntheticDemoEncounter(response) || response.dataClass !== listed.dataClass) throw new Error("synthetic-boundary-response");
        if (!Number.isInteger(response.encounterVersion) || !response.reviewContext || !response.completeness) throw new Error("incomplete-review-context");
        if (detail.id !== encounterId || detail.encounterVersion !== response.encounterVersion
          || detail.dataClass !== response.dataClass) throw new Error("source-version-response");
        const reviewable = reviewableCoderView(response);
        if (reviewable.contractViolations > 0) throw new Error("evidence-contract-response");
        const initial = initialCoderDraft(reviewable);
        reviewId.current = globalThis.crypto.randomUUID();
        setDecisions(initial.decisions);
        setEngine(reviewable);
        setHeaderEncounter({...detail, encounterId: detail.id});
        onStatusChange(encounterId, {
          completenessState: completenessLabel(reviewable.completeness),
          processingStatus: reviewable.degraded ? "Inference unavailable" : "Ready for coder decisions",
          reviewState: reviewable.degraded ? "Awaiting provider clarification" : "Needs review",
        });
        activeLoadEncounter.current = null;
      } catch (error) {
        if (error?.name === "AbortError" || requestId !== loadRequest.current) return;
        setLoadError({ encounterId, message: coderErrorMessage(error, "load") });
        onStatusChange(encounterId, { processingStatus: "Coder load failed", reviewState: "Needs review" });
        activeLoadEncounter.current = null;
      }
    })();
    return cancelRequests;
  }, [encounterId, live, encounters, onStatusChange, onNavigationLockChange]);

  const headerEnabled = Boolean(headerEncounter?.headerSource);
  const headerView = headerState?.view;
  const proposals = useMemo(() => headerView ? headerProposals(headerView) : [], [headerView]);
  const proposedOrder = headerView ? proposedDiagnosisOrder(headerView) : [];
  const proposalIds = headerView ? headerDecisionIds(headerView) : [];
  const missingBaseline = useMemo(() => headerView && engine
    ? missingHeaderBaseline(headerView, capturedRecommendations(engine)
      .map(({kind, row}) => ({kind, engine_code: row.code, action: "unreviewed"})))
    : [], [headerView, engine]);
  const headerReady = !headerEnabled || Boolean(headerView && !headerView.signoff
    && !headerState.dirty && !headerState.busy && !headerState.stale && headerAcknowledged
    && proposalIds.every((id) => ["accept", "reject"].includes(headerChoices[id]?.decision)
      && headerChoices[id]?.reason?.trim())
    && missingBaseline.every((row) => ["retain", "reject"].includes(baselineChoices[row.id]?.decision)
      && baselineChoices[row.id]?.reason?.trim()));

  useEffect(() => {
    if (headerState?.dirty || headerState?.stale) setHeaderAcknowledged(false);
    if (!headerView) return;
    const identity = `${headerView.header.headerId}:${headerView.revisionNumber}`;
    if (identity !== headerIdentity.current) {
      headerIdentity.current = identity;
      setHeaderChoices({}); setBaselineChoices({}); setHeaderAcknowledged(false);
    }
    if (receipt?.headerSignoff && (receipt.headerSignoff.headerId !== headerView.header.headerId
      || receipt.headerSignoff.headerRevision !== headerView.revisionNumber
      || (headerView.signoff && receipt.reviewId !== headerView.signoff.reviewId))) {
      // A saved edit starts a fresh, entirely explicit review of the current revision.
      pendingPayload.current = null; pendingHeaderRequest.current = null;
      reviewId.current = globalThis.crypto.randomUUID();
      setReceipt(null); setFrozen(false); setSaveError(null); setSaveErrorStatus(null);
      setDecisions(initialCoderDraft(engine).decisions); setAdditions([]); setSeconds(0);
      setShowValidation(false); onNavigationLockChange(false);
    }
  }, [headerView, headerState?.dirty, headerState?.stale, receipt, engine, onNavigationLockChange]);

  useEffect(() => {
    if (!engine || engine.degraded || receipt) return undefined;
    const timer = globalThis.setInterval(() => setSeconds((value) => value + 1), 1000);
    return () => globalThis.clearInterval(timer);
  }, [engine, receipt]);

  const validation = useMemo(
    () => validateCoderDraft(engine, decisions, additions, {allowEmpty: missingBaseline.length > 0
      && missingBaseline.every((row) => baselineChoices[row.id]?.decision === "reject"
        && baselineChoices[row.id]?.reason?.trim())}),
    [engine, decisions, additions, missingBaseline, baselineChoices],
  );
  const queueState = coderQueueState({ engine, draftValid: validation.valid && headerReady, saved: Boolean(receipt) });

  useEffect(() => {
    if (encounterId && engine?.encounterId === encounterId) {
      onStatusChange(encounterId, { reviewState: queueState });
    }
  }, [encounterId, engine, queueState, onStatusChange]);

  useEffect(() => {
    if (receipt) receiptRef.current?.focus();
  }, [receipt]);

  useEffect(() => {
    if (!frozen || receipt) return undefined;
    const warnBeforeUnload = (event) => {
      event.preventDefault();
      event.returnValue = true;
    };
    globalThis.addEventListener("beforeunload", warnBeforeUnload);
    return () => globalThis.removeEventListener("beforeunload", warnBeforeUnload);
  }, [frozen, receipt]);

  const captured = capturedRecommendations(engine);
  const excluded = excludedRecommendations(engine);
  const issuesFor = (key) => validation.issues.filter((item) => item.key === key).map((item) => item.message);
  const updateDecision = (key, value) => setDecisions((current) => ({ ...current, [key]: value }));
  const startManualAddition = (item) => setAdditions((current) => (
    current.some((addition) => addition.sourceKey === item.key)
      ? current
      : [...current, {
        id: globalThis.crypto.randomUUID(), sourceKey: item.key,
        sourceDisposition: item.row.status, kind: item.row.kind,
        code: item.row.code, rationale: "", evidence: "",
      }]
  ));

  const draftPayload = engine && validation.valid && reviewId.current
    ? buildFeedbackPayload({ engine, decisions, additions, reviewId: reviewId.current, seconds })
    : null;
  const visiblePayload = pendingPayload.current || draftPayload;

  const saveReview = async () => {
    if (!engine || savingRef.current) return;
    const verifyingReplay = Boolean(receipt);
    if (!pendingPayload.current) {
      if (!validation.valid) {
        setShowValidation(true);
        globalThis.requestAnimationFrame(() => validationRef.current?.focus());
        return;
      }
      const candidate = buildFeedbackPayload({
        engine, decisions, additions, reviewId: reviewId.current, seconds,
      });
      if (headerEnabled) {
        if (!headerReady) {
          setSaveError("Generate or save the current header, review every human code proposal and earlier suggestion, and confirm your header review before recording it.");
          return;
        }
        try {
          pendingHeaderRequest.current = headerReviewRequest(headerView, candidate,
            proposalIds.map((id) => ({id, ...headerChoices[id]})), headerAcknowledged,
            missingBaseline.map((row) => ({id: row.id, ...baselineChoices[row.id]})));
        } catch { setSaveError("Header decisions must match final code outcomes. Accepting diagnosis order requires the proposed primary to be accepted and every final diagnosis to appear in the saved order. Rejected primary codes cannot be replaced by a secondary. Edit and save the header if the intended order changed."); return; }
      }
      pendingPayload.current = candidate;
      setFrozen(true);
      onNavigationLockChange(true);
    }
    const payload = pendingPayload.current;
    const requestId = ++saveRequest.current;
    const controller = new AbortController();
    saveAbort.current?.abort();
    saveAbort.current = controller;
    savingRef.current = true;
    setSaving(true); setSaveError(null); setSaveErrorStatus(null);
    onStatusChange(encounterId, { processingStatus: "Saving coder review" });
    try {
      const composed = pendingHeaderRequest.current;
      const response = await api(composed ? "/api/encounters/header/signoff" : "/api/feedback",
        { method: "POST", body: composed || payload, signal: controller.signal });
      if (requestId !== saveRequest.current || encounterId !== payload.encounter_id) return;
      if (response.ok !== true || response.reviewId !== payload.review_id || typeof response.replayed !== "boolean") throw new Error("invalid-feedback-receipt");
      if (composed) {
        const verified = readBillingHeader(response.view, headerEncounter);
        if (!verified?.signoff || verified.signoff.reviewId !== payload.review_id
          || verified.header.headerId !== composed.headerId || verified.revisionNumber !== composed.expectedRevision
          || JSON.stringify(response.headerSignoff) !== JSON.stringify(verified.signoff)) throw new Error("invalid-header-receipt");
        setHeaderRefresh((value) => value + 1);
      }
      setReceipt({ ...response, payload });
      onNavigationLockChange(false);
      onStatusChange(encounterId, { processingStatus: response.replayed ? "Exact replay confirmed" : "Review recorded", reviewState: coderQueueState({ engine, draftValid: true, saved: true }) });
    } catch (error) {
      if (error?.name === "AbortError" || requestId !== saveRequest.current) return;
      setSaveError(coderErrorMessage(error, verifyingReplay ? "verify" : "save"));
      setSaveErrorStatus(error?.status || null);
      if (pendingHeaderRequest.current && [403, 404, 409].includes(error?.status)) {
        setReceipt(null); setHeaderRefresh((value) => value + 1);
      }
      onStatusChange(encounterId, {
        processingStatus: verifyingReplay ? "Replay verification unavailable" : "Save failed",
        ...(verifyingReplay ? { reviewState: coderQueueState({ engine, draftValid: true, saved: true }) } : {}),
      });
      if (!verifyingReplay && error?.status === 422) {
        pendingPayload.current = null;
        pendingHeaderRequest.current = null;
        setFrozen(false);
        onNavigationLockChange(false);
      }
    } finally {
      if (requestId === saveRequest.current) {
        savingRef.current = false;
        setSaving(false);
      }
    }
  };

  const reloadAfterDefinitiveFailure = () => {
    pendingPayload.current = null;
    pendingHeaderRequest.current = null;
    setFrozen(false);
    onNavigationLockChange(false);
    globalThis.location.reload();
  };

  return (
    <div className="space-y-5">
      <ProviderQueue encounters={encounters} selectedId={encounterId} statuses={statuses} onSelect={onSelect} selectionDisabled={frozen && !receipt} title="Coder work queue" description="Choose a visit, make an explicit decision for every recommendation, then record the review." />

      {!encounterId ? (
        <section className="rounded-2xl border border-dashed border-slate-300 bg-white p-7 text-center"><FileCheck2 size={26} className="mx-auto text-slate-500" aria-hidden="true" /><h2 className="mt-2 font-semibold text-slate-950">Choose an encounter for coder review</h2><p className="mt-1 text-sm text-slate-600">No recommendation is accepted until a coder makes an explicit decision.</p></section>
      ) : loadError?.encounterId === encounterId ? <ErrorNotice>{loadError.message}</ErrorNotice> : !engine || engine.encounterId !== encounterId ? (
        <p role="status" className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white p-5 text-sm text-slate-600 shadow-sm"><RefreshCw size={16} className="animate-spin" aria-hidden="true" />Loading evidence-linked coder review…</p>
      ) : engine.degraded ? (
        <section className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-amber-950 shadow-sm"><div className="flex gap-3"><AlertTriangle size={20} className="mt-0.5 shrink-0" aria-hidden="true" /><div><h2 className="font-semibold">No codes proposed for this encounter</h2><p className="mt-1 text-sm">{engine.degraded.detail}</p><p className="mt-2 text-sm">No code recommendations, prepared draft, or feedback actions are shown. Route this encounter to manual review.</p></div></div></section>
      ) : (
        <>
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="coder-review-heading">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div><h1 id="coder-review-heading" className="text-lg font-semibold text-slate-950">Coder review · {engine.encounterId}</h1><p className="mt-1 text-sm text-slate-600">Review each evidence-linked recommendation{headerEnabled ? " and the saved billing header" : ""}. An on-screen prepared preview also requires a completeness pass; nothing is transmitted.</p></div>
              <div className="flex flex-wrap items-center gap-2"><Pill><Clock size={12} className="mr-1" aria-hidden="true" />{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}</Pill><Pill tone="bg-violet-50 text-violet-800 ring-violet-200">{sourceLabel(engine.dataClass)}</Pill><Pill>version {engine.encounterVersion}</Pill></div>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <div className="rounded-xl bg-slate-50 p-3"><p className="text-xs font-medium uppercase tracking-wide text-slate-500">Source E/M</p><p className="mt-1 font-mono text-lg font-semibold">{engine.submittedEm || "None"}</p></div>
              <div className="rounded-xl bg-slate-50 p-3"><p className="text-xs font-medium uppercase tracking-wide text-slate-500">Engine E/M</p><p className="mt-1 font-mono text-lg font-semibold">{engine.em?.code || "None"}</p></div>
              <div className="rounded-xl bg-slate-50 p-3"><p className="text-xs font-medium uppercase tracking-wide text-slate-500">Queue state</p><p className="mt-1 text-sm font-semibold">{queueState}</p></div>
            </div>
            <div className={`mt-4 rounded-xl border p-4 text-sm ${engine.completeness.complete !== true ? "border-rose-200 bg-rose-50 text-rose-900" : "border-slate-200 bg-slate-50 text-slate-700"}`}><strong>Documentation completeness:</strong> {completenessLabel(engine.completeness)}{engine.completeness.complete !== true ? " — you can record coder feedback; an explicit pass is required before a prepared state." : ""}</div>
            {engine.suppressedExclusions > 0 && <p className="mt-3 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"><ShieldCheck size={15} className="mr-1 inline" aria-hidden="true" />{engine.suppressedExclusions} engine candidate{engine.suppressedExclusions === 1 ? " is" : "s are"} not shown because no cited documentation evidence was returned. It cannot enter the prepared preview.</p>}
          </section>

          <DiagnosisSequencingNotice value={engine.diagnosisSequencing} />
          {headerEnabled && <>
            <BillingHeader encounter={headerEncounter} onStateChange={onHeaderState}
              disabled={frozen && !receipt || saving} refreshKey={headerRefresh} />
            <section className="rounded-2xl border border-teal-200 bg-teal-50 p-5" aria-labelledby="header-confirmation-heading">
              <h2 id="header-confirmation-heading" className="font-semibold text-teal-950">Review the saved header</h2>
              <p className="mt-1 text-sm text-teal-900">Review the facts, evidence, and documentation warnings above. For each human code proposal, record a decision and reason, then make matching code decisions below.</p>
              {missingBaseline.length > 0 && <div className="mt-4" aria-label="Earlier header suggestions">
                <h3 className="font-semibold text-teal-950">Earlier header suggestions need reconciliation</h3>
                <p className="mt-1 text-sm text-teal-900">The current pipeline run does not include these original header codes. The source and saved edits are unchanged. Review each earlier code explicitly. Retaining one requires a separate human-added code with rationale and evidence below, or an edit to the current E/M. Choosing “Retain” alone adds nothing.</p>
                {missingBaseline.map((row, index) => <fieldset key={row.id} disabled={frozen || headerState?.dirty || headerState?.busy || headerState?.stale || Boolean(headerView?.signoff)} className="mt-3 grid gap-3 rounded-xl border border-amber-300 bg-white p-3 sm:grid-cols-2">
                  <legend className="px-1 text-sm font-semibold">Earlier suggestion {index + 1}: {row.kind} {row.code}</legend>
                  <p className="text-sm text-slate-700 sm:col-span-2">{row.label} · Original pipeline suggestion; absent from the current review.</p>
                  {row.evidenceIds.map((id) => <blockquote key={id} className="border-l-2 border-slate-300 pl-2 text-sm text-slate-600 sm:col-span-2">{headerView.header.evidence.find((e) => e.id === id).text}</blockquote>)}
                  <label className="grid gap-1 text-xs font-medium">Earlier suggestion {index + 1} decision
                    <select aria-label={`Earlier suggestion ${index + 1} decision`} className="min-h-10 rounded-lg border border-slate-300 bg-white px-2 text-sm" value={baselineChoices[row.id]?.decision || ""} onChange={(event) => {setHeaderAcknowledged(false); setBaselineChoices((current) => ({...current, [row.id]: {...current[row.id], decision: event.target.value}}));}}>
                      <option value="">Choose a decision</option><option value="retain">Retain through a human code decision</option><option value="reject">Reject earlier suggestion</option>
                    </select>
                  </label>
                  <label className="grid gap-1 text-xs font-medium">Earlier suggestion {index + 1} reason
                    <input className="min-h-10 rounded-lg border border-slate-300 px-2 text-sm" maxLength={1000} value={baselineChoices[row.id]?.reason || ""} onChange={(event) => {setHeaderAcknowledged(false); setBaselineChoices((current) => ({...current, [row.id]: {...current[row.id], reason: event.target.value}}));}} />
                  </label>
                </fieldset>)}
              </div>}
              {proposals.map((proposal, index) => <fieldset key={proposal.id} disabled={frozen || !headerView || headerState?.dirty || headerState?.busy || headerState?.stale || Boolean(headerView?.signoff)} className="mt-3 grid gap-3 rounded-xl border border-teal-200 bg-white p-3 sm:grid-cols-2">
                <legend className="px-1 text-sm font-semibold">Header proposal {index + 1}: {proposal.original?.code || "New code"} → {proposal.proposed?.code || "Removal"}</legend>
                <p className="text-sm text-slate-700 sm:col-span-2">{proposal.proposed?.label || proposal.original?.label}</p>
                <label className="grid gap-1 text-xs font-medium">Header proposal {index + 1} decision
                  <select aria-label={`Header proposal ${index + 1} decision`} className="min-h-10 rounded-lg border border-slate-300 bg-white px-2 text-sm" value={headerChoices[proposal.id]?.decision || ""} onChange={(event) => {setHeaderAcknowledged(false); setHeaderChoices((current) => ({...current, [proposal.id]: {...current[proposal.id], decision: event.target.value}}));}}>
                    <option value="">Choose a decision</option><option value="accept">Accept human proposal</option><option value="reject">Reject human proposal</option>
                  </select>
                </label>
                <label className="grid gap-1 text-xs font-medium">Header proposal {index + 1} reason
                  <input className="min-h-10 rounded-lg border border-slate-300 px-2 text-sm" maxLength={1000} value={headerChoices[proposal.id]?.reason || ""} onChange={(event) => {setHeaderAcknowledged(false); setHeaderChoices((current) => ({...current, [proposal.id]: {...current[proposal.id], reason: event.target.value}}));}} />
                </label>
              </fieldset>)}
              {proposedOrder.length > 0 && <fieldset disabled={frozen || headerState?.dirty || headerState?.busy || headerState?.stale || Boolean(headerView?.signoff)} className="mt-3 grid gap-3 rounded-xl border border-violet-300 bg-white p-3 sm:grid-cols-2">
                <legend className="px-1 text-sm font-semibold">Human diagnosis order</legend>
                <p className="text-sm sm:col-span-2">Proposed primary: <code>{proposedOrder[0].code}</code> · {proposedOrder.map((row) => row.code).join(" → ")}. Accept only after matching code decisions below. A rejected primary leaves sequencing unresolved; edit the header to propose a different primary.</p>
                {proposedOrder.map((row) => <div key={row.id} className="text-xs sm:col-span-2"><strong>{row.code}</strong>{row.evidenceIds.map((id) => <blockquote key={id} className="mt-1 border-l-2 border-slate-300 pl-2">{headerView.header.evidence.find((e) => e.id === id).text}</blockquote>)}</div>)}
                <label className="grid gap-1 text-xs font-medium">Diagnosis order decision
                  <select aria-label="Diagnosis order decision" className="min-h-10 rounded-lg border border-slate-300 bg-white px-2 text-sm" value={headerChoices[diagnosisOrderDecisionId]?.decision || ""} onChange={(event) => {setHeaderAcknowledged(false); setHeaderChoices((current) => ({...current, [diagnosisOrderDecisionId]: {...current[diagnosisOrderDecisionId], decision: event.target.value}}));}}>
                    <option value="">Choose a decision</option><option value="accept">Accept proposed primary and order</option><option value="reject">Reject order; primary unresolved</option>
                  </select>
                </label>
                <label className="grid gap-1 text-xs font-medium">Diagnosis order reason
                  <input className="min-h-10 rounded-lg border border-slate-300 px-2 text-sm" maxLength={1000} value={headerChoices[diagnosisOrderDecisionId]?.reason || ""} onChange={(event) => {setHeaderAcknowledged(false); setHeaderChoices((current) => ({...current, [diagnosisOrderDecisionId]: {...current[diagnosisOrderDecisionId], reason: event.target.value}}));}} />
                </label>
              </fieldset>}
              <label className="mt-4 flex items-start gap-2 text-sm font-medium text-teal-950"><input className="mt-1" type="checkbox" checked={headerAcknowledged} disabled={frozen || !headerView || headerState?.dirty || headerState?.busy || headerState?.stale || Boolean(headerView?.signoff)} onChange={(event) => setHeaderAcknowledged(event.target.checked)} />I reviewed this header’s facts, code proposals, evidence, and documentation warnings.</label>
              {headerView?.signoff && <p className="mt-3 text-sm text-teal-900">This header revision already has a recorded coder review. A saved header edit requires a fresh review.</p>}
              {headerState?.dirty && <p className="mt-3 text-sm text-teal-900">Save or cancel the header edits before recording the review.</p>}
            </section>
          </>}

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="captured-heading"><h2 id="captured-heading" className="font-semibold text-slate-950">Engine suggestions</h2><p className="mt-1 text-sm text-slate-600">Each row starts unreviewed. Accept, edit, or reject it affirmatively.</p>{captured.length ? captured.map((item) => <CodeRow key={item.key} item={item} decision={decisions[item.key]} disabled={frozen} issues={showValidation ? issuesFor(item.key) : []} primaryLabel={primaryBadge(engine.diagnosisSequencing)} onChange={(value) => updateDecision(item.key, value)} />) : <p className="mt-3 text-sm text-slate-600">No evidence-supported engine suggestions are available.</p>}</section>

          {excluded.length > 0 && <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="excluded-heading"><h2 id="excluded-heading" className="font-semibold text-slate-950">Engine-held and removed candidates</h2><p className="mt-1 text-sm text-slate-600">These exclusions are read-only because the feedback contract has no “hold” action. A coder may start a separate manual addition, which still requires rationale and evidence.</p>{excluded.map((item) => <ExcludedRow key={item.key} item={item} disabled={frozen} added={additions.some((addition) => addition.sourceKey === item.key)} onStartManualAddition={() => startManualAddition(item)} />)}</section>}

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="manual-heading">
            <h2 id="manual-heading" className="font-semibold text-slate-950">Human-added codes</h2><p className="mt-1 text-sm text-slate-600">Every addition requires a rationale and a supporting synthetic documentation excerpt. Human additions remain separate from engine suggestions.</p>
            <div className="mt-3 space-y-3">{additions.map((addition, index) => {
              const key = `addition:${addition.id}`;
              const rowIssues = showValidation ? issuesFor(key) : [];
              const errorId = `${key}-errors`;
              const invalid = rowIssues.length > 0;
              const describedBy = invalid ? errorId : undefined;
              return (
                <fieldset key={addition.id} aria-describedby={describedBy} className="grid gap-3 rounded-xl border border-slate-200 p-4 md:grid-cols-2">
                  <legend className="sr-only">Human-added code {index + 1}</legend>
                  <label htmlFor={`${key}-kind`} className="grid gap-1 text-xs font-medium text-slate-700">Code type
                    <select id={`${key}-kind`} aria-label="Code type" disabled={frozen} aria-invalid={invalid || undefined} aria-describedby={describedBy} value={addition.kind} onChange={(event) => setAdditions((current) => current.map((item) => item.id === addition.id ? { ...item, kind: event.target.value } : item))} className="min-h-10 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm">{MANUAL_KINDS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}{!engine.em && <option value="EM">E/M (human addition)</option>}</select>
                  </label>
                  <label htmlFor={`${key}-code`} className="grid gap-1 text-xs font-medium text-slate-700">Code
                    <input id={`${key}-code`} required maxLength={32} disabled={frozen} aria-invalid={invalid || undefined} aria-describedby={describedBy} value={addition.code} onChange={(event) => setAdditions((current) => current.map((item) => item.id === addition.id ? { ...item, code: event.target.value } : item))} className="min-h-10 rounded-md border border-slate-300 px-2 py-1 font-mono text-sm" />
                  </label>
                  <label htmlFor={`${key}-rationale`} className="grid gap-1 text-xs font-medium text-slate-700 md:col-span-2">Rationale
                    <textarea id={`${key}-rationale`} required maxLength={1900} disabled={frozen} aria-invalid={invalid || undefined} aria-describedby={describedBy} value={addition.rationale} onChange={(event) => setAdditions((current) => current.map((item) => item.id === addition.id ? { ...item, rationale: event.target.value } : item))} rows={2} className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
                  </label>
                  <label htmlFor={`${key}-evidence`} className="grid gap-1 text-xs font-medium text-slate-700 md:col-span-2">Supporting documentation evidence
                    <textarea id={`${key}-evidence`} required maxLength={1900} disabled={frozen} aria-invalid={invalid || undefined} aria-describedby={describedBy} value={addition.evidence} onChange={(event) => setAdditions((current) => current.map((item) => item.id === addition.id ? { ...item, evidence: event.target.value } : item))} rows={2} className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
                  </label>
                  <button type="button" disabled={frozen} aria-label={`Remove human-added code ${index + 1}`} onClick={() => setAdditions((current) => current.filter((item) => item.id !== addition.id))} className="inline-flex min-h-10 w-fit items-center gap-1 rounded-lg px-2 text-sm text-rose-800 hover:bg-rose-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-700 disabled:opacity-50"><X size={15} aria-hidden="true" />Remove</button>
                  {invalid && <div className="md:col-span-2"><RowIssues id={errorId} issues={rowIssues} /></div>}
                </fieldset>
              );
            })}</div>
            <button type="button" disabled={frozen} onClick={() => setAdditions((current) => [...current, { id: globalThis.crypto.randomUUID(), sourceKey: null, kind: "ICD10", code: "", rationale: "", evidence: "" }])} className="mt-3 inline-flex min-h-10 items-center gap-1 rounded-lg px-2 text-sm font-medium text-sky-800 hover:bg-sky-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-700 disabled:opacity-50"><Plus size={15} aria-hidden="true" />Add code</button>
          </section>

          {showValidation && validation.issues.length > 0 && <div ref={validationRef} tabIndex={-1} role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-900 focus:outline-none"><p className="font-semibold">Complete the review before saving</p><ul className="mt-2 list-disc space-y-1 pl-5">{validation.issues.map((item, index) => <li key={`${item.key}-${index}`}>{item.message}</li>)}</ul></div>}
          {saveError && <ErrorNotice>{saveError}{frozen && !receipt && (saveErrorStatus == null || saveErrorStatus >= 500) && <p className="mt-2 font-semibold">The server outcome is unknown. Keep this review open and use “Retry exact saved review.” Do not start a replacement review with a new ID.</p>}{frozen && !receipt && saveErrorStatus === 401 && <p className="mt-2"><a href="/.auth/login/aad?post_login_redirect_uri=/" target="_blank" rel="noreferrer" className="font-semibold underline underline-offset-2">Sign in again in a new tab</a>, then return here and retry the exact saved review.</p>}{frozen && !receipt && [403, 404, 409].includes(saveErrorStatus) && <button type="button" onClick={reloadAfterDefinitiveFailure} className="mt-3 flex min-h-10 items-center gap-2 rounded-lg border border-rose-300 bg-white px-3 py-2 font-medium hover:bg-rose-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-700"><RefreshCw size={15} aria-hidden="true" />{saveErrorStatus === 409 ? "Reload after confirmed conflict" : "Return to the refreshed workspace"}</button>}{saveErrorStatus === 409 && receipt && <p className="mt-2">Reload the encounter before starting another review.</p>}</ErrorNotice>}

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="save-heading"><div className="flex flex-wrap items-center justify-between gap-4"><div><h2 id="save-heading" className="font-semibold text-slate-950">Record coder review</h2><p className="mt-1 text-sm text-slate-600">Saving records your decisions{headerEnabled ? " with this exact header revision" : ""}. If confirmation is interrupted, retry the same review.</p></div><button type="button" disabled={saving || Boolean(receipt) || (Boolean(headerView?.signoff) && !frozen)} onClick={saveReview} className="inline-flex min-h-11 items-center gap-2 rounded-lg bg-slate-950 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-60"><Save size={16} aria-hidden="true" />{saving ? "Saving…" : frozen ? "Retry exact saved review" : headerEnabled ? "Record coder review and header" : canPrepareCoderPreview(engine) ? "Save review and prepare preview" : "Save coder review"}</button></div><p className="mt-3 text-xs text-slate-600">This action records coder feedback{headerEnabled ? " and a header review receipt" : ""}. The clinical note remains unchanged. Nothing is written to Athena or an EHR, and no claim is transmitted.</p></section>

          <div ref={receiptRef} tabIndex={receipt ? -1 : undefined} className="focus:outline-none"><PreparedPreview payload={visiblePayload} engine={engine} receipt={receipt} onRetry={saveReview} saving={saving} /></div>
        </>
      )}
    </div>
  );
}
