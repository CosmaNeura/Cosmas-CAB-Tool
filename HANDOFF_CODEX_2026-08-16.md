# Handoff — platform stabilization (for Codex)

**Repo:** `CosmaNeura/Cosmas-CAB-Tool`
**Working folder:** `/home/user/Cosmas-CAB-Tool` → the tool lives in **`cosmaneura_billing/`**
(the repo root still holds the June 2023 FluencyMed prototype; see §7)
**Branch to develop on:** `claude/platform-handoff-codex-obnlqb` (currently identical to `main`)
**Baseline verified:** 2026-08-16 — 156 tests pass, API boots in ~14 s with real 2026 regulatory
data, frontend builds clean.

---

## 0. Scope boundary — read this first

John is separately integrating **significant user feedback into feature performance and
UI/UX**. That work is his lane. **Do not** redesign the React components, restyle, rename
UI copy, or re-tune coding/E-M behavior for accuracy in response to user feedback.

Your lane is everything underneath: correctness of the degraded path, the real-inference
path, persistence, deployment, auth, and CI. Where a fix needs a UI surface (§1), add the
**data and the minimal state**, not a design — leave a clearly-marked seam John can restyle.

Keep the diff surface in `cosmaneura_billing/frontend/src/` as small as possible so his
UI/UX work doesn't collide with yours.

---

## 1. Priority 1 — the offline/degraded path lies about its results

**This is the highest-value item and it is actively poisoning the user-feedback loop.**

With no `ANTHROPIC_API_KEY` set (the default, and how every demo has been run),
`MockLLM` (`cosmaneura_billing/llm.py:178`) returns the **same canned GI-bleed facts for
every encounter regardless of input**. The evidence verifier (`evidence.py`) correctly
refuses to attach quotes that don't appear in the actual note — so on the 10 ACI-Bench
encounters the pipeline produces a claim with **nothing in it**.

Verified today:

```
GET /api/encounters/aci-virtassist-D2N001/coder-view
→ { "em": null, "dx": [], "withheld": [ GI-bleed codes, evidence: "" ] }
```

The UI renders that as an empty review with **no explanation**. A coder clicking through
encounter 2 of 11 concludes the engine is broken. Every piece of user feedback gathered on
a non-demo encounter in offline mode is feedback about a bug, not about the product.

Fix, in this order:

1. **Report inference mode.** `/api/health` currently says nothing about which LLM is
   live. Add `inferenceMode: "mock" | "anthropic"` and the model id. Add the same field to
   `/api/billing`, `/api/encounters/{id}/billing`, and `/api/encounters/{id}/coder-view`
   responses so a stored result is self-describing.
2. **Make MockLLM honest.** It should return facts only for the encounter it actually
   models (`DEMO-GI-001`) and return `{}` — or raise a typed `UnsupportedEncounter` — for
   anything else, rather than fabricating GI-bleed facts for every note. Today the only
   reason those fabricated codes don't reach a claim is the evidence verifier catching
   them downstream; that is a safety net doing a job the mock shouldn't create.
3. **Surface the degraded state.** When the pipeline produced no codes *because* the
   offline model doesn't cover the encounter, the response needs a machine-readable reason
   (e.g. `degraded: {reason: "offline_model_unsupported_encounter", …}`). Add a plain,
   unstyled empty state in `CoderReview.jsx` / `BillingReview.jsx` that reads it — one
   sentence, minimal markup, clearly marked with a comment as a seam for John's UI pass.
4. **Decide what the demo corpus is.** Either (a) hand-author mock fact payloads for the
   10 ACI-Bench encounters so offline mode is genuinely walkable end-to-end, or (b) cut the
   encounter list in offline mode to what the mock actually supports. (a) is more work and
   much more useful for gathering coder feedback without an API key. Recommend (a) —
   flag it to John before investing the time, since he may prefer keys-on pilots.

Also drop the frontend's hand-maintained `FALLBACK_ENGINE` constant
(`CoderReview.jsx:13`) — it duplicates the API's response shape by hand and will drift.
Have offline mode come from the API's degraded response instead.

