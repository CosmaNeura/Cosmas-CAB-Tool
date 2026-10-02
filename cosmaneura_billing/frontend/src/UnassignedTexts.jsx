import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { eventReviewError } from "./communicationsEventReview.js";
import { readUnassignedCount, readUnassignedTexts, readUnassignedCases, unassignedCommand, readUnassignedReceipt } from "./unassignedTexts.js";
import {pollVisibleCounts} from "./communicationCounts.js";

const base = "/api/chase/communications";
const button = "rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50";

export default function UnassignedTexts({ blocked = false, refreshVersion = 0, onLock, onReviewed }) {
  const [count, setCount] = useState(null), [hidden, setHidden] = useState(false), [open, setOpen] = useState(false);
  const [state, setState] = useState("pending"), [view, setView] = useState(null), [item, setItem] = useState(null);
  const [choices, setChoices] = useState(null), [selected, setSelected] = useState(""), [decision, setDecision] = useState("");
  const [confirmed, setConfirmed] = useState(false), [loading, setLoading] = useState(false), [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(null), [stale, setStale] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const pending = useRef(new Map()), sequence = useRef(0), countEpoch = useRef(0), mounted = useRef(true);
  const [countStale, setCountStale] = useState(false);
  const countState = useRef({});
  const locked = busy || retry !== null;
  countState.current = {blocked: blocked || locked || loading || hidden};
  useEffect(() => { onLock?.(locked); return () => onLock?.(false); }, [locked, onLock]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; pending.current.forEach((c) => c.abort()); }; }, []);

  async function request(path, body, key) {
    pending.current.get(key)?.abort();
    const controller = new AbortController(); pending.current.set(key, controller);
    const timer = window.setTimeout(() => controller.abort(), 15_000);
    try { return await api(base + path, { method: "POST", body, signal: controller.signal }); }
    finally { window.clearTimeout(timer); if (pending.current.get(key) === controller) pending.current.delete(key); }
  }
  useEffect(() => {
    let current = true; const epoch = ++countEpoch.current;
    request("/unassigned-texts/count", {}, "count").then((v) => { if (current && epoch === countEpoch.current) { setCount(readUnassignedCount(v)); setHidden(false); setCountStale(false); } })
      .catch((e) => { if (current && epoch === countEpoch.current) { setCountStale(true); setHidden(e?.status === 404); } });
    return () => { current = false; pending.current.get("count")?.abort(); };
  }, [refreshVersion]);
  useEffect(() => {
    let epoch;
    return pollVisibleCounts({
      blocked: () => countState.current.blocked || pending.current.has("count"),
      read: async (signal) => {
        epoch = countEpoch.current;
        const observed = epoch;
        const response = await api(base + "/unassigned-texts/count", {method: "POST", body: {}, signal});
        return {epoch: observed, count: readUnassignedCount(response)};
      },
      accept: ({epoch: observed, count: next}) => {
        if (observed === countEpoch.current) { setCount(next); setCountStale(false); }
      },
      failed: () => { if (epoch === countEpoch.current) setCountStale(true); },
    });
  }, []);

  function reset() { setItem(null); setChoices(null); setSelected(""); setDecision(""); setConfirmed(false); setStale(false); }
  async function load(nextState = state, cursor = null) {
    const seq = ++sequence.current;
    // A late badge response must not replace a newer list/decision count.
    ++countEpoch.current; pending.current.get("count")?.abort();
    setLoading(true); setView(null); reset(); setError(""); setState(nextState);
    try {
      const result = readUnassignedTexts(await request("/unassigned-texts", { state: nextState, cursor, limit: 50 }, "read"), nextState);
      if (!mounted.current || seq !== sequence.current) return;
      setView(result); setCount(result.pendingCount); setCountStale(false);
    } catch (e) { if (mounted.current && seq === sequence.current) setError(e?.status === 403 ? "Your access does not allow this inbox." : "Unassigned texts could not be loaded. Refresh to try again."); }
    finally { if (mounted.current && seq === sequence.current) setLoading(false); }
  }
  async function loadCases(source, cursor = null) {
    const seq = ++sequence.current;
    setChoices(null); setSelected(""); setConfirmed(false); setLoading(true); setError("");
    try {
      const result = readUnassignedCases(await request("/unassigned-text-cases", { eventRef: source.ref, sourceHash: source.sourceHash, cursor, limit: 20 }, "read"), source);
      if (mounted.current && seq === sequence.current) setChoices(result);
    } catch (e) { if (mounted.current && seq === sequence.current) { setError("Case choices could not be verified. Refresh this inbox before reviewing."); setStale(true); } }
    finally { if (mounted.current && seq === sequence.current) setLoading(false); }
  }
  async function save(command = null) {
    if (blocked || busy) return;
    let stable = command;
    try { stable ||= unassignedCommand(item, decision, decision === "attribute" ? choices?.items.find((c) => c.chaseId === selected) : null, confirmed, crypto.randomUUID()); }
    catch { setError("Choose a decision and confirm it before saving."); return; }
    ++countEpoch.current;
    setBusy(true); setRetry(stable); setError(""); setNotice("");
    try {
      const receipt = readUnassignedReceipt(await request("/review-unassigned-text", stable, "save"), stable);
      if (!mounted.current) return;
      setCount(receipt.pendingCount); setRetry(null); reset();
      setNotice(stable.decision === "attribute" ? "Attributed to the selected case. Follow-up remains in that case’s staff review." : "Marked reviewed and left unassigned.");
      await load("pending");
      // The durable receipt is confirmed even if a separate workspace refresh fails.
      Promise.resolve(onReviewed?.(stable.chaseId)).catch(() => { if (mounted.current) setNotice("Review saved. Refresh the case workspace to see the latest information."); });
    } catch (e) {
      if (!mounted.current) return;
      const uncertain = !e?.status || e.status === 429 || e.status >= 500;
      setError(eventReviewError(e)); setRetry(uncertain ? stable : null); setStale(!uncertain);
    } finally { if (mounted.current) setBusy(false); }
  }
  if (hidden) return null;
  const disabled = blocked || locked;
  return <section aria-label="Unassigned texts" className="mb-4 rounded-xl border border-slate-200 bg-white">
    <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
      <button type="button" className="text-sm font-semibold text-slate-800 disabled:opacity-50" aria-expanded={open} aria-controls="unassigned-text-panel" disabled={disabled} onClick={() => { setOpen(!open); if (!open) load(); }}>Unassigned texts · {count ?? "Unavailable"}</button>
      <p className="text-xs text-slate-500">Replies awaiting a staff decision about the case they belong to.</p>
    </div>
    {countStale && <p role="status" className="px-4 pb-3 text-xs text-amber-800">The unassigned text count may be out of date. Refresh the inbox to try again.</p>}
    {open && <div id="unassigned-text-panel" className="border-t border-slate-200 p-4">
      <div className="flex flex-wrap items-center gap-2">
        {["pending", "reviewed"].map((value) => <button type="button" key={value} aria-pressed={state === value} disabled={disabled || loading} className={button} onClick={() => { setNotice(""); load(value); }}>{value === "pending" ? "Pending texts" : "Reviewed texts"}</button>)}
        <button type="button" className={button} disabled={disabled || loading} onClick={() => load()}>Refresh inbox</button>
      </div>
      <p className="mt-3 text-xs text-slate-500">Case choices use the current verified SMS contact. Staff must verify who sent the message. Review does not grant consent, clear STOP, or send a message.</p>
      {notice && <p role="status" className="mt-3 text-sm text-emerald-800">{notice}</p>}
      {error && <p role="alert" className="mt-3 text-sm text-red-800">{error}</p>}
      {loading && <p role="status" className="mt-3 text-sm text-slate-500">Loading review information…</p>}
      {view && <>
        <ol aria-label={state === "pending" ? "Unassigned text messages" : "Reviewed unassigned text messages"} className="mt-3 space-y-2">
          {view.items.map((v) => <li key={v.ref} className="rounded-lg border border-slate-200 p-3 text-sm">
            <div className="flex items-center justify-between gap-3"><span className="text-xs text-slate-500">{v.senderHint} · <time dateTime={v.at}>{new Date(v.at).toLocaleString()}</time></span>{state === "pending" && <button type="button" className={button} disabled={disabled || loading || stale} onClick={() => { reset(); setItem(v); setNotice(""); }}>Review text</button>}</div>
            <p className="mt-2 whitespace-pre-wrap break-words text-slate-800">{v.text || "No text body received."}</p>
            {v.mediaCount > 0 && <p className="mt-1 text-xs text-amber-800">{v.mediaCount} attachment{v.mediaCount === 1 ? "" : "s"} reported; attachment content is not loaded.</p>}
            {v.disposition === "sms_opt_out" && <p className="mt-1 text-xs font-semibold text-amber-800">STOP received · SMS suppression remains in effect</p>}
            {state === "reviewed" && <p className="mt-2 text-xs text-slate-600">{v.reviewState === "attributed" ? `Attributed by staff to ${v.attributedCaseId} · Case follow-up is separate` : "Marked reviewed · Left unassigned"} · <time dateTime={v.reviewedAt}>{new Date(v.reviewedAt).toLocaleString()}</time></p>}
          </li>)}
        </ol>
        {view.items.length === 0 && <p className="mt-3 text-sm text-slate-500">{state === "pending" ? "No unassigned texts await review." : "No reviewed texts on this page."}</p>}
        {view.hasMore && <button type="button" className={`${button} mt-3`} disabled={disabled || loading} onClick={() => load(state, view.nextCursor)}>Next texts</button>}
      </>}
      {item && <form aria-label="Review selected text" className="mt-4 rounded-lg bg-slate-50 p-4" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <h3 className="text-sm font-semibold text-slate-900">Review selected text · {item.senderHint}</h3><p className="mt-2 whitespace-pre-wrap break-words text-sm">{item.text || "No text body received."}</p>
        <label className="mt-3 block text-sm">Text review decision<select aria-label="Text review decision" className="ml-3 rounded-md border border-slate-300 p-2" value={decision} disabled={disabled || loading || stale} onChange={(e) => { setDecision(e.target.value); setSelected(""); setConfirmed(false); if (e.target.value === "attribute") loadCases(item); else setChoices(null); }}><option value="">Choose a decision</option><option value="attribute">Attribute to an existing case</option><option value="leave_unassigned">Mark reviewed; leave unassigned</option></select></label>
        {decision === "attribute" && choices && <>
          <label className="mt-3 block text-sm">Existing case<select aria-label="Existing case" className="ml-3 max-w-full rounded-md border border-slate-300 p-2" value={selected} disabled={disabled || stale} onChange={(e) => { setSelected(e.target.value); setConfirmed(false); }}><option value="">Choose a case</option>{choices.items.map((c) => <option key={c.chaseId} value={c.chaseId}>{c.patientLabel} · {c.chaseId} · {c.bucket}</option>)}</select></label>
          {choices.items.length === 0 && <p className="mt-2 text-sm text-amber-800">No eligible case on this page has a matching current SMS contact. Leave this text unassigned or refresh after verified contact information is updated.</p>}
          {choices.hasMore && <button type="button" className={`${button} mt-2`} disabled={disabled} onClick={() => loadCases(item, choices.nextCursor)}>Next matching cases</button>}
        </>}
        {decision && <label className="mt-3 flex items-start gap-2 text-sm"><input type="checkbox" checked={confirmed} disabled={disabled || loading || stale || (decision === "attribute" && !selected)} onChange={(e) => setConfirmed(e.target.checked)} />{decision === "attribute" ? "I verified that this message belongs to the selected case and its current contact." : "I reviewed this message and will leave it unassigned."}</label>}
        <button type="submit" className={`${button} mt-3`} disabled={disabled || loading || stale || !confirmed || !decision || (decision === "attribute" && !selected)}>{busy ? "Saving review…" : "Save text review"}</button>
      </form>}
      {retry && <button type="button" className={`${button} mt-3`} disabled={busy || blocked} onClick={() => save(retry)}>Retry same text review</button>}
    </div>}
  </section>;
}
