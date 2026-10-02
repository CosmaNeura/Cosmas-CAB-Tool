import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { chaseAttemptTime } from "./chaseActivity.js";
import { communicationsDisposition, communicationsStatus } from "./caseCommunications.js";
import { eventReviewCommand, eventReviewError, eventReviewLabel, readEventReview, reviewDecisions } from "./communicationsEventReview.js";

const base = "/api/chase/communications";
const button = "rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50";
const labels = { handled: "Mark handled", attribute: "Attribute to this case", leave_unassigned: "Leave unassigned" };

function Decision({ item, disabled, save }) {
  const [decision, setDecision] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  return <form className="mt-3" onSubmit={(event) => { event.preventDefault(); if (decision && confirmed && !disabled) save(item.ref, decision); }}>
    <fieldset disabled={disabled} className="space-y-2 disabled:opacity-60">
      <label className="block text-xs font-medium text-slate-700">Review decision
        <select aria-label="Review decision" className="mt-1 block w-full rounded-md border border-slate-300 bg-white p-2 text-sm" value={decision} onChange={(event) => { setDecision(event.target.value); setConfirmed(false); }}>
          <option value="">Choose a decision…</option>
          {reviewDecisions(item).map((value) => <option key={value} value={value}>{labels[value]}</option>)}
        </select>
      </label>
      {decision && <label className="flex items-start gap-2 text-xs leading-relaxed text-slate-600"><input type="checkbox" className="mt-0.5" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
        {decision === "attribute" ? "I verified that this message belongs to this case and matches its current contact." : decision === "handled" ? "I reviewed this event and completed the needed staff follow-up." : "I reviewed this message and will leave it unassigned to a case."}
      </label>}
      <button type="submit" className={button} disabled={!decision || !confirmed}>Save review</button>
    </fieldset>
  </form>;
}

