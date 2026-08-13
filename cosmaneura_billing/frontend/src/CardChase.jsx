import React, { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle, Check, ClipboardCopy, CreditCard, Link2, Mail, Phone,
  RefreshCw, ShieldCheck, Upload, UserX, X,
} from "lucide-react";
import { api } from "./api.js";

/*
  F9 — the Card Chase worklist (Grace's "mantra", 8/6): the system collates
  every patient whose insurance data is missing, expired, or inactive; staff
  work the list — call, email, take card info over the phone — and rows close
  themselves the moment a verified card writes to (mock) athenaNet.

  Synthetic data only: 555 numbers, example.test addresses, a mock Athena.
  Direct-contract patients arrive parked in a human-review bucket and expose
  no outreach affordances at all (the F3 hard gate, rendered).
*/

const STAFF = "biller-demo";

const TRIGGER_LABEL = {
  no_card_on_file: "no card on file",
  expired: "coverage expired",
  eligibility_inactive: "eligibility inactive",
  eligibility_timeout: "eligibility timeout",
  manual: "staff flagged",
};
const TRIGGER_TONE = {
  no_card_on_file: "bg-rose-50 text-rose-600 ring-rose-200",
  expired: "bg-amber-50 text-amber-700 ring-amber-200",
  eligibility_inactive: "bg-amber-50 text-amber-700 ring-amber-200",
  eligibility_timeout: "bg-slate-100 text-slate-500 ring-slate-300",
  manual: "bg-sky-50 text-sky-700 ring-sky-200",
};
const PRIORITY_TONE = {
  P0: "bg-rose-600 text-white ring-rose-600",
  P1: "bg-amber-500 text-white ring-amber-500",
  P2: "bg-slate-200 text-slate-600 ring-slate-300",
};
const STATE_LABEL = {
  open: "to work", staff_approved: "approved", sent: "emailed",
  reminded: "reminded", submitted: "card submitted",
  human_review: "human review", escalated_to_staff: "escalated",
  unreachable: "unreachable", verified_closed: "closed ✓",
};

