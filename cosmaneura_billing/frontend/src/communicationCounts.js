import {reviewCounts} from "./communicationsEventReview.js";

const identifier = (value) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const require = (condition) => { if (!condition) throw new Error("Communication counts could not be verified."); };

export function listedCaseIds(worklist) {
  require(Array.isArray(worklist?.rows));
  const ids = worklist.rows.map((row) => row.chaseId);
  require(ids.every(identifier) && new Set(ids).size === ids.length);
  return ids;
}

export function hasCommunicationCounts(worklist) {
  return Array.isArray(worklist?.rows) && worklist.rows.length > 0
    && worklist.rows.every((row) => reviewCounts(row.communicationReview) !== null);
}

/** Read only known case counts; fetched contact/state/ordering data has no authority. */
export function readListedCounts(response, expectedIds) {
  require(Array.isArray(expectedIds) && expectedIds.every(identifier) && new Set(expectedIds).size === expectedIds.length);
  const ids = listedCaseIds(response), wanted = new Set(expectedIds), counts = new Map();
  for (let i = 0; i < ids.length; i++) {
    if (!wanted.has(ids[i])) continue;
    const parsed = reviewCounts(response.rows[i].communicationReview);
    require(parsed !== null);
    counts.set(ids[i], parsed);
  }
  // A case leaving this worklist needs an explicit board reload, not inferred zero.
  require(counts.size === wanted.size);
  return counts;
}

export function mergeListedCounts(worklist, counts, preserveIds = new Set()) {
  const ids = listedCaseIds(worklist);
  require(counts instanceof Map && ids.every((id) => counts.has(id) && reviewCounts(counts.get(id))));
  require(preserveIds instanceof Set && [...preserveIds].every((id) => ids.includes(id)));
  return {...worklist, rows: worklist.rows.map((row) => ({...row,
    // A newer selected-case read wins without suppressing other cases' updates.
    communicationReview: preserveIds.has(row.chaseId) ? row.communicationReview : {...counts.get(row.chaseId)}}))};
}

/** Bounded read-only polling. Caller epochs protect its newer local decisions. */
export function pollVisibleCounts({read, accept, failed, blocked, page = document, timer = window}) {
  let stopped = false, active = null;
  async function refresh() {
    if (stopped || page.hidden || blocked() || active) return;
    const controller = new AbortController();
    active = controller;
    let timedOut = false;
    const deadline = timer.setTimeout(() => { timedOut = true; controller.abort(); }, 10_000);
    try {
      const value = await read(controller.signal);
      if (stopped || page.hidden || blocked() || active !== controller) return;
      if (timedOut) throw new Error("Communication count update timed out.");
      accept(value);
    } catch (error) {
      if (!stopped && !page.hidden && !blocked() && active === controller
        && (error.name !== "AbortError" || timedOut)) failed(error);
    } finally {
      timer.clearTimeout(deadline);
      if (active === controller) active = null;
    }
  }
  function visibility() {
    if (page.hidden) { active?.abort(); active = null; }
    else refresh();
  }
  const interval = timer.setInterval(refresh, 15_000);
  page.addEventListener("visibilitychange", visibility);
  return () => {
    stopped = true; active?.abort(); active = null;
    timer.clearInterval(interval); page.removeEventListener("visibilitychange", visibility);
  };
}
