import React, { useEffect, useRef, useState } from "react";
import {
  CalendarClock, CheckCircle2, CreditCard, LockKeyhole, Phone,
  RefreshCw, ShieldAlert, UserCheck, UserRoundSearch,
} from "lucide-react";
import { api } from "./api.js";
import {
  seedConfirmationIsSafe,
  syntheticSeedConfirmationMessage,
  syntheticSeedErrorMessage,
} from "./syntheticDemoSeed.js";


const FILTERS = [
  ["", "All"],
  ["missing", "Missing card"],
  ["expired", "Expired"],
  ["eligibility", "Eligibility"],
];

const STATUS = {
  detected: ["Detected", "bg-sky-50 text-sky-700 ring-sky-200"],
  needs_review: ["Human review", "bg-amber-50 text-amber-800 ring-amber-200"],
  ready: ["Ready", "bg-emerald-50 text-emerald-700 ring-emerald-200"],
  in_progress: ["In progress", "bg-violet-50 text-violet-700 ring-violet-200"],
  submitted: ["Submitted", "bg-slate-100 text-slate-700 ring-slate-300"],
};

function Pill({ tone, children }) {
  return (
    <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>
      {children}
    </span>
  );
}

const OUTCOMES = [
  ["reached", "Reached patient"],
  ["left_message", "Left message"],
  ["no_answer", "No answer"],
  ["declined", "Patient declined"],
];

