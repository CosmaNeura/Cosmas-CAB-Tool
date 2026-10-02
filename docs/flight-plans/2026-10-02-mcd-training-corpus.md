# MCD synthetic training source integration

Tier 2. Integration base: `3c3cf9da50f1c9228a929dd34d0305304848dc39`.
Reviewed source: `8d0caca22aaed8001aaeec955c81c3f7ecc11bd4`.

## Outcome and authorization

Package the twelve requested fictional visits, including three fertility visits,
and reuse the existing owner-only protected seed so staff can find the intended
cases in the ordinary authenticated queue. The user authorized this synthetic
training preparation. This slice does not add editable headers, signoff, clinical
qualification repairs, hosted admission, or service activation; those remain
separate integrations. Preserve the current v3 review signer and all workflows.

## Source and implementation contract

- Preserve `SYN-CAB-20260930-01` through `12` and every original VisitBundle field.
  The exact input SHA-256 is
  `a2d334c64b8b0bf6607c3ac70539d105d482329c590d018bfebf5975614e6afc`.
  The original provenance manifest SHA-256 is
  `4d8408bf84a5d7b0a0ee2f7395084718a63b4cba701d0ff4db625d84149a504f`.
- Package only source inputs and concise Synthea/fictional-narrative provenance.
  No original patient tables, source-patient mappings, local paths, expected
  codes, model results or clinician approval are packaged. Completion markers
  are fictional fixture conventions.
- Keep the fixed no-argument loader and seed command from the reviewed source.
  The loader verifies source/metadata digests, exact identity/order and shape.
  No generic path, payload, seed parameter or public import endpoint is added.
- Extract only the existing private prepared-seed helper; preserve all existing
  database owner, practice, migration-008 guard and source immutability checks.
  All twelve insert atomically; exact replay changes nothing and an occupied ID
  with differing source rolls back the entire attempt.
- Preserve reserved `synthetic_demo_release_candidate` provenance and existing
  runtime restrictions. Add only two MCD package-data entries to pyproject.
  Do not modify API, settings, authentication, migrations or workflows.

## Verification and limits

Before edits, the existing seed module and inherited seed test helpers were
byte-identical between main and the original source parent. VisitBundle fields
and migration-008 protections are unchanged. Verify the candidate package path,
exact twelve notes/three fertility IDs, loader and fixed CLI behavior, and shared
seed owner/binding/conflict/replay unit checks under this integration tree.

Reuse explicitly identified prior real PostgreSQL/wheel evidence only where
bytes and prerequisites remain identical. PostgreSQL is stopped; no database
rehearsal is claimed for this candidate. Do not start it absent a material
persistence dependency change. Defer the original header-dependent API test:
this tree does not yet have those modules or the later clinical extraction
contract. No fresh inference, vendor request, browser campaign or broad suite
is needed for this source-only outcome. A final independent reviewer binds the
actual final pushed commit and evidence before merge.

## Recovery and promotion

No new schema or deployed data mutation occurs here. The target owner must
separately review deployment and seeding before running the command. Recovery
stops further seeding/access and preserves already populated sources and review
history; it does not delete rows or weaken their guard. No rollout authorization
is inferred. Keep the change below 2,000 lines and 50 files.