---

## 2. Priority 2 — real-inference path

The real path is written and tested but has never carried the pilot.

- **Stale default model.** `llm.py:89` defaults `ANTHROPIC_MODEL` to `claude-opus-4-8`.
  Confirm the intended model and update; make the resolved model id visible in
  `/api/health` (see §1.1) so nobody has to guess what produced a claim.
- **No PHI guard.** `AnthropicLLM` posts the full intake + transcript + signed note to the
  API. There is no gate, no redaction, and no documented data flow. Add an explicit
  opt-in (e.g. refuse to run real inference unless `COSMANEURA_PHI_OK=1` is set) and write
  the data-flow doc that the BAA conversation will need. Until a BAA is in place, the
  rule from `STATUS_REVIEW_2026-08-06.md` still holds: de-identified notes only.
- **No cost/latency accounting.** Add per-request token and wall-clock accounting to
  `complete_json`, logged structurally. Without it nobody can price a chart.
- **No recorded-fixture test.** `tests/test_anthropic_llm.py` covers the adapter's
  mechanics. Add a fixture-replay test (captured request/response pairs, key not required)
  so the real-mode pipeline is regression-covered in CI, not only the mock path.

---

## 3. Priority 3 — persistence and multi-user safety

Coder feedback is the product's learning loop. It is currently a local file.

- `FeedbackStore.append` (`coder_feedback.py:104`) opens a file in `"a"` mode with no
  locking and no atomicity. Two coders on one server can interleave writes. Every summary
  and every preference derivation reads and parses the **entire** file (`load()`,
  `PreferenceStore._load`).
- There is no practice/tenant separation, no user table, and no audit of who viewed or
  decided what.
- `migrations/001_init_pgvector.up.sql` covers only the dialogue-RAG vector table. Nothing
  exists for encounters, feedback, preferences, users, or audit — and no Postgres is stood up.

Work: define a storage interface behind `FeedbackStore`/`PreferenceStore`, keep JSONL as
the zero-setup dev default, add a Postgres implementation plus migrations for the tables
above, and a migration path for existing JSONL records. Make the choice a config flag, not
an import-time surprise.

---

## 4. Priority 4 — deployment and runtime

Today the tool is a two-terminal localhost dev setup. There is **no Dockerfile, no compose
file, no served frontend build, and no config module**.

- **Package it.** Dockerfile + compose (API, built frontend, Postgres). The FastAPI app
  should serve the built frontend from the same origin — `frontend/src/api.js` already
  assumes same-origin in production, but nothing implements it.
- **Fix cold start.** First request pays ~14 s (NCCI/ICD-10/coverage load, measured today)
  because `get_orchestrator()` is lazily `lru_cache`d. Warm it on startup and add a
  readiness endpoint so a load balancer doesn't route to a cold process.
- **Centralize config.** 23 scattered `os.environ` reads across the package
  (`ANTHROPIC_*`, `AMA_*`, `CPT_*`, `COSMANEURA_*`, `ICD10_CCSR_CSV`, `OPENAI_API_KEY`).
  Consolidate into one settings module with defaults and validation, and ship a
  `.env.example` — there isn't one, and the AMA turnkey path alone needs four variables.
- **Observability.** No structured logging, no request ids, no exception middleware. An
  unhandled error returns a bare 500. Add all three; keep PHI out of log lines by design,
  not by accident.

---

## 5. Priority 5 — access control

Even for a pilot this is below the bar:

- `CORSMiddleware(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])`
  (`api.py:36`).
- **No authentication of any kind.**
- **Coder identity is a query parameter.** `/api/encounters/{id}/coder-view?user_id=…` and
  the `coder_id` field in the feedback POST body are caller-supplied. Any caller can act
  as any coder, and — because `PreferenceStore` derives the adaptive layer from that same
  log — poison another coder's learned preferences.
- No rate limiting and no request-size limit; `POST /api/billing` accepts unbounded text.

Work: session or token auth, coder identity taken **from the session** and never from the
query string, CORS restricted to configured origins, request size caps, basic rate
limiting, and an access-log of view/decide events (the start of the HIPAA audit posture).