function Pill({ tone, children, title }) {
  return (
    <span title={title}
          className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${tone}`}>
      {children}
    </span>
  );
}

function Stat({ label, value }) {
  return (
    <div className="rounded-xl bg-white px-3 py-2 ring-1 ring-slate-200">
      <div className="text-lg font-semibold leading-tight text-slate-800">{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-slate-400">{label}</div>
    </div>
  );
}

function ManualEntryForm({ item, onDone, onError }) {
  const [payer, setPayer] = useState("");
  const [last4, setLast4] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      await api(`/api/chase/${item.chase_id}/manual-entry`, {
        method: "POST",
        body: { staff_id: STAFF, payer_name: payer, member_id_last4: last4 },
      });
      onDone();
    } catch (e) { onError(String(e)); } finally { setBusy(false); }
  };
  return (
    <div className="mt-2 flex flex-wrap items-end gap-2 rounded-lg bg-slate-50 p-2 ring-1 ring-slate-200">
      <div>
        <label className="block text-[10px] font-semibold uppercase tracking-wider text-slate-400">Payer</label>
        <input value={payer} onChange={(e) => setPayer(e.target.value)}
               placeholder="Synthetic Health Plan"
               className="w-44 rounded-md border border-slate-200 px-2 py-1 text-xs" />
      </div>
      <div>
        <label className="block text-[10px] font-semibold uppercase tracking-wider text-slate-400">
          Member ID — last 4 only
        </label>
        <input value={last4} maxLength={4}
               onChange={(e) => setLast4(e.target.value.replace(/\D/g, ""))}
               placeholder="4821"
               className="w-20 rounded-md border border-slate-200 px-2 py-1 text-xs" />
      </div>
      <button onClick={submit} disabled={busy || payer.length < 2 || last4.length < 2}
              className="rounded-lg bg-slate-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40">
        {busy ? "Writing…" : "Verify & write to Athena"}
      </button>
      <p className="basis-full text-[10px] text-slate-400">
        You are the verifier — the entry writes a masked package to mock
        athenaNet and closes this item. Full member numbers are never stored.
      </p>
    </div>
  );
}

function Row({ item, refresh }) {
  const [entryOpen, setEntryOpen] = useState(false);
  const [sentBody, setSentBody] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const closed = ["verified_closed", "escalated_to_staff", "unreachable"]
    .includes(item.sequence_state);
  const parked = item.sequence_state === "human_review";

  const act = useCallback(async (path, extra = {}) => {
    setBusy(true); setError(null);
    try {
      const out = await api(`/api/chase/${item.chase_id}/${path}`, {
        method: "POST", body: { staff_id: STAFF, ...extra },
      });
      if (out.message_body) setSentBody(out.message_body);
      await refresh();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }, [item.chase_id, refresh]);

  const emailOutreach = async () => {
    if (item.sequence_state === "open") await act("approve");
    await act("send");
  };

  return (
    <li className={`rounded-xl border px-3 py-2.5 ${closed ? "border-slate-100 bg-slate-50/60"
      : parked ? "border-violet-200 bg-violet-50/40" : "border-slate-200 bg-white"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={PRIORITY_TONE[item.priority]}>{item.priority}</Pill>
        <span className={`text-sm font-medium ${closed ? "text-slate-400" : "text-slate-800"}`}>
          {item.display_name}
        </span>
        <Pill tone={TRIGGER_TONE[item.trigger] || TRIGGER_TONE.manual}>
          {TRIGGER_LABEL[item.trigger] || item.trigger}
        </Pill>
        {item.appointment_at && (
          <span className="text-xs text-slate-400">appt {item.appointment_at.slice(0, 10)}</span>
        )}
        <span className="ml-auto text-xs font-medium text-slate-500">
          {STATE_LABEL[item.sequence_state] || item.sequence_state}
        </span>
      </div>

      {parked && (
        <p className="mt-1 flex items-center gap-1 text-xs text-violet-700">
          <ShieldCheck size={12} />
          Direct-contract gate: never auto-chased — a person decides what happens here.
        </p>
      )}

      {!parked && !closed && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <a href={`tel:${item.phone.replace(/\D/g, "")}`}
             className="inline-flex items-center gap-1 rounded-lg bg-white px-2 py-1 text-xs font-medium text-slate-600 ring-1 ring-inset ring-slate-200 hover:bg-slate-50">
            <Phone size={12} />{item.phone}
          </a>
          <button onClick={emailOutreach} disabled={busy}
                  className="inline-flex items-center gap-1 rounded-lg bg-white px-2 py-1 text-xs font-medium text-slate-600 ring-1 ring-inset ring-slate-200 hover:bg-slate-50 disabled:opacity-40">
            <Mail size={12} />
            {item.attempts.length ? "Send reminder" : "Approve & email card request"}
          </button>
          <button onClick={() => setEntryOpen(!entryOpen)} disabled={busy}
                  className="inline-flex items-center gap-1 rounded-lg bg-white px-2 py-1 text-xs font-medium text-slate-600 ring-1 ring-inset ring-slate-200 hover:bg-slate-50 disabled:opacity-40">
            <CreditCard size={12} />Record card info (call)
          </button>
          <button onClick={() => act("simulate-submission")} disabled={busy}
                  title="Demo only: pretend the patient used the upload link"
                  className="inline-flex items-center gap-1 rounded-lg bg-white px-2 py-1 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-200 hover:bg-emerald-50 disabled:opacity-40">
            <Upload size={12} />Simulate patient upload
          </button>
          <button onClick={() => act("unreachable",
                    { reason: "phone and email unreachable (staff)" })}
                  disabled={busy}
                  className="inline-flex items-center gap-1 rounded-lg bg-white px-2 py-1 text-xs font-medium text-slate-400 ring-1 ring-inset ring-slate-200 hover:bg-slate-50 disabled:opacity-40">
            <UserX size={12} />Unreachable
          </button>
        </div>
      )}

      {entryOpen && !closed && (
        <ManualEntryForm item={item} onError={setError}
                         onDone={async () => { setEntryOpen(false); await refresh(); }} />
      )}

      {sentBody && (
        <p className="mt-2 rounded-lg bg-slate-50 p-2 text-[11px] text-slate-500 ring-1 ring-slate-100">
          <Mail size={11} className="mr-1 inline" />sent: “{sentBody}”
        </p>
      )}

      {item.attempts.length > 0 && (
        <p className="mt-1 text-[11px] text-slate-400">
          {item.attempts.map((a, i) => (
            <span key={i} className="mr-2">
              {a.channel} · {a.sent_at.slice(0, 16).replace("T", " ")}
              {a.staff_approved === false ? " · auto" : ` · ${a.sent_by || a.approved_by}`}
            </span>
          ))}
        </p>
      )}

      {closed && item.resolution && (
        <p className="mt-1 text-xs text-slate-400">
          {item.sequence_state === "verified_closed" && item.insurance
            ? <>card verified → {item.insurance.payer_name} {item.insurance.member_id_masked} · package {item.insurance.package_id} written → eligibility ACTIVE</>
            : item.resolution.reason}
        </p>
      )}

      {error && (
        <p className="mt-1 flex items-center gap-1 text-xs text-rose-600">
          <AlertTriangle size={12} />{error}
        </p>
      )}
    </li>
  );
}

