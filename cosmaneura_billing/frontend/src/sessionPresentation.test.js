import assert from "node:assert/strict";
import test from "node:test";

import { sessionPersonaLabel } from "./sessionPresentation.js";


test("renders the server-derived persona without inferring from capabilities", () => {
  assert.equal(sessionPersonaLabel("Medical coder"), "Medical coder");
  assert.equal(sessionPersonaLabel("  Billing staff  "), "Billing staff");
  assert.equal(sessionPersonaLabel("Future limited persona"), "Future limited persona");
});


test("uses a neutral label when the server persona is absent", () => {
  assert.equal(sessionPersonaLabel(undefined), "Assigned pilot user");
  assert.equal(sessionPersonaLabel(null), "Assigned pilot user");
  assert.equal(sessionPersonaLabel("   "), "Assigned pilot user");
});
