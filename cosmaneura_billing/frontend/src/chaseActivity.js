export const CHASE_CHANNELS = Object.freeze({
  phone: "Phone", email: "Email", sms: "SMS", portal: "Portal",
});

const OUTCOMES = Object.freeze({
  reached: "Reached person",
  left_message: "Left message",
  no_answer: "No answer",
  wrong_number: "Wrong number",
  declined: "Declined",
  sent: "Sent — delivery unconfirmed",
  accepted: "Accepted by provider — delivery unconfirmed",
  delivered: "Delivered",
  bounced: "Bounced",
  recorded: "Recorded",
  unknown: "Outcome unavailable",
  queued: "Queued by provider — delivery unconfirmed", scheduled: "Scheduled by provider",
  sending: "Sending", read: "Read reported by provider", failed: "Failed", undelivered: "Not delivered",
  rejected: "Rejected by provider", canceled: "Canceled by provider", initiated: "Call starting",
  ringing: "Ringing", "in-progress": "Call answered", completed: "Call completed",
  busy: "Busy", "no-answer": "No answer",
  confirmed_delivered: "Delivery confirmed by staff", confirmed_not_sent: "Confirmed not sent by staff",
  confirmed_attempted: "Attempt confirmed by staff — delivery unknown",
});

const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const count = (value) => Number.isSafeInteger(value) && value >= 0;
const minutes = (value) => Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER;
const owns = (collection, value) => typeof value === "string" && Object.hasOwn(collection, value);
const rounded = (value) => Math.round((value + Number.EPSILON) * 100) / 100;

function validInstant(value) {
  if (typeof value !== "string" || value.length > 40 || value.endsWith("-00:00")) return false;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(Z|[+-](\d{2}):(\d{2}))$/.exec(value);
  if (!match || Number(match[2]) > 23 || Number(match[3]) > 59 || Number(match[4]) > 59
      || Number(match[6] || 0) > 23 || Number(match[7] || 0) > 59) return false;
  const day = new Date(`${match[1]}T00:00:00Z`);
  return Number.isFinite(day.getTime()) && day.toISOString().slice(0, 10) === match[1]
    && Number.isFinite(Date.parse(value));
}

function projectAttempt(value) {
  if (!object(value) || !validInstant(value.at) || !owns(CHASE_CHANNELS, value.channel)
      || !["staff", "system"].includes(value.performer) || !owns(OUTCOMES, value.outcome)
      || !minutes(value.minutes) || value.minutes > 1440) return null;
  // Whitelist the display DTO; never forward staff IDs, notes, or destinations.
  return { at: value.at, channel: value.channel, performer: value.performer,
    outcome: value.outcome, minutes: value.minutes };
}

/** Missing/unsupported/inconsistent data is unavailable, never a zero-contact claim. */
export function readChaseActivity(value) {
  if (!object(value) || value.schemaVersion !== "chase-activity.v1"
      || !count(value.totalAttempts) || !minutes(value.staffMinutes)
      || !object(value.countsByChannel) || !Array.isArray(value.history)
      || value.history.length !== Math.min(value.totalAttempts, 50)
      || value.truncated !== (value.totalAttempts > 50)) return null;
  const countsByChannel = {};
  for (const channel of Object.keys(CHASE_CHANNELS)) {
    if (!count(value.countsByChannel[channel])) return null;
    countsByChannel[channel] = value.countsByChannel[channel];
  }
  if (Object.values(countsByChannel).reduce((sum, amount) => sum + amount, 0) !== value.totalAttempts) return null;
  const history = value.history.map(projectAttempt);
  if (history.some((entry) => !entry)) return null;
  for (let index = 1; index < history.length; index += 1) {
    if (Date.parse(history[index].at) > Date.parse(history[index - 1].at)) return null;
  }
  const lastAttempt = value.lastAttempt === null ? null : projectAttempt(value.lastAttempt);
  if (value.totalAttempts === 0 ? value.lastAttempt !== null
    : !lastAttempt || JSON.stringify(lastAttempt) !== JSON.stringify(history[0])) return null;
  const visibleMinutes = rounded(history.reduce((sum, entry) => sum + entry.minutes, 0));
  // Python and JavaScript round halfway values differently; allow one hundredth
  // while rejecting a total that does not describe the displayed contact log.
  const minuteDifference = visibleMinutes - rounded(value.staffMinutes);
  if (minuteDifference > 0.010000001
      || (!value.truncated && Math.abs(minuteDifference) > 0.010000001)) return null;
  for (const channel of Object.keys(CHASE_CHANNELS)) {
    const visibleCount = history.filter((entry) => entry.channel === channel).length;
    if (visibleCount > countsByChannel[channel]
        || (!value.truncated && visibleCount !== countsByChannel[channel])) return null;
  }
  return { totalAttempts: value.totalAttempts, countsByChannel,
    staffMinutes: value.staffMinutes, lastAttempt, history, truncated: value.truncated };
}

export function chaseOutcomeLabel(outcome) { return owns(OUTCOMES, outcome) ? OUTCOMES[outcome] : "Outcome unavailable"; }

export function chaseAttemptTime(at) {
  if (!validInstant(at)) return "Time unavailable";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
  }).format(new Date(at));
}

export function chaseMinutesLabel(value) {
  return minutes(value) ? new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value) : "Unavailable";
}
