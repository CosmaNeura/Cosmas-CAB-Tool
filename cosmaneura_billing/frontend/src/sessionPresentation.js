/**
 * @typedef {Object} SessionResponse
 * @property {string} persona Server-derived display name for the active persona.
 * @property {Array<"provider" | "coder" | "chase">} views
 * @property {"provider" | "coder" | "chase"} defaultView
 * @property {{manageChase?: boolean}} [capabilities]
 * @property {{seedSyntheticChase?: unknown}} [actions]
 */

/**
 * Preserve the server's persona label without recreating its authorization
 * rules in the browser. A missing label is deliberately generic rather than
 * inferring a role from the views returned for the session.
 *
 * @param {unknown} persona
 * @returns {string}
 */
export function sessionPersonaLabel(persona) {
  if (typeof persona !== "string") return "Assigned pilot user";
  const label = persona.trim();
  return label || "Assigned pilot user";
}
