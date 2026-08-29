import React, { useMemo, useState } from "react";
import { ClipboardCheck, LockKeyhole, Phone, ShieldAlert, UserRoundSearch } from "lucide-react";
import {
  hasSafeChaseEnvelope, outcomeCompletesTask, recordPhoneAttempt, safeChaseTasks,
} from "./operationalDemoContract.js";

const OUTCOMES = [["reached", "Reached person"], ["left_message", "Left message"], ["no_answer", "No answer"], ["wrong_number", "Wrong number"], ["declined", "Declined"]];
const ACTOR = "SYN-STAFF-DEMO-001";

export default function McdAdminChase({ worklist }) {
  const admitted = useMemo(() => safeChaseTasks(worklist), [worklist]);
  const [selectedId, setSelectedId] = useState(admitted[0]?.task_id || null);
  const [outcome, setOutcome] = useState("no_answer");
  const [minutes, setMinutes] = useState("2");
  const [localActions, setLocalActions] = useState([]);
  const [notice, setNotice] = useState("");
  const allActions = (task) => [
    ...(task.actions || []), ...localActions.filter((action) => action.task_id === task.task_id),
  ];
  const isComplete = (task) => task.status === "completed"
    || allActions(task).some((action) => outcomeCompletesTask(action.outcome));
  const selected = admitted.find((task) => task.task_id === selectedId);
  const rejected = (worklist?.tasks?.length || 0) - admitted.length;

  const submit = (event) => {
    event.preventDefault();
    try {
      const action = recordPhoneAttempt(selected, { outcome, minutes_spent: Number(minutes) }, {
        action_id: `local-action-${localActions.length + 1}`, actor_ref: ACTOR,
        occurred_at: new Date().toISOString(),
      });
      setLocalActions((current) => [action, ...current]);
      setNotice("Manual phone attempt recorded. This page did not place a call.");
    } catch { setNotice("Attempt not recorded. This page did not place a call."); }
  };

  return (
    <section aria-labelledby="mcd-chase-heading" className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="mcd-chase-heading" className="flex items-center gap-2 text-lg font-semibold"><UserRoundSearch size={20} />MCD insurance-card outreach</h2>
          <p className="mt-1 text-sm text-slate-600">Synthetic roster of people staff need to reach for missing or unusable insurance-card information.</p>
        </div>
        <span className="rounded-md bg-rose-50 px-2 py-0.5 text-xs font-medium text-rose-800 ring-1 ring-rose-200">default off · not production</span>
      </div>
      <dl className="mt-4 grid gap-3 sm:grid-cols-3">
        {[['Transport', 'record_only'], ['Scheduler', 'Off'], ['Outbound channels', 'None enabled']].map(([term, value]) => <div key={term} className="rounded-xl bg-slate-50 p-3 ring-1 ring-slate-200"><dt className="text-xs uppercase text-slate-500">{term}</dt><dd className="mt-1 text-sm font-semibold">{value}</dd></div>)}
      </dl>
      <p className="mt-4 flex gap-2 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950"><ShieldAlert size={18} className="shrink-0" />Staff can record the outcome of a manual phone call made outside this page. This page cannot call, send SMS, open a patient portal, or contact anyone automatically.</p>
      {!hasSafeChaseEnvelope(worklist) && <p role="alert" className="mt-3 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">The worklist safety envelope is invalid, so no tasks are available.</p>}
      {rejected > 0 && <p role="alert" className="mt-3 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{rejected} task{rejected === 1 ? " was" : "s were"} withheld for missing synthetic, consent, direct-contract, or record-only proof.</p>}

      <div className="mt-5 grid gap-5 lg:grid-cols-2">
        <div>
          <h3 className="flex items-center gap-2 text-sm font-semibold"><ClipboardCheck size={16} />People to reach</h3>
          <div className="mt-2 space-y-2">
            {admitted.map((task) => {
              const actions = allActions(task); const complete = isComplete(task);
              return <button type="button" key={task.task_id} onClick={() => { setSelectedId(task.task_id); setNotice(""); }} aria-pressed={task.task_id === selectedId} className={`w-full rounded-xl border p-3 text-left ${task.task_id === selectedId ? "border-sky-300 bg-sky-50" : "border-slate-200 hover:bg-slate-50"}`}>
                <span className="flex justify-between gap-2"><strong>{task.person.display_label}</strong><span className="text-xs text-slate-600">{task.person.outreach_reason.replaceAll("_", " ")}</span></span>
                <span className="mt-1 block text-xs text-slate-500">{task.task_id} · {complete ? "completed" : "open"} · {actions.length} recorded attempt{actions.length === 1 ? "" : "s"}</span>
              </button>;
            })}
            {!admitted.length && <p className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-600">No safe synthetic task is available.</p>}
          </div>
        </div>

        <div>
          <h3 className="flex items-center gap-2 text-sm font-semibold"><Phone size={16} />Staff phone-task record</h3>
          {selected && (isComplete(selected) ? (
            <p className="mt-2 rounded-xl bg-emerald-50 p-4 text-sm text-emerald-900 ring-1 ring-emerald-200">This task is complete. No additional phone attempt can be recorded.</p>
          ) : (
            <form onSubmit={submit} className="mt-2 rounded-xl border border-slate-200 bg-slate-50 p-4">
              <p className="font-medium">{selected.person.display_label}</p>
              <p className="mt-1 text-xs text-slate-600">Manual insurance-card follow-up only. Voice consent is recorded and the authoritative direct-contract gate is clear.</p>
              <div className="mt-3 rounded-lg bg-sky-50 p-3 ring-1 ring-sky-200"><p className="text-xs uppercase text-sky-800">Synthetic callback reference</p><p className="font-mono text-sm">{selected.person.phone}</p><p className="text-xs text-sky-800">Display only; not a dial link.</p></div>
              <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_7rem]">
                <label className="text-xs font-medium">Manual phone outcome<select value={outcome} onChange={(event) => setOutcome(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm">{OUTCOMES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                <label className="text-xs font-medium">Staff minutes<input type="number" min="0" max="480" step="0.5" required value={minutes} onChange={(event) => setMinutes(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" /></label>
              </div>
              <button type="submit" className="mt-3 inline-flex min-h-10 items-center gap-2 rounded-lg bg-slate-950 px-3 py-2 text-sm font-semibold text-white"><ClipboardCheck size={15} />Record completed manual attempt</button>
              <p className="mt-2 min-h-5 text-xs font-medium text-sky-800" role="status">{notice}</p>
            </form>
          ))}
        </div>
      </div>

      <details className="mt-5 rounded-xl border border-slate-200 bg-slate-50">
        <summary className="flex cursor-pointer items-center gap-2 px-4 py-3 text-sm font-medium"><LockKeyhole size={15} />Record-only activity ({admitted.flatMap(allActions).length})</summary>
        <ol className="space-y-2 border-t border-slate-200 p-3">{admitted.flatMap(allActions).map((action) => <li key={action.action_id} className="rounded-lg bg-white p-3 text-xs ring-1 ring-slate-200"><code>{action.task_id}</code> · {action.outcome.replaceAll("_", " ")} · {action.minutes_spent} min<span className="block text-slate-500">manual outreach recorded: yes · automated outbound initiated: no</span></li>)}</ol>
      </details>
    </section>
  );
}