---

## 6. Foundational scope gaps (confirm with John before building)

These are product-shaped, so check before investing — but they are engine work, not UI work:

- **No CPT procedure proposals.** `CodeKind.PROCEDURE` exists and the NCCI/HCPCS
  infrastructure is loaded, but `agents/coding.py` proposes only E/M + ICD-10.
  Procedure-heavy specialties hit this on day one.
- **No modifier proposals at all.** `CodeKind.MODIFIER` is defined and the orchestrator
  collects modifiers into the worksheet (`orchestrator.py:90`), but **no agent ever emits
  one**. That field is always empty. Modifier 25 is already showing up in the feedback
  taxonomy as something coders add by hand.
- **No claim export.** There is no 837P or CSV output, so a coder can't act on the result
  outside the tool. `docs/CAC_DATA_SOURCES.md` §16 already surveys the parser options.
- **CPT descriptors are 0** without an AMA license. The turnkey auto-fetch path is built
  and documented (`cosmaneura_billing/README.md`); it just needs credentials.

---

## 7. Housekeeping

- **Repo root is still the 2023 prototype.** `feature1`–`feature5`, `BioGPT-Large-*.py`,
  and a root `README.md` that is the FluencyMed pitch deck rather than the tool's docs.
  Credentials were already cleaned (all keys now read from env — verified today), but the
  scripts are dead weight. Decide: archive under `prototype/`, or delete.
- **~30 MB of data files committed to git**: `DXCCSR_v2023-1.csv` (4.8 MB) plus its
  `.feather`, `iCliniq.feather` (9.9 MB), `med_chat_QA_data.csv` (9.9 MB — byte-identical
  size to `iCliniq.feather`, likely a duplicate), `combined.feather`, `GenMedGPT-5k.feather`,
  and the 1.9 MB pitch-deck PDF. Move to LFS or out of the repo.
- **CI gaps** (`.github/workflows/ci.yml`): no Python linting (no ruff/mypy config), no
  frontend lint or tests (no eslint/prettier), no coverage gate, and no contract test
  binding the API's response shapes to what the frontend consumes. Add at least the
  contract test — it's what keeps §1's changes from silently breaking the UI.
- pytest emits a `PytestRemovedIn10Warning` for class-scoped fixtures defined as instance
  methods in `tests/test_bundled_real_data.py`. Clean it up.

---

## 8. How to run it

```bash
cd /home/user/Cosmas-CAB-Tool/cosmaneura_billing
pip install -e ".[dev]"
python -m pytest              # 156 tests, ~expect all green
python prove_no_upcode.py     # guardrail invariant proof — must stay green
python run_demo.py            # offline end-to-end

# two-terminal dev setup
uvicorn cosmaneura_billing.api:app --port 8000     # ~14s cold start on first request
cd frontend && npm install && npm run dev          # http://localhost:5173
```

**Non-negotiable invariant:** the compliance guardrail can only keep, downgrade, or remove
a code — never inflate one. `prove_no_upcode.py` is the proof and runs in CI. Nothing in
this handoff is worth breaking it for.

---

## 9. Suggested order

1. §1 offline honesty — unblocks every demo and makes incoming user feedback trustworthy.
2. §4 config + Dockerfile + warm start — makes everything after it deployable.
3. §5 auth + CORS — smallest change with the largest risk reduction.
4. §3 Postgres behind the storage interface.
5. §2 real-inference hardening, then §6 after confirming scope with John.

Land these as separate, reviewable commits on
`claude/platform-handoff-codex-obnlqb`. Keep `cosmaneura_billing/frontend/src/` diffs
minimal and comment any UI seam you introduce so John's UI/UX pass can absorb it cleanly.

---

*Prior context: `STATUS_REVIEW_2026-08-06.md` (readiness review written for the
medical-coder meeting), `cosmaneura_billing/DESIGN.md` (architecture and compliance
stance), `cosmaneura_billing/VALIDATION_PROTOCOL.md` (the accuracy study this all
feeds).*