function FollowUpTask({
  task, loading, error, manageChase, actionBusy, actionError,
  actionNotice, retryAction, onAction,
}) {
  const [outcome, setOutcome] = useState("reached");
  const [minutes, setMinutes] = useState("0");
  const [note, setNote] = useState("");
  const [confirming, setConfirming] = useState(null);
  const may = (action) => (
    manageChase && Array.isArray(task?.allowedActions)
      && task.allowedActions.includes(action)
  );

  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white p-5 text-sm text-slate-500 shadow-sm">
        <RefreshCw size={16} className="animate-spin" /> Loading the audited contact task…
      </div>
    );
  }
  if (error) {
    return <p className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{error}</p>;
  }
  if (!task) {
    return (
      <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-5 text-sm text-slate-500">
        Select a worklist row to open its minimum-necessary contact task.
      </div>
    );
  }
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-slate-900">{task.patientLabel}</p>
          <p className="mt-0.5 text-xs text-slate-500">Manual insurance follow-up task</p>
        </div>
        <Pill tone={task.dataClass === "synthetic_demo"
          ? "bg-violet-50 text-violet-700 ring-violet-200"
          : "bg-slate-100 text-slate-600 ring-slate-300"}>
          {task.dataClass === "synthetic_demo" ? "synthetic" : "system of record"}
        </Pill>
      </div>

      <p className="mt-4 text-sm leading-relaxed text-slate-700">{task.instruction}</p>

      {task.contact ? (
        <div className="mt-4 rounded-xl border border-sky-200 bg-sky-50 p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-sky-700">
            <Phone size={13} /> Synthetic manual-call destination
          </p>
          <p className="mt-1 font-mono text-sm text-slate-900">{task.contact.destination}</p>
          <p className="mt-1 text-xs text-sky-800">
            Display only. The application does not dial, text, email, or open an external link.
          </p>
        </div>
      ) : (
        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <p className="flex items-center gap-1.5 font-medium">
            <LockKeyhole size={15} /> Contact destination withheld
          </p>
          <p className="mt-1 text-xs leading-relaxed text-amber-800">
            No contact destination is available in this surface. Follow the task instruction above.
          </p>
        </div>
      )}

      <div className="mt-4 rounded-xl bg-slate-50 p-3 text-xs leading-relaxed text-slate-600">
        <p className="flex items-center gap-1.5 font-medium text-slate-700">
          <UserRoundSearch size={14} /> {task.systemOfRecord.name}
        </p>
        <p className="mt-1">{task.systemOfRecord.instruction}</p>
      </div>

      {!manageChase ? (
        <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">
          This worklist is read-only for your assigned role.
        </div>
      ) : !task.open ? (
        <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800">
          This task is closed. No additional patient contact is needed.
        </div>
      ) : (
        <div className="mt-5 border-t border-slate-200 pt-4">
          <p className="text-sm font-medium text-slate-900">Staff actions</p>
          <p className="mt-1 text-xs text-slate-500">
            These controls record work only. They never call, text, email, or write to a source system.
          </p>

          {!task.assigned && may("assign") && (
            <button
              type="button"
              disabled={actionBusy}
              onClick={() => onAction({ action: "assign" }, "Task assigned to you")}
              className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
            >
              <UserCheck size={14} /> Assign to me
            </button>
          )}

          {may("log_attempt") && (
            <form
              className="mt-4 rounded-xl border border-slate-200 p-3"
              onSubmit={(event) => {
                event.preventDefault();
                onAction({
                  action: "log_attempt",
                  channel: "phone",
                  outcome,
                  minutes_spent: Number(minutes),
                  note,
                }, "Manual attempt recorded");
              }}
            >
              <p className="text-xs font-medium uppercase tracking-wide text-slate-600">
                Log a manual phone attempt
              </p>
              <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-[1fr_7rem]">
                <label className="text-xs text-slate-600">
                  Outcome
                  <select
                    value={outcome}
                    onChange={(event) => setOutcome(event.target.value)}
                    className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-sm text-slate-900"
                  >
                    {OUTCOMES.map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                </label>
                <label className="text-xs text-slate-600">
                  Minutes
                  <input
                    type="number"
                    min="0"
                    max="1440"
                    step="0.5"
                    required
                    value={minutes}
                    onChange={(event) => setMinutes(event.target.value)}
                    className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-2 text-sm text-slate-900"
                  />
                </label>
              </div>
              <label className="mt-2 block text-xs text-slate-600">
                Note (optional)
                <textarea
                  value={note}
                  maxLength={4000}
                  onChange={(event) => setNote(event.target.value)}
                  className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-2 text-sm text-slate-900"
                  rows={2}
                />
              </label>
              <button
                type="submit"
                disabled={actionBusy || Number(minutes) < 0 || Number(minutes) > 1440}
                className="mt-2 inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-60"
              >
                <Phone size={14} /> Record attempt
              </button>
            </form>
          )}

          <div className="mt-3 flex flex-wrap gap-2">
            {may("submitted") && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => setConfirming("submitted")}
                className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
              >
                Mark submitted
              </button>
            )}
            {may("verify") && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => setConfirming("verify")}
                className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-60"
              >
                <CheckCircle2 size={14} /> Verify and close
              </button>
            )}
          </div>

          {!may("log_attempt") && !may("submitted") && !may("verify") && (
            <p className="mt-3 text-xs text-amber-800">
              This task requires a reviewed workflow transition before contact can be recorded.
            </p>
          )}

          {confirming && (
            <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 p-3" role="group" aria-label="Confirm staff action">
              <p className="text-sm font-medium text-amber-950">
                {confirming === "submitted"
                  ? "Confirm that insurance information was submitted for staff review."
                  : "Confirm that staff verified the insurance information in the system of record."}
              </p>
              <p className="mt-1 text-xs text-amber-800">
                This records workflow state only; it does not write to a source system or contact the patient.
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  disabled={actionBusy}
                  onClick={async () => {
                    const action = confirming;
                    setConfirming(null);
                    await onAction(
                      { action },
                      action === "submitted" ? "Submission recorded" : "Task verified and closed",
                    );
                  }}
                  className="rounded-lg bg-amber-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-900 disabled:opacity-60"
                >
                  Confirm
                </button>
                <button
                  type="button"
                  disabled={actionBusy}
                  onClick={() => setConfirming(null)}
                  className="rounded-lg border border-amber-300 px-3 py-1.5 text-sm text-amber-900 hover:bg-amber-100 disabled:opacity-60"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {actionNotice && <p className="mt-3 text-xs text-emerald-800" role="status">{actionNotice}</p>}
          {actionError && <p className="mt-3 text-xs text-red-700" role="alert">{actionError}</p>}
          {retryAction && (
            <button
              type="button"
              disabled={actionBusy}
              onClick={retryAction}
              className="mt-2 inline-flex items-center gap-1.5 rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-800 hover:bg-red-50 disabled:opacity-60"
            >
              <RefreshCw size={13} /> Retry the same action safely
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default function CardChase({ live, demoSeed = null, manageChase = false }) {
  const [bucket, setBucket] = useState("");
  const [worklist, setWorklist] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [seeding, setSeeding] = useState(false);
  const [seedResult, setSeedResult] = useState(null);
  const [seedError, setSeedError] = useState(null);
  const [task, setTask] = useState(null);
  const [taskLoading, setTaskLoading] = useState(false);
  const [taskError, setTaskError] = useState(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState(null);
  const [actionNotice, setActionNotice] = useState(null);
  const [retry, setRetry] = useState(null);
  const [selectedChaseId, setSelectedChaseId] = useState(null);
  const selectedChaseIdRef = useRef(null);
  const taskRequestIdRef = useRef(0);

  const load = async () => {
    if (!live) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api("/api/chase/worklist", {
        method: "POST",
        body: bucket ? { bucket } : {},
      });
      setWorklist(result);
    } catch (loadError) {
      setError(`Could not load insurance follow-up work: ${loadError.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    selectedChaseIdRef.current = null;
    setSelectedChaseId(null);
    taskRequestIdRef.current += 1;
    setTask(null);
    setTaskError(null);
    load();
  }, [bucket, live]); // eslint-disable-line react-hooks/exhaustive-deps

  const openTaskById = async (chaseId) => {
    const requestId = ++taskRequestIdRef.current;
    setTask(null);
    setTaskError(null);
    setTaskLoading(true);
    try {
      const result = await api("/api/chase/task", {
        method: "POST",
        body: { chaseId },
      });
      if (requestId !== taskRequestIdRef.current) return;
      setTask(result);
    } catch (loadError) {
      if (requestId !== taskRequestIdRef.current) return;
      setTaskError(`Could not open contact task: ${loadError.message}`);
    } finally {
      if (requestId === taskRequestIdRef.current) setTaskLoading(false);
    }
  };

  const openTask = async (row) => {
    selectedChaseIdRef.current = row.chaseId;
    setSelectedChaseId(row.chaseId);
    setActionError(null);
    setActionNotice(null);
    setRetry(null);
    await openTaskById(row.chaseId);
  };

  const submitAction = async (
    payload, successMessage, actionId = null, targetChaseId = task?.chaseId,
  ) => {
    if (!manageChase || !targetChaseId) return;
    const chaseId = targetChaseId;
    setActionBusy(true);
    setActionError(null);
    setActionNotice(null);
    setRetry(null);
    let stableActionId = actionId;
    try {
      stableActionId = stableActionId || crypto.randomUUID();
      await api("/api/chase/action", {
        method: "POST",
        body: { chaseId, action_id: stableActionId, ...payload },
      });
      await load();
      if (selectedChaseIdRef.current === chaseId) {
        setActionNotice(successMessage);
        await openTaskById(chaseId);
      }
    } catch (actionFailure) {
      if (selectedChaseIdRef.current !== chaseId) return;
      const retryable = !actionFailure?.status
        || actionFailure.status === 429
        || actionFailure.status >= 500;
      setActionError(
        actionFailure?.status === 409
          ? "This task changed while you were working. Refresh it before trying again."
          : actionFailure?.status === 422
            ? "That action was not valid for the current task. Review the fields and try again."
            : "The action could not be confirmed. No automatic contact was sent.",
      );
      if (retryable && stableActionId) {
        setRetry({
          payload, successMessage, actionId: stableActionId, chaseId,
        });
      }
    } finally {
      setActionBusy(false);
    }
  };

  const seedSyntheticDemo = async () => {
    if (!live || !demoSeed?.seedDigest) return;
    setSeeding(true);
    setSeedError(null);
    setSeedResult(null);
    let confirmed = false;
    try {
      const result = await api("/api/chase/demo-seed", {
        method: "POST",
        body: { seedDigest: demoSeed.seedDigest },
      });
      if (!seedConfirmationIsSafe(result, demoSeed)) {
        throw new Error("synthetic demo seed confirmation did not match");
      }
      setSeedResult(result);
      confirmed = true;
      setTask(null);
      setTaskError(null);
    } catch (seedFailure) {
      setSeedError(syntheticSeedErrorMessage(seedFailure));
    } finally {
      setSeeding(false);
    }
    if (confirmed) await load();
  };

  if (!live) {
    return (
      <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-sm text-amber-900 shadow-sm">
        Insurance follow-up is unavailable while the service is offline. No mock patient task was loaded.
      </div>
    );
  }

  const counts = worklist?.counts;
  const outbound = worklist?.outbound;
  return (
    <div>
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-violet-50 text-violet-700">
              <CreditCard size={19} />
            </span>
            <div>
              <p className="text-sm font-medium text-slate-900">Insurance follow-up</p>
              <p className="text-xs text-slate-500">Missing cards, expired plans, and eligibility exceptions</p>
            </div>
          </div>
          <button
            onClick={load}
            disabled={loading}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-60"
          >
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} /> Refresh
          </button>
        </div>

        {demoSeed && (
          <div className="mt-4 rounded-xl border border-violet-200 bg-violet-50 p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium text-violet-950">Synthetic demo setup</p>
                <p className="mt-0.5 text-xs leading-relaxed text-violet-800">
                  Admin only. Load the fixed {demoSeed.caseCount}-case synthetic pack; retries are safe and do not create duplicates.
                </p>
              </div>
              <button
                onClick={seedSyntheticDemo}
                disabled={seeding}
                className="inline-flex items-center gap-1.5 rounded-lg bg-violet-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-violet-800 disabled:opacity-60"
              >
                <RefreshCw size={14} className={seeding ? "animate-spin" : ""} />
                {seeding ? "Loading synthetic tasks…" : "Load synthetic demo tasks"}
              </button>
            </div>
            {seedResult && (
              <p className="mt-2 text-xs text-emerald-800" role="status">
                {syntheticSeedConfirmationMessage(seedResult)}
              </p>
            )}
            {seedError && <p className="mt-2 text-xs text-red-700" role="alert">{seedError}</p>}
          </div>
        )}

        <div className="mt-4 flex flex-wrap gap-2">
          {FILTERS.map(([value, label]) => (
            <button
              type="button"
              key={value || "all"}
              onClick={() => setBucket(value)}
              disabled={actionBusy}
              aria-pressed={bucket === value}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium ring-1 ring-inset ${
                bucket === value
                  ? "bg-slate-900 text-white ring-slate-900"
                  : "bg-white text-slate-600 ring-slate-200 hover:bg-slate-50"
              }`}
            >
              {label}{counts && value ? ` · ${counts.by_bucket?.[value] || 0}` : counts ? ` · ${counts.open}` : ""}
            </button>
          ))}
        </div>

        {outbound && (
          <div className="mt-4 flex items-start gap-2 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-xs leading-relaxed text-emerald-900">
            <ShieldAlert size={15} className="mt-0.5 shrink-0" />
            <span>
              Outbound automation is {outbound.automaticContact ? "active" : "off"}; transport is {outbound.transport}.
              This surface opens manual tasks only and never sends patient messages.
            </span>
          </div>
        )}
      </div>

      {error && <p className="mt-4 rounded-xl bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      <div className="mt-5 grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1.35fr)_minmax(18rem,0.85fr)]">
        <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
          <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3 text-xs font-medium uppercase tracking-wide text-slate-500">
            <span>Patient task</span><span>Priority</span>
          </div>
          {loading && !worklist ? (
            <p className="p-5 text-sm text-slate-500">Loading follow-up work…</p>
          ) : (worklist?.rows || []).length === 0 ? (
            <p className="p-5 text-sm text-slate-500">No open tasks match this filter.</p>
          ) : (
            <div className="divide-y divide-slate-100">
              {worklist.rows.map((row) => {
                const [statusLabel, statusTone] = STATUS[row.state] || [row.state, "bg-slate-100 text-slate-600 ring-slate-300"];
                return (
                  <button
                    type="button"
                    key={row.chaseId}
                    onClick={() => openTask(row)}
                    disabled={actionBusy}
                    aria-pressed={selectedChaseId === row.chaseId}
                    className="grid w-full grid-cols-[minmax(0,1fr)_auto] gap-3 px-4 py-3 text-left hover:bg-slate-50 disabled:cursor-wait disabled:opacity-60"
                  >
                    <span>
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-slate-900">{row.patientLabel}</span>
                        <Pill tone={statusTone}>{statusLabel}</Pill>
                        {row.dataClass === "synthetic_demo" && (
                          <Pill tone="bg-violet-50 text-violet-700 ring-violet-200">synthetic</Pill>
                        )}
                      </span>
                      <span className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                        <span>{row.bucket}</span>
                        {row.appointmentAt && (
                          <span className="inline-flex items-center gap-1"><CalendarClock size={12} />{row.appointmentAt.slice(0, 10)}</span>
                        )}
                        {row.assigned && <span>assigned</span>}
                      </span>
                      {row.reviewReason && <span className="mt-1 block text-xs text-amber-700">{row.reviewReason}</span>}
                    </span>
                    <span className="font-mono text-sm font-medium text-slate-700">{row.priority}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <FollowUpTask
          key={task?.chaseId || "empty-task"}
          task={task}
          loading={taskLoading}
          error={taskError}
          manageChase={manageChase}
          actionBusy={actionBusy}
          actionError={actionError}
          actionNotice={actionNotice}
          retryAction={retry ? () => submitAction(
            retry.payload, retry.successMessage, retry.actionId, retry.chaseId,
          ) : null}
          onAction={submitAction}
        />
      </div>
    </div>
  );
}
