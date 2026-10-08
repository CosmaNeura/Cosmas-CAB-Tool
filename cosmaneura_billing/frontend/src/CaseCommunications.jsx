import React, { useEffect, useRef, useState } from "react";
import { MessageSquare, Phone, RefreshCw } from "lucide-react";
import { api } from "./api.js";
import { chaseAttemptTime } from "./chaseActivity.js";
import CommunicationsEventReview from "./CommunicationsEventReview.jsx";
import {
  SMS_MAX_LENGTH, SMS_DEMO_EVENTS, VOICE_DEMO_EVENTS, communicationError,
  communicationsDeliveryError, communicationsDisposition, communicationsReason, communicationsStatus, readCommunications, smsThread,
} from "./caseCommunications.js";

const BASE = "/api/chase/communications";
const button = "rounded-lg border border-slate-300 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50";
const primary = "rounded-lg bg-sky-800 px-3 py-2 text-sm font-medium text-white hover:bg-sky-900 disabled:cursor-not-allowed disabled:opacity-50";
const field = "mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 disabled:bg-slate-50";

function OperationPicker({ label, operations, value, onChange, disabled }) {
  return <label className="block text-xs text-slate-600">{label}
    <select aria-label={label} className={`${field} min-w-0 max-w-full`} value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled || !operations.length}>
      <option value="">Select a simulation</option>
      {operations.map((item, index) => <option key={item.operationId} value={item.operationId}>#{index + 1} · {chaseAttemptTime(item.createdAt)} · {item.unknownResolution ? "Original outcome unknown · investigated" : communicationsStatus(item.status)}</option>)}
    </select>
  </label>;
}

function DeliveryError({ item }) {
  const message = communicationsDeliveryError(item.deliveryError, item.status);
  return message ? <p className="mt-2 text-xs text-amber-800" aria-label="Delivery problem">{message}</p> : null;
}


function AuthorizeDraft({ operationId, channel, locked, act, availability }) {
  const [confirmed, setConfirmed] = useState(false);
  const unavailable = availability?.allowed === false;
  useEffect(() => { setConfirmed(false); }, [operationId, availability?.allowed, availability?.reason]);
  return <div className="mt-3 border-t border-slate-200 pt-2 text-xs">
    <label className="flex items-start gap-2"><input type="checkbox" checked={confirmed} disabled={locked || unavailable} onChange={(event) => setConfirmed(event.target.checked)} />
      <span>I reviewed this exact {channel === "sms" ? "message" : "script"}, recipient, and permission.</span></label>
    {unavailable && <p className="mt-2 text-amber-800">{communicationsReason(availability.reason)} This draft is saved; nothing has been sent.</p>}
    <button type="button" disabled={locked || unavailable || !confirmed} className={`${button} mt-2`} onClick={() => act("/authorize", { operationId, confirmed: true },
      "Authorization recorded. Delivery is not yet confirmed.")}>Authorize {channel === "sms" ? "SMS" : "call"}</button>
  </div>;
}

function CancelDraft({ item, channel, locked, act }) {
  if (item.canceledAt) return <p className="mt-2 text-xs text-slate-600">Canceled before dispatch · <time dateTime={item.canceledAt}>{chaseAttemptTime(item.canceledAt)}</time></p>;
  if (!item.cancelable) return null;
  return <button type="button" className={`${button} mt-2`} disabled={locked} onClick={() => act("/cancel", {
    operationId: item.operationId, confirmed: true,
  }, "Canceled before dispatch. No recall was requested from the provider.")}>Cancel {channel === "sms" ? "SMS" : "call"}</button>;
}

function UnknownOutcome({ item, channel, locked, act }) {
  const [decision, setDecision] = useState("");
  const [reference, setReference] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  if (item.unknownResolution) return <div className="mt-2 border-t border-slate-200 pt-2 text-xs text-slate-600">
    <p className="font-medium">Investigation recorded · {({ confirmed_delivered: "Confirmed delivered", confirmed_not_sent: "Confirmed not sent", confirmed_attempted: "Attempt confirmed · delivery not established" })[item.unknownResolution.decision]}</p>
    <p className="mt-1 break-words">Reference: {item.unknownResolution.vendorReference} · {chaseAttemptTime(item.unknownResolution.resolvedAt)}</p>
    <p className="mt-1">Original uncertainty retained. This operation will not be submitted again.</p>
  </div>;
  if (item.status !== "unknown") return null;
  if (!item.canResolveUnknown) return <p className="mt-2 text-xs text-amber-800">Investigation is not available yet. Refresh to check again.</p>;
  const label = channel === "sms" ? "SMS" : "Call";
  return <details className="mt-2 border-t border-slate-200 pt-2 text-xs text-slate-700">
    <summary className="cursor-pointer font-medium">Resolve unknown outcome</summary>
    <p className="mt-2">Check the provider's delivery records before another text or call. Do not infer failure from a timeout.</p>
    <label className="mt-2 block">Confirmed outcome<select aria-label={label + " investigated outcome"} className={field} value={decision} disabled={locked} onChange={(event) => setDecision(event.target.value)}>
      <option value="">Select the confirmed result</option>
      <option value="confirmed_delivered">{channel === "sms" ? "Delivered to recipient" : "Call reached recipient"}</option>
      <option value="confirmed_not_sent">Not sent / call not placed</option>
      <option value="confirmed_attempted">Provider accepted / attempt confirmed; delivery not established</option>
    </select></label>
    <label className="mt-2 block">Vendor investigation reference<input aria-label={label + " investigation reference"} className={field} value={reference} maxLength={200} disabled={locked} onChange={(event) => setReference(event.target.value)} placeholder="Record reference; no patient details" /></label>
    <label className="mt-3 flex items-start gap-2"><input type="checkbox" checked={confirmed} disabled={locked} onChange={(event) => setConfirmed(event.target.checked)} /><span>I confirmed this outcome in the provider's records.</span></label>
    <button type="button" className={button + " mt-2"} disabled={locked || !confirmed || !decision || !/^[A-Za-z0-9_.:/-]{1,200}$/.test(reference)} onClick={() => act("/resolve-unknown", {
      operationId: item.operationId, decision, vendorReference: reference, confirmed: true,
    }, "Investigation recorded. No message or call was submitted.")}>Record investigated outcome</button>
  </details>;
}

function ContactPermission({ route, binding, contact, suppressed, locked, act }) {
  const [checked, setChecked] = useState(false);
  const [decision, setDecision] = useState("");
  const [reference, setReference] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const label = route.channel === "sms" ? "Text messages" : "Automated calls";
  const validDecision = decision === "withdrawn" || (decision === "granted" && !suppressed && !["denied", "expired"].includes(route.consentStatus));
  return <section aria-label={`${label} contact and permission`} className="rounded-lg border border-slate-200 p-3">
    <div className="flex flex-wrap items-start justify-between gap-2">
      <h4 className="text-xs font-semibold text-slate-800">{label}</h4><span className="text-sm tabular-nums text-slate-800">{route.destination}</span>
    </div>
    {binding ? <p className="mt-2 text-xs text-slate-600">Contact verified for this channel. {binding.consentGranted ? "Purpose-specific permission recorded." : "Purpose-specific permission is needed."}</p> : <>
      <label className="mt-3 flex items-start gap-2 text-xs text-slate-700"><input type="checkbox" checked={checked} disabled={locked} onChange={(event) => setChecked(event.target.checked)} /><span>I verified this contact against the current source.</span></label>
      <button type="button" className={`${button} mt-2`} disabled={locked || !checked} onClick={() => act("/bind", {
        channel: route.channel, contactSnapshotId: contact.snapshotId, confirmedContact: true,
      }, "Contact verification recorded. Permission for outreach is a separate step.")}>Verify {route.channel === "sms" ? "text" : "call"} contact</button>
    </>}
    {suppressed && <p className="mt-2 text-xs font-medium text-rose-700">Opt-out active. Recording consent here does not remove it.</p>}
    {["denied", "expired"].includes(route.consentStatus) && <p className="mt-2 text-xs text-amber-800">Source permission is {route.consentStatus}; verify updated source evidence before outreach.</p>}
    {binding && <details className="mt-3 text-xs text-slate-600"><summary className="cursor-pointer font-medium">Record or update permission</summary>
      <label className="mt-2 block">Permission for {label.toLowerCase()}<select aria-label={`Permission for ${label.toLowerCase()}`} className={field} value={decision} disabled={locked} onChange={(event) => setDecision(event.target.value)}>
        <option value="">Select a reviewed decision</option>
        {!suppressed && !["denied", "expired"].includes(route.consentStatus) && <option value="granted">Permission confirmed</option>}
        <option value="withdrawn">Permission withdrawn</option>
      </select></label>
      <label className="mt-2 block">Consent record reference<input aria-label={`${label} consent record reference`} className={field} maxLength={200} value={reference} disabled={locked} onChange={(event) => setReference(event.target.value)} placeholder="Record ID or reference; no patient details" /></label>
      <label className="mt-3 flex items-start gap-2"><input type="checkbox" checked={confirmed} disabled={locked} onChange={(event) => setConfirmed(event.target.checked)} /><span>I reviewed explicit permission for insurance follow-up by {label.toLowerCase()}.</span></label>
      <button type="button" className={`${button} mt-2`} disabled={locked || !confirmed || !validDecision || !/^[A-Za-z0-9_.:/-]{1,200}$/.test(reference)} onClick={() => act("/consent", {
        bindingId: binding.bindingId, granted: decision === "granted", sourceReference: reference, confirmed: true,
      }, "Permission evidence recorded. Other outreach safeguards still apply.")}>Record {route.channel === "sms" ? "text" : "call"} permission</button>
    </details>}
  </section>;
}

function OutreachSetup({ view, locked, act }) {
  const [policyChecked, setPolicyChecked] = useState(false);
  const p = view.policy;
  const routes = view.contact?.routes.filter((route) => view.enabledChannels.includes(route.channel)) || [];
  const voiceEnabled = view.enabledChannels.includes("automated_voice");
  return <details open={!view.contact || !p.approved || routes.some((r) => !view.bindings[r.channel]?.consentGranted)} className="rounded-xl border border-slate-200 p-4">
    <summary className="cursor-pointer text-sm font-semibold text-slate-800">Contact and outreach permissions</summary>
    {view.contact ? <><p className="mt-2 text-xs text-slate-500">Source: {view.contact.source} · Verified {chaseAttemptTime(view.contact.verifiedAt)}. Contact accuracy does not establish consent.</p>
      <div className={`mt-3 grid gap-3 ${routes.length > 1 ? "lg:grid-cols-2" : ""}`}>{routes.map((route) => <ContactPermission key={`${view.contact.snapshotId}:${route.channel}:${view.bindings[route.channel]?.bindingId || ""}:${view.bindings[route.channel]?.consentGranted}`}
        route={route} binding={view.bindings[route.channel]} contact={view.contact} suppressed={view.suppression[route.channel === "sms" ? "sms" : "automatedVoice"]} locked={locked} act={act} />)}</div>
      {!routes.length && <p className="mt-3 text-sm text-slate-500">No contact is available for the enabled communication channels.</p>}
    </> : <p className="mt-3 text-sm text-slate-600">A staff-reviewed contact source is needed before outreach.</p>}
    <details className="mt-4 border-t border-slate-100 pt-3 text-xs text-slate-600">
      <summary className="cursor-pointer font-medium">Outreach policy · {p.approved ? "Approved" : "Administrator approval needed"}</summary>
      <p className="mt-2">{p.practiceName} · Quiet hours {String(p.quietHours[0]).padStart(2, "0")}:00–{String(p.quietHours[1]).padStart(2, "0")}:00 ({p.timezone}). Limit: {p.maxTotalTouches} total attempts per case, {p.maxTouches24h} in 24 hours.</p>
      {p.testDispatchAllowance && <p className="mt-2">Practice-wide SMS test allowance: {p.testDispatchAllowance.remainingStarts} of {p.testDispatchAllowance.maxStarts} sends remaining. Started sends stay reserved even if delivery is unknown. Each test must use one text segment. Provider replies and usage charges require separate reconciliation.</p>}
      <p className="mt-2">Every draft needs a separate staff authorization. Opt-outs and direct-contract screening still block outreach.</p>
      <div className="mt-2 space-y-2">{view.enabledChannels.includes("sms") && view.templates.map((template) => <p key={template.id}><strong>{template.label}:</strong> {template.text}</p>)}{voiceEnabled && <p><strong>Call script:</strong> {view.callScript}</p>}</div>
      {p.canApprove && <><label className="mt-3 flex items-start gap-2"><input type="checkbox" checked={policyChecked} disabled={locked} onChange={(event) => setPolicyChecked(event.target.checked)} /><span>I reviewed the current templates, {voiceEnabled ? "script, " : ""}limits, and quiet hours.</span></label>
        <div className="mt-2 flex gap-2"><button type="button" className={button} disabled={locked || !policyChecked || p.approved} onClick={() => act("/policy/decision", { expectedPolicyHash: p.hash, approved: true }, "Outreach policy approval recorded.")}>Approve outreach policy</button>
          {p.approved && <button type="button" className={button} disabled={locked} onClick={() => act("/policy/decision", { expectedPolicyHash: p.hash, approved: false }, "Outreach policy approval revoked.")}>Revoke approval</button>}</div></>}
    </details>
    {!p.outboundEnabled && <p className="mt-3 text-xs text-amber-800">Outbound communication is not enabled.</p>}
  </details>;
}


export default function CaseCommunications({ task, active, manageChase, blocked, onReviewCounts, onReviewLock, onMutationNavigationLock }) {
  const [view, setView] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [retry, setRetry] = useState(null);
  const [mutationPending, setMutationPending] = useState(false);
  const [requiresRefresh, setRequiresRefresh] = useState(false);
  const [draft, setDraft] = useState("");
  const [reply, setReply] = useState("");
  const [smsOperation, setSmsOperation] = useState("");
  const [voiceOperation, setVoiceOperation] = useState("");
  const [smsEvent, setSmsEvent] = useState("sms_delivered");
  const [voiceEvent, setVoiceEvent] = useState("voice_callback");
  const [reviewAvailable, setReviewAvailable] = useState(task.communications?.mode === "durable");
  const [reviewVersion, setReviewVersion] = useState(0);
  const [reviewLocked, setReviewLocked] = useState(false);
  const [syncError, setSyncError] = useState("");
  const background = useRef(null);
  const reviewCountsCallback = useRef(onReviewCounts);
  reviewCountsCallback.current = onReviewCounts;
  const request = useRef({ sequence: 0, controller: null, busy: false });
  const chaseId = task.chaseId;
  const durable = view ? view.mode === "durable" : task.communications?.mode === "durable";

  useEffect(() => () => {
    request.current.sequence += 1;
    request.current.controller?.abort();
    background.current?.abort();
    request.current.busy = false;
  }, [chaseId]);

  useEffect(() => {
    onReviewLock?.(busy || !!retry || requiresRefresh || reviewLocked);
    return () => onReviewLock?.(false);
  }, [busy, retry, requiresRefresh, reviewLocked, onReviewLock]);

  useEffect(() => {
    onMutationNavigationLock?.(mutationPending || !!retry);
  }, [mutationPending, retry, onMutationNavigationLock]);
  useEffect(() => () => onMutationNavigationLock?.(false), [onMutationNavigationLock]);

  async function run(command = null) {
    if (request.current.busy || blocked) return;
    if (command) {
      // Publish synchronously before the request: changing cases must not
      // unmount the component that owns an uncertain exact command.
      onMutationNavigationLock?.(true);
      setMutationPending(true);
    }
    background.current?.abort();
    background.current = null;
    const sequence = ++request.current.sequence;
    const controller = new AbortController();
    let timedOut = false;
    const deadline = window.setTimeout(() => { timedOut = true; controller.abort(); }, 15_000);
    request.current = { sequence, controller, busy: true };
    setBusy(true); setError(""); setNotice(""); setSyncError("");
    if (!command) setView(null);
    try {
      let result = await api(command ? BASE + command.path : BASE, {
        method: "POST", body: command ? command.body : { chaseId }, signal: controller.signal,
      });
      if (timedOut) throw new Error("Communications request timed out.");
      if (sequence !== request.current.sequence) return;
      if (command?.path === "/policy/decision") result = await api(BASE, {
        method: "POST", body: { chaseId }, signal: controller.signal,
      });
      if (timedOut) throw new Error("Communications request timed out.");
      if (sequence !== request.current.sequence) return;
      const parsed = readCommunications(result, chaseId);
      setView(parsed);
      setReviewAvailable(parsed.mode === "durable");
      setReviewVersion((value) => value + 1);
      setRequiresRefresh(false);
      if (command) {
        setRetry(null);
        setNotice(command.notice);
        if (command.body.channel === "sms") setDraft("");
        if (command.body.eventKind === "sms_reply") setReply("");
      }
    } catch (failure) {
      if (sequence !== request.current.sequence || (failure.name === "AbortError" && !timedOut)) return;
      setError(command ? communicationError(failure, durable ? "durable" : "simulated") : "Communications could not be loaded. Refresh to try again.");
      if (command) {
        // Keep the exact body and action ID after an ambiguous response. Never
        // replace an uncertain action with a second operation on Retry.
        const ambiguous = timedOut || !failure.status || failure.status >= 500 || failure.status === 429;
        setRetry(ambiguous ? command : null);
        setRequiresRefresh(!ambiguous && [403, 404, 409].includes(failure.status));
      }
    } finally {
      window.clearTimeout(deadline);
      if (sequence === request.current.sequence) {
        request.current.busy = false;
        setBusy(false);
        setMutationPending(false);
      }
    }
  }

  useEffect(() => { if (active && !retry && !reviewLocked && !blocked) run(); }, [active, chaseId]);

  useEffect(() => {
    if (!active || !view || blocked || reviewLocked || retry || requiresRefresh) return;
    let stopped = false;
    async function refreshConversation() {
      if (stopped || document.hidden || request.current.busy || background.current) return;
      const controller = new AbortController();
      background.current = controller;
      const sequence = request.current.sequence;
      let timedOut = false;
      const deadline = window.setTimeout(() => { timedOut = true; controller.abort(); }, 10_000);
      try {
        const result = await api(BASE, { method: "POST", body: { chaseId }, signal: controller.signal });
        if (stopped || sequence !== request.current.sequence || background.current !== controller) return;
        if (timedOut) throw new Error("Conversation update timed out");
        const next = readCommunications(result, chaseId);
        setView(next); setSyncError("");
        // Updating conversation history must not reset an unfinished staff review.
        if (next.reviewPendingCount !== null) reviewCountsCallback.current?.(chaseId,
          { pending: next.reviewPendingCount, held: next.heldInboundCount });
      } catch (failure) {
        if (stopped || sequence !== request.current.sequence || background.current !== controller
            || (failure.name === "AbortError" && !timedOut)) return;
        setSyncError("Live updates were interrupted. Refresh communications before continuing.");
        setRequiresRefresh(true);
      } finally {
        window.clearTimeout(deadline);
        if (background.current === controller) background.current = null;
      }
    }
    const interval = window.setInterval(refreshConversation, 15_000);
    document.addEventListener("visibilitychange", refreshConversation);
    return () => {
      stopped = true;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshConversation);
      background.current?.abort();
      background.current = null;
    };
  }, [active, !!view, blocked, reviewLocked, retry, requiresRefresh, chaseId]);

  const reviewBlocked = busy || blocked || !!retry || requiresRefresh || !manageChase;
  const locked = reviewBlocked || reviewLocked;
  function act(path, body, noticeText) {
    if (locked || !view) return;
    if (body.channel && !view.enabledChannels.includes(body.channel)) return;
    if (path === "/authorize" && !view.enabledChannels.includes(view.operations.find((item) => item.operationId === body.operationId)?.channel)) return;
    const context = path === "/policy/decision" ? {} : { chaseId };
    const identity = ["/bind", "/authorize", "/cancel", "/resolve-unknown"].includes(path) ? {} : { actionId: crypto.randomUUID() };
    run({ path, body: { ...context, ...identity, ...body }, notice: noticeText });
  }
  function demoEvent(kind, operationId, text) {
    act("/demo-event", { eventKind: kind, ...(operationId ? { operationId } : {}),
      ...(kind === "sms_reply" ? { text } : {}) }, "Demo event recorded. No external contact occurred.");
  }
  const sms = view?.operations.filter((item) => item.channel === "sms") || [];
  const smsEnabled = view?.enabledChannels.includes("sms");
  const voiceEnabled = view?.enabledChannels.includes("automated_voice");
  const calls = view?.operations.filter((item) => item.channel === "automated_voice") || [];
  const responses = view?.events.filter((item) => item.channel === "automated_voice" && communicationsDisposition(item.disposition)) || [];
  const reviewCount = view ? view.reviewPendingCount ?? (view.events.filter((item) => item.needsStaffReview).length
    + view.operations.filter((item) => item.needsStaffReview).length) : 0;

  return <section aria-label="Case communications" aria-busy={busy} className="space-y-4 py-4">
    <div className="flex items-start justify-between gap-3">
      <p className="text-xs leading-relaxed text-slate-500">{durable ? "Drafts require explicit authorization. Delivery outcomes are reported separately by the provider." : "Local simulation · No external contact occurs."}</p>
      <button type="button" className="inline-flex shrink-0 items-center gap-1 text-xs text-slate-600 disabled:opacity-50" disabled={busy || blocked || !!retry || reviewLocked} onClick={() => run()}><RefreshCw size={13} className={busy ? "animate-spin" : ""} />Refresh communications</button>
    </div>
    {active && view && !syncError && <p className="text-[11px] text-slate-500">Replies and delivery updates appear automatically while this conversation is open.</p>}
    {syncError && <p role="alert" className="rounded-lg bg-amber-50 p-3 text-xs text-amber-900">{syncError}</p>}
    {error && <div role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-800"><p>{error}</p>
      {retry && <button type="button" className={`${button} mt-2`} disabled={busy || blocked} onClick={() => run(retry)}>{durable ? "Retry same action" : "Retry same simulation"}</button>}
    </div>}
    {notice && <p role="status" className="text-xs text-sky-800">{notice}</p>}
    {reviewAvailable && <CommunicationsEventReview key={chaseId} chaseId={chaseId} active={active} blocked={reviewBlocked}
      canReview={manageChase} refreshVersion={reviewVersion} onReviewed={() => run()} onCounts={onReviewCounts}
      onLockChange={setReviewLocked} />}
    {!view ? <p className="py-4 text-sm text-slate-500">{busy ? "Loading communications…" : "Communication history unavailable."}</p> : <>
      {durable && <OutreachSetup key={`${chaseId}:${view.policy.hash}:${view.policy.approved}:${view.enabledChannels.join(",")}`} view={view} locked={locked} act={act} />}
      {view.truncated && <p className="text-xs text-slate-500">Showing the most recent 200 history entries. Current delivery states include earlier receipts.</p>}
      {(reviewCount > 0 || view.heldInboundCount > 0) && <div className="rounded-lg bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-900">
        {reviewCount > 0 && <p>{reviewCount} communication item{reviewCount === 1 ? " needs" : "s need"} staff review. Replies do not verify receipt of a card or close this case.</p>}
        {view.heldInboundCount > 0 && <p>{view.heldInboundCount} unmatched inbound event{view.heldInboundCount === 1 ? " is" : "s are"} held for staff review; no message is attributed here.</p>}
      </div>}
      <section aria-label="SMS conversation" className="rounded-xl border border-slate-200">
        <div className="flex items-center justify-between gap-2 border-b border-slate-100 px-4 py-3"><h3 className="flex items-center gap-2 text-sm font-semibold text-slate-900"><MessageSquare size={16} />Text conversation</h3>{view.suppression.sms && <span className="text-xs font-medium text-rose-700">SMS suppressed · STOP</span>}</div>
        <ol aria-label="SMS thread" className="max-h-80 space-y-3 overflow-auto p-4">
          {smsThread(view).map((item) => <li key={item.id} className={`flex ${item.direction === "outbound" ? "justify-end" : "justify-start"}`}>
            <div className={`max-w-[90%] rounded-xl px-3 py-2 ${item.direction === "outbound" ? "bg-sky-50" : "bg-slate-100"}`}>
              <p className="text-[11px] font-semibold text-slate-500">{item.direction === "outbound" ? (durable ? "Staff draft / SMS" : "Staff · simulated SMS") : (durable ? "Inbound reply" : "Simulated inbound reply")}</p>
              <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-800">{item.text || "Attachment received; text content unavailable here."}</p>
              {item.direction === "inbound" && item.mediaCount > 0 && <p className="mt-1 text-xs text-amber-800" aria-label="Reported attachments">{item.mediaCount} attachment{item.mediaCount === 1 ? "" : "s"} reported; attachment content is not loaded.</p>}
              <p className="mt-2 text-[11px] text-slate-500"><time dateTime={item.at}>{chaseAttemptTime(item.at)}</time> · {item.unknownResolution ? "Original outcome unknown · investigated" : communicationsStatus(item.status)}</p>
              {item.needsStaffReview && <p className="mt-1 text-xs font-medium text-amber-800">Needs staff review</p>}
              {item.blockedReason && <p className="mt-2 text-xs text-amber-800">{communicationsReason(item.blockedReason)} · Checked {chaseAttemptTime(item.lastCheckedAt)}</p>}
              <DeliveryError item={item} />
              {durable && smsEnabled && item.status === "prepared" && <AuthorizeDraft operationId={item.operationId} channel="sms" locked={locked} act={act} availability={view.actions.sms.authorization} />}
              {durable && <CancelDraft item={item} channel="sms" locked={locked} act={act} />}
              {durable && <UnknownOutcome item={item} channel="sms" locked={locked} act={act} />}
            </div>
          </li>)}
          {!smsThread(view).length && <li className="py-3 text-center text-sm text-slate-500">{durable ? "No recorded messages yet." : "No simulated messages yet."}</li>}
        </ol>
        {smsEnabled ? <form className="border-t border-slate-100 p-4" onSubmit={(event) => { event.preventDefault(); if (view.actions.sms.allowed && draft.trim()) act("/action", { channel: "sms", text: draft }, durable ? "SMS draft prepared. Review and authorize it separately." : "SMS simulated. No text was sent."); }}>
          {durable && <p className="mb-2 text-xs text-slate-500">Saving a draft does not authorize sending. You must review and authorize it when outreach is available.</p>}
          <div className="mb-2 flex flex-wrap gap-2">{view.templates.map((template) => <button key={template.id} type="button" disabled={locked || !view.actions.sms.allowed} className="rounded-md bg-slate-100 px-2 py-1 text-xs text-slate-600 hover:bg-slate-200 disabled:opacity-50" onClick={() => setDraft(template.text)}>{template.label}</button>)}</div>
          <label className="block text-xs font-medium text-slate-700">SMS draft<textarea rows={3} className={field} maxLength={SMS_MAX_LENGTH} value={draft} onChange={(event) => setDraft(event.target.value)} disabled={locked || !view.actions.sms.allowed} placeholder={durable ? "Use approved reminder language; no medical or insurance details." : "Use synthetic text only."} /></label>
          <div className="mt-2 flex items-center justify-between gap-3"><span className="text-xs text-slate-400">{draft.length}/{SMS_MAX_LENGTH}</span><button type="submit" className={primary} disabled={locked || !view.actions.sms.allowed || !draft.trim()}>{durable ? "Prepare SMS" : "Simulate SMS"}</button></div>
          {view.actions.sms.reason && <p className="mt-2 text-xs text-amber-800">{durable ? communicationsReason(view.actions.sms.reason) : view.actions.sms.reason}</p>}
        </form> : <p className="border-t border-slate-100 p-4 text-xs text-slate-500">Texting is unavailable for this workspace. Previous messages remain available for review.</p>}
      </section>
      {(voiceEnabled || calls.length > 0 || responses.length > 0) && <section aria-label={voiceEnabled ? "Scripted call automation" : "Call history"} className="rounded-xl border border-slate-200 p-4">
        <div className="flex items-center justify-between gap-2"><h3 className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Phone size={16} />{voiceEnabled ? "Scripted reminder call" : "Previous calls"}</h3>{view.suppression.automatedVoice && <span className="text-xs font-medium text-rose-700">Calls suppressed</span>}</div>
        <p className="mt-1 text-xs leading-relaxed text-slate-500">Keypad responses request staff follow-up. Manual phone attempts remain in Activity.</p>
        {voiceEnabled ? <><details className="mt-3 text-xs text-slate-600"><summary className="cursor-pointer font-medium">Preview call script</summary><p className="mt-2 whitespace-pre-wrap leading-relaxed">{view.callScript}</p></details>
        <button type="button" className={`${button} mt-3`} disabled={locked || !view.actions.automatedVoice.allowed} onClick={() => act("/action", { channel: "automated_voice" }, durable ? "Call draft prepared. Review and authorize the script separately." : "Scripted call queued in the simulation. No call was placed.")}>{durable ? "Prepare scripted call" : "Queue simulated call"}</button>
        {view.actions.automatedVoice.reason && <p className="mt-2 text-xs text-amber-800">{durable ? communicationsReason(view.actions.automatedVoice.reason) : view.actions.automatedVoice.reason}</p>}</> : <p className="mt-3 text-xs text-slate-500">Automated calling is unavailable for this workspace. Previous calls remain available for review.</p>}
        {calls.length > 0 && <ul aria-label={durable ? "Scripted calls" : "Simulated calls"} className="mt-3 space-y-3 border-t border-slate-100 pt-3">{calls.map((item, index) => <li key={item.operationId} className="text-xs text-slate-600"><div className="flex flex-wrap justify-between gap-1"><span>Call #{index + 1} · <time dateTime={item.createdAt}>{chaseAttemptTime(item.createdAt)}</time></span><span className="font-medium">{item.unknownResolution ? "Original outcome unknown · investigated" : communicationsStatus(item.status)}</span></div>{item.needsStaffReview && <span className="text-amber-800">Needs staff review</span>}<DeliveryError item={item} />{item.blockedReason && <p className="mt-1 text-amber-800">{communicationsReason(item.blockedReason)} · Checked {chaseAttemptTime(item.lastCheckedAt)}</p>}{durable && item.status === "prepared" && <><p className="mt-2 whitespace-pre-wrap">{item.text}</p>{voiceEnabled && <AuthorizeDraft operationId={item.operationId} channel="automated_voice" locked={locked} act={act} />}</>}{durable && item.status !== "prepared" && <details className="mt-2"><summary className="cursor-pointer">Script for this call</summary><p className="mt-1 whitespace-pre-wrap">{item.text}</p></details>}{durable && <CancelDraft item={item} channel="automated_voice" locked={locked} act={act} />}{durable && <UnknownOutcome item={item} channel="automated_voice" locked={locked} act={act} />}</li>)}</ul>}
        {responses.length > 0 && <details open className="mt-3 text-xs text-slate-600"><summary className="cursor-pointer font-medium">Keypad responses ({responses.length})</summary><ul className="mt-2 space-y-2">{responses.map((item) => <li key={item.eventId}><span className="font-medium text-amber-800">{communicationsDisposition(item.disposition)}</span><span className="ml-1 text-slate-500">· {chaseAttemptTime(item.occurredAt)}</span></li>)}</ul></details>}
      </section>}
      {manageChase && !durable && <details className="border-t border-slate-200 pt-3 text-xs text-slate-600"><summary className="cursor-pointer font-medium">Demo events</summary>
        <p className="mt-2 text-xs leading-relaxed text-slate-500">Create a local inbound reply or {voiceEnabled ? "delivery/keypad" : "delivery"} event for this case. These events never contact anyone or verify case completion.</p>
        <fieldset disabled={locked} className="mt-3 space-y-4 disabled:opacity-60">
          <div><label className="block text-xs font-medium text-slate-700">Simulated reply<textarea rows={2} className={field} maxLength={SMS_MAX_LENGTH} value={reply} onChange={(event) => setReply(event.target.value)} /></label>
            <div className="mt-2 flex flex-wrap gap-2"><button type="button" className={button} disabled={!reply.trim()} onClick={() => demoEvent("sms_reply", null, reply)}>Simulate reply</button>{["STOP", "START", "HELP"].map((kind) => <button key={kind} type="button" className={button} onClick={() => demoEvent(`sms_${kind.toLowerCase()}`)}>Simulate {kind}</button>)}</div>
            <p className="mt-2 text-[11px] text-slate-500">START is recorded for review; it does not restore permission to text.</p>
          </div>
          <div className="space-y-2 border-t border-slate-100 pt-3"><OperationPicker label="SMS simulation" operations={sms} value={smsOperation} onChange={setSmsOperation} disabled={locked} /><label className="block">SMS delivery event<select aria-label="SMS delivery event" className={`${field} min-w-0 max-w-full`} value={smsEvent} onChange={(event) => setSmsEvent(event.target.value)}>{SMS_DEMO_EVENTS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button type="button" className={button} disabled={!sms.some((item) => item.operationId === smsOperation)} onClick={() => demoEvent(smsEvent, smsOperation)}>Simulate SMS event</button></div>
          {voiceEnabled && <div className="space-y-2 border-t border-slate-100 pt-3"><OperationPicker label="Call simulation" operations={calls} value={voiceOperation} onChange={setVoiceOperation} disabled={locked} /><label className="block">Call event<select aria-label="Call event" className={`${field} min-w-0 max-w-full`} value={voiceEvent} onChange={(event) => setVoiceEvent(event.target.value)}>{VOICE_DEMO_EVENTS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button type="button" className={button} disabled={!calls.some((item) => item.operationId === voiceOperation)} onClick={() => demoEvent(voiceEvent, voiceOperation)}>Simulate call event</button></div>}
        </fieldset>
      </details>}
    </>}
  </section>;
}
