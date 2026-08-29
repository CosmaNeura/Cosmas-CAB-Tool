const PIPELINE_STAGES = [
  "CodingAgent", "DocumentationIntegrityAgent",
  "ComplianceGuardrailAgent", "DenialRiskAgent",
];
const SOURCES = {
  signed_note: ["signed_note", "signedNote"], transcript: ["transcript"],
  intake_text: ["intake_text", "intakeText", "intake"],
};
const OUTCOMES = ["reached", "left_message", "no_answer", "wrong_number", "declined"];
const TERMINAL_OUTCOMES = new Set(["reached", "declined"]);
const SHA256 = /^[0-9a-f]{64}$/;
const FICTIONAL_PHONE = /^\+1[2-9][0-9]{2}55501[0-9]{2}$/;
const SYNTHETIC_STAFF = /^SYN-STAFF-[A-Z0-9][A-Z0-9-]{0,63}$/;
const PERSON_REF = /^SYN-MCD-[A-Z0-9][A-Z0-9-]{0,63}$/;

const sourceText = (documents, source) => SOURCES[source]
  ?.map((key) => documents?.[key]).find((value) => typeof value === "string") ?? null;

export function isExactEvidenceSpan(span, documents) {
  const points = Array.from(sourceText(documents, span?.source) ?? "");
  const { start, end } = span?.loc ?? {};
  return Boolean(typeof span?.text === "string" && span.text.trim()
    && Number.isInteger(start) && Number.isInteger(end)
    && start >= 0 && end > start && end <= points.length
    && points.slice(start, end).join("") === span.text);
}

function hasDormantSuggestedCodePosture(payload) {
  return payload?.enabled === false && payload.status === "ready"
    && payload.human_verification_required === true
    && payload.pipeline?.source === "BillingResult"
    && payload.pipeline.advisory_only === true
    && JSON.stringify(payload.pipeline.stages) === JSON.stringify(PIPELINE_STAGES);
}

export function visibleSuggestedCodes(payload, documents) {
  if (!hasDormantSuggestedCodePosture(payload) || payload.degraded) return [];
  return (Array.isArray(payload.codes) ? payload.codes : []).filter((row) => (
    typeof row?.code === "string" && row.code.trim() && row.status === "suggested"
    && row.selectable === true && row.copy_text === row.code
    && Array.isArray(row.evidence_spans) && row.evidence_spans.length > 0
    && row.evidence_spans.every((span) => isExactEvidenceSpan(span, documents))
  ));
}

const validTime = (value) => typeof value === "string" && Number.isFinite(Date.parse(value));
const safeDirectContract = (gate, personRef) => gate?.patient_ref === personRef
  && gate.known === true && gate.is_direct_contract === false && gate.blocked === false
  && typeof gate.source === "string" && gate.source.length > 0
  && !gate.source.startsWith("unavailable:") && SHA256.test(gate.gate_fingerprint)
  && validTime(gate.checked_at);

function safeAction(action, taskId) {
  return typeof action?.action_id === "string" && action.action_id
    && action.task_id === taskId && action.channel === "phone"
    && action.performed_by === "staff" && action.automated_outbound_initiated === false
    && action.manual_outreach_recorded === true && SYNTHETIC_STAFF.test(action.actor_ref)
    && OUTCOMES.includes(action.outcome) && Number.isFinite(action.minutes_spent)
    && action.minutes_spent >= 0 && action.minutes_spent <= 480
    && validTime(action.occurred_at);
}

function safeTask(task) {
  const person = task?.person;
  const actionsSafe = Array.isArray(task?.actions)
    && task.actions.every((action) => safeAction(action, task.task_id));
  const hasTerminalAction = actionsSafe
    && task.actions.some((action) => TERMINAL_OUTCOMES.has(action.outcome));
  return typeof task?.task_id === "string" && task.task_id
    && typeof task.roster_id === "string" && validTime(task.created_at)
    && task.task_type === "insurance_card_phone_outreach" && task.channel === "phone"
    && task.performed_by === "staff" && task.automated === false
    && task.transport === "record_only" && ["open", "completed"].includes(task.status)
    && Number.isInteger(task.priority) && person?.synthetic === true
    && PERSON_REF.test(person.person_ref) && /^Synthetic (?:Person|Patient) /.test(person.display_label)
    && FICTIONAL_PHONE.test(person.phone) && validTime(person.due_at)
    && person.requested_channel === "phone" && person.voice_consent === true
    && safeDirectContract(task.direct_contract, person.person_ref)
    && actionsSafe && (task.status === "completed" ? hasTerminalAction : !hasTerminalAction);
}

export function hasSafeChaseEnvelope(worklist) {
  const safety = worklist?.safety;
  const counts = worklist?.counts;
  return worklist?.mode === "synthetic_only" && Array.isArray(worklist.tasks)
    && safety?.transport === "record_only" && safety.scheduler_enabled === false
    && safety.patient_messaging_enabled === false
    && safety.patient_messaging_operationally_activated === false
    && safety.cab_deploy_enabled === false
    && ["disabled", "mock"].includes(safety.athena_mode)
    && [counts?.open, counts?.completed, counts?.total]
      .every((value) => Number.isInteger(value) && value >= 0)
    && counts.total === worklist.tasks.length
    && counts.open === worklist.tasks.filter((task) => task?.status === "open").length
    && counts.completed === worklist.tasks.filter((task) => task?.status === "completed").length;
}

export function safeChaseTasks(worklist) {
  return hasSafeChaseEnvelope(worklist) ? worklist.tasks.filter(safeTask) : [];
}

export const outcomeCompletesTask = (outcome) => TERMINAL_OUTCOMES.has(outcome);

export function recordPhoneAttempt(task, input, meta) {
  if (!safeTask(task)) throw new Error("unsafe-task");
  if (!OUTCOMES.includes(input?.outcome)) throw new Error("invalid-outcome");
  const minutes = Number(input.minutes_spent);
  if (!Number.isFinite(minutes) || minutes < 0 || minutes > 480) throw new Error("invalid-minutes");
  if (!meta?.action_id || !SYNTHETIC_STAFF.test(meta.actor_ref) || !validTime(meta.occurred_at)) {
    throw new Error("invalid-metadata");
  }
  return Object.freeze({
    action_id: meta.action_id, task_id: task.task_id, channel: "phone",
    performed_by: "staff", automated_outbound_initiated: false,
    manual_outreach_recorded: true, actor_ref: meta.actor_ref,
    outcome: input.outcome, minutes_spent: minutes, occurred_at: meta.occurred_at,
  });
}