export default function CardChase({ live }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      setData(await api("/api/chase/queue"));
      setError(null);
    } catch (e) { setError(String(e)); }
  }, []);

  useEffect(() => {
    if (!live) return;
    let stale = false;
    api("/api/chase/queue")
      .then((d) => { if (!stale) setData(d); })
      .catch((e) => { if (!stale) setError(String(e)); });
    return () => { stale = true; };
  }, [live]);

  if (!live) {
    return (
      <div className="rounded-2xl bg-white p-6 text-sm text-slate-500 shadow-sm ring-1 ring-slate-200">
        The card-chase worklist runs on the live engine — start it
        (<code className="rounded bg-slate-100 px-1">uvicorn cosmaneura_billing.api:app --port 8000</code>) and reload.
      </div>
    );
  }
  if (error) return <div className="rounded-2xl bg-rose-50 p-4 text-sm text-rose-700 ring-1 ring-rose-200">{error}</div>;
  if (!data) return <div className="p-6 text-sm text-slate-400">Collating the worklist…</div>;

  const m = data.metrics;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Stat label="cards outstanding" value={m.cards_outstanding} />
        <Stat label="human review" value={m.in_human_review} />
        <Stat label="closed" value={m.completed} />
        <Stat label="completion (chased)" value={`${Math.round(m.completion_rate * 100)}%`} />
        <Stat label="staff touches" value={m.staff_touches} />
        <Stat label="machine touches" value={m.machine_touches} />
        <button onClick={refresh}
                className="ml-auto inline-flex items-center gap-1 rounded-lg bg-white px-2.5 py-1.5 text-xs font-medium text-slate-600 ring-1 ring-inset ring-slate-200 hover:bg-slate-50">
          <RefreshCw size={12} />Re-detect
        </button>
      </div>

      <div className="rounded-2xl bg-white p-4 shadow-sm ring-1 ring-slate-200">
        <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          Patients needing insurance data — collated automatically, worked here
        </p>
        <ul className="space-y-2">
          {data.items.map((item) => (
            <Row key={item.chase_id} item={item} refresh={refresh} />
          ))}
        </ul>
      </div>

      <p className="text-center text-xs text-slate-400">
        Synthetic patients only (555 numbers, example.test). Verified cards
        write once to mock athenaNet — replays report duplicate, rows close
        themselves. Baseline metrics arrive with the month-end close.
      </p>
    </div>
  );
}
