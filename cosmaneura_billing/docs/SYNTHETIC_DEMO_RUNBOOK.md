# Cosmas CAB deterministic synthetic demo

This runbook demonstrates provider billing review, affirmative coder review,
and manual Card Chase work without real patient data or an external write. It
is a product demonstration, not evidence that the real-PHI pilot is approved.

## Safety boundary

- All encounter and contact records are wholly synthetic.
- The server supplies identity, persona, capabilities, provenance, and
  read-only status. The browser does not assert them.
- The offline extractor is deterministic and knows only the local
  `ENC-100237` teaching case. Other synthetic encounters degrade visibly rather
  than substituting a canned result.
- The service binds only to `127.0.0.1` and accepts only the API routes needed
  for this walkthrough.
- Athena, EHR writes, claim transmission, scheduler execution, patient
  messaging, cloud models, telemetry export, and preference learning are off.
- The four Card Chase tasks use fictional contact data and `record_only`
  transport. A staff action records local state; it contacts nobody.
- Restarting the command creates a clean temporary state. It never modifies the
  digest-pinned packaged seed files.

The allowed review routes include rationale, evidence, and action-note fields.
Use only the supplied synthetic material and never type or paste real patient
information. Loopback binding and temporary storage reduce exposure; they are
not a substitute for operator discipline.

## Start and stop

Complete the one-time dependency setup from `cosmaneura_billing/`:

```bash
python -m pip install -e ".[dev]"
cd frontend && npm ci && cd ..
```

Then run the demo:

```bash
python -m cosmaneura_billing.local_demo
```

Wait for the `READY` message, then open the exact loopback URL it prints. Do
not replace `127.0.0.1` with a LAN address. Press `Ctrl-C` once to stop safely.
Run the same command again for a deterministic reset.

The default is the synthetic Administrator persona. For a focused role check,
restart with exactly one local-only persona:

```bash
python -m cosmaneura_billing.local_demo --persona provider
python -m cosmaneura_billing.local_demo --persona coder
python -m cosmaneura_billing.local_demo --persona billing-staff
```

These switches exist only in this development launcher. They do not alter or
bypass deployed Entra authentication or PostgreSQL membership enforcement.

## Five-to-seven-minute walkthrough

1. **Establish the boundary (30 seconds).** Point out the persistent synthetic
   safety banner, connected status, and server-derived Administrator persona.
   State that nothing on this screen submits a claim or writes to Athena.
2. **Open provider work (45 seconds).** In Provider, choose local-only
   `ENC-100237`. Explain that it is a strong teaching fixture and is deliberately
   separate from the immutable deployable release-candidate pack. Select
   **Run billing review**.
3. **Review evidence (60 seconds).** Compare the submitted 99213 with the
   evidence-supported 99214. Expand the MDM facts and source evidence. Show an
   evidence-linked suggestion and the held or removed candidates with their
   reasons. Confirm the provenance panel says the clinical note was not
   modified.
4. **Demonstrate human handoff (30 seconds).** Copy one code, then copy the
   suggested codes or prepared Athena-friendly block. Read the confirmation:
   copied on this device, not sent.
5. **Show completeness fail-closed behavior (45 seconds).** Choose
   `SYN-RC-0003`. Its telehealth documentation lacks the expected duration,
   patient location, provider location, and consent elements. Show that the
   deterministic completeness result blocks a false ready state. Its offline
   inference result is intentionally degraded; no code is invented.
6. **Perform coder review (90 seconds).** Return to `ENC-100237`, open Coder
   review, and explicitly accept, edit, or reject every recommendation. Show
   that each row begins unreviewed. Demonstrate that an edit requires a
   controlled reason and rationale, and a human-added code additionally
   requires an `Evidence:` excerpt. Save the review and show the
   `prepared_not_transmitted` receipt.
7. **Prove retry semantics (30 seconds).** Select **Verify exact replay**. The
   same review ID and payload return the existing result rather than creating a
   duplicate. Explain that a changed payload under that ID fails with a 409 and
   the completed review remains preserved.
8. **Open Card Chase (60 seconds).** Show the four preloaded, prioritized
   synthetic cases. Open the direct-contract case and point out that it is held
   for human review. On an ordinary case, record one manual staff action. If
   demonstrating a simulated lost response in browser acceptance, use **Retry
   the same action safely** and show the idempotent result.
9. **Close on responsibility (30 seconds).** Reconfirm that no patient message,
   EHR/Athena write, payer submission, or claim transmission occurred. End on
   evidence provenance, immutable encounter versions, stable human-review IDs,
   and human accountability.

## Acceptance checks

- The first useful screen states what the reviewer should do and displays all
  five safety statements.
- The role is visible and matches the server session; direct URLs cannot grant
  an unavailable capability.
- Every rendered code has evidence. A no-evidence code is suppressed.
- Completeness blockers never render a ready state or attestation.
- Coder recommendations start unreviewed; changed or added codes require the
  appropriate reason and evidence.
- Prepared results consistently say `not transmitted`; no browser-owned claim
  success exists.
- Network or server failures show an error and never load a mock fallback.
- Keyboard focus is visible, controls have accessible names, and the layout is
  usable at desktop and common tablet widths.
- Process output and application logs contain metadata only: no note text,
  contact destination, token, credential, or raw request target.

## Deliberate limitations

The local model produces a rich coding result only for `ENC-100237`; the twelve
digest-verified release-candidate encounters are present for provenance,
queue, and completeness demonstrations and otherwise fail honestly into manual
review. This command does not run a PostgreSQL migration or prove deployed
Entra/RLS behavior. Those remain separate exact-image acceptance activities.