export default function CommunicationsEventReview({ chaseId, active, blocked, canReview, refreshVersion, onReviewed, onCounts, onLockChange }) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState(null);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [stale, setStale] = useState(false);
  const [editVersion, setEditVersion] = useState(0);
  const request = useRef({ sequence: 0, busy: false, controller: null });
  const callbacks = useRef({ onReviewed, onCounts, onLockChange });
  callbacks.current = { onReviewed, onCounts, onLockChange };
  const context = useRef(chaseId);
  context.current = chaseId;
  const current = view?.chaseId === chaseId ? view : null;

  useEffect(() => () => {
    request.current.sequence += 1;
    request.current.controller?.abort();
    request.current.busy = false;
    callbacks.current.onLockChange?.(false);
  }, [chaseId]);
  useEffect(() => { callbacks.current.onLockChange?.(busy || !!retry); }, [busy, retry]);

  async function run(command = null) {
    if (request.current.busy || blocked || !active || (command && !canReview)) return;
    const selected = chaseId;
    const sequence = ++request.current.sequence;
    const controller = new AbortController();
    let timedOut = false;
    const deadline = window.setTimeout(() => { timedOut = true; controller.abort(); }, 15_000);
    request.current = { sequence, controller, busy: true };
    setBusy(true); setError(""); setNotice("");
    if (!command) { setView(null); setEditVersion((value) => value + 1); }
    try {
      const result = await api(base + (command ? "/review-event" : "/review-events"), {
        method: "POST", body: command || { chaseId: selected }, signal: controller.signal,
      });
      if (timedOut) throw new Error("Review request timed out.");
      if (sequence !== request.current.sequence || context.current !== selected) return;
      const parsed = readEventReview(result, selected);
      setView(parsed); setRetry(null); setStale(false); setEditVersion((value) => value + 1);
      callbacks.current.onCounts?.(selected, { pending: parsed.totalPending, held: parsed.heldPending }, !!command);
      if (command) {
        setNotice(command.decision === "attribute" ? "Message attributed to this case. Staff follow-up is still pending." : command.decision === "leave_unassigned" ? "Message left unassigned. No case was selected for it." : "Staff review recorded.");
        callbacks.current.onReviewed?.();
      }
    } catch (failure) {
      if (sequence !== request.current.sequence || context.current !== selected || (failure.name === "AbortError" && !timedOut)) return;
      setError(command ? eventReviewError(failure) : "Staff review could not be loaded. Refresh to try again.");
      if (command) {
        const ambiguous = timedOut || !failure.status || failure.status >= 500 || failure.status === 429 || failure.status === 408;
        setRetry(ambiguous ? command : null);
        setStale(!ambiguous);
      }
    } finally {
      window.clearTimeout(deadline);
      if (sequence === request.current.sequence && context.current === selected) {
        request.current.busy = false; setBusy(false);
      }
    }
  }
  useEffect(() => { if (open && active && !retry && !blocked) run(); }, [open, active, refreshVersion, blocked]);
  const locked = busy || blocked || !!retry || stale || !canReview;
  function save(ref, decision) {
    if (locked || !current) return;
    run(eventReviewCommand(current, ref, decision, true, crypto.randomUUID()));
  }

  return <details className="rounded-xl border border-slate-200 bg-slate-50/50" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-slate-800">Staff review{current ? ` · ${current.totalPending} pending` : ""}</summary>
    {open && <section aria-label="Communication event review" aria-busy={busy} className="space-y-3 border-t border-slate-200 p-4">
      <p className="text-xs leading-relaxed text-slate-500">Review replies, keypad responses, and delivery failures. A review does not verify a card, close this case, restore contact permission, or resolve an unknown send outcome.</p>
      <button type="button" className={button} disabled={busy || blocked || !!retry} onClick={() => run()}>Refresh staff review</button>
      {error && <div role="alert" className="rounded-md bg-red-50 p-3 text-xs text-red-800"><p>{error}</p>{retry && <button type="button" className={`${button} mt-2`} disabled={busy || blocked} onClick={() => run(retry)}>Retry same review</button>}</div>}
      {notice && <p role="status" className="text-xs text-sky-800">{notice}</p>}
      {!current ? <p className="text-sm text-slate-500">{busy ? "Loading staff review…" : "Review information unavailable."}</p> : <>
        <p className="text-xs text-slate-600">{current.totalPending} pending · {current.heldPending} held without case attribution</p>
        {current.truncated && <p className="text-xs text-slate-500">Showing the next 50 pending events. Counts include the full review queue for this case and eligible held messages.</p>}
        {!current.items.length && <p className="text-sm text-slate-600">No communication events await staff review.</p>}
        <ul aria-label="Events awaiting staff review" className="max-h-[36rem] space-y-3 overflow-auto">
          {current.items.map((item) => <li key={item.ref} className="rounded-lg border border-slate-200 bg-white p-3">
            <div className="flex flex-wrap justify-between gap-1 text-xs"><h4 className="font-semibold text-slate-800">{eventReviewLabel(item.kind)}</h4><time className="text-slate-500" dateTime={item.at}>{chaseAttemptTime(item.at)}</time></div>
            <p className="mt-1 text-[11px] text-slate-500">{item.channel === "sms" ? "Text message" : "Scripted call"} · {communicationsStatus(item.status)}{communicationsDisposition(item.disposition) ? ` · ${communicationsDisposition(item.disposition)}` : ""}</p>
            {item.text && <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-800">{item.text}</p>}
            {item.mediaCount > 0 && <p className="mt-2 text-xs text-slate-500">{item.mediaCount} attachment{item.mediaCount === 1 ? "" : "s"} reported. Attachments are not opened here.</p>}
            {item.held && <p className="mt-2 text-xs text-amber-800">Held · Not attributed to this case.{!item.canAttribute && " Current contact evidence does not support attribution."}</p>}
            {item.reviewState === "attributed" && <p className="mt-2 text-xs text-amber-800">Attributed by staff · Follow-up still pending</p>}
            {canReview ? <Decision key={`${item.ref}:${item.sourceHash}:${editVersion}`} item={item} disabled={locked} save={save} /> : <p className="mt-2 text-xs text-slate-500">Staff review access is required to record a decision.</p>}
          </li>)}
        </ul>
      </>}
    </section>}
  </details>;
}
