# Direct-contract roster file format — v1

The roster the F3 direct-contract gate reads to answer *"is this patient on the
practice's direct-contract list?"* Billing a direct-contract patient a self-pay
fee is a broken promise (Grace, 8/6), so the gate **fails closed**: a missing,
unreadable, or unparseable roster makes every answer `known=false`, which blocks
charging/dunning and routes insurance chase to human review.

Loader: `cosmaneura_billing.direct_contract.DirectContractList`. Path:
`COSMANEURA_DIRECT_CONTRACT_LIST` (default `direct_contract_patients.csv`).

> **Never commit a real roster.** It is patient data. `.env`-configured paths
> point at a practice-provided file outside source control. The samples in this
> directory are synthetic (`SYNTH-DC-*`) and for tests/docs only.

## Accepted formats

One patient reference per entry. References are normalized by **strip + casefold**
before comparison, so `PAT-1`, `pat-1`, and ` Pat-1 ` are the same patient.

### CSV

A header row with a `patient_ref` column (extra columns are ignored):

```csv
patient_ref,note
SYNTH-DC-0001,synthetic
SYNTH-DC-0002,synthetic
```

A CSV without a `patient_ref` column is **unparseable** → fail closed.

### JSON

A list of strings, a list of `{"patient_ref": "..."}` objects, or an object with
a `patients` array:

```json
["SYNTH-DC-0001", "SYNTH-DC-0002"]
```

```json
{"patients": [{"patient_ref": "SYNTH-DC-0001"}, {"patient_ref": "SYNTH-DC-0002"}]}
```

An invalid row (a non-string, or an object without a string `patient_ref`) makes
the whole file unparseable → fail closed. A partially-parsed allow-list is
exactly the quiet wrong answer the gate refuses to produce.

## Semantics (do not change)

- **Charging/dunning (F5):** a match is a hard stop; an unknown answer also
  blocks. `guard_patient_charge` raises rather than returning a boolean a caller
  could forget to check.
- **Insurance chase (F9a):** a match or unknown answer routes to `review`
  (named human), never silently suppressed off the worklist.
- **Fingerprint:** the roster answer carries a content-sensitive
  `gate_fingerprint`; a human review release binds to that exact fingerprint, so
  a release cannot carry across a roster content change. With
  `COSMANEURA_CHASE_GATE_HMAC_KEY` set, references are covered only by a
  secret-keyed digest, never a reversible low-entropy hash.

## Maintenance and refresh

Drop an updated file at the configured path; the loader reloads when the file's
size/mtime change. Keep a `version` label with the deployment record so an
answer's provenance is auditable.

## Open decision (synthesis §8 Q5)

Where the authoritative roster lives and who maintains it is still open. Until it
is decided, this versioned local file is the source, with fail-closed semantics.
When the real source is chosen (an Athena read, a practice API, or a maintained
form export), it plugs in as a `DirectContractSource` via the seam in
`direct_contract_sources.py` (`CallableDirectContractSource`) — **the gate
semantics do not change**, only where the refs come from.
