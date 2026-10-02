# Provider completed-note receiver integration

Tier 2. Base: reviewed PR78 `a7e1e0d9f6b95ef197d44244b24e80f5a9adfd9c`.

Outcome: CAB receives an exact completed-note v1 envelope from an authorized
Provider/Admin after an operator has registered the exact source connection and
operation. Intake is disabled by default. A receipt starts no inference.

Port the final `8d0caca` receiver/domain/store/admission/operator CLI and complete
receiver suites. Preserve actor/practice binding, source CAS, immutable original
text and receipt replay, completion as provider attestation, and atomic audit.
Only the API runtime gets the guarded writer and read-only registry privileges;
the private019 bypass and worker access stay denied. Registrations are owner-only.

Canonical024 adds the optional calendar service date without changing omitted-date
request identities. Apply all 18 sparse rungs:001–009,011,013,014,019–024. Existing
canonical SQL stays byte-identical. Retain the020 admission wrapper and its ACLs.
Document hashing and verbatim evidence accept exact CRLF; metadata still rejects
carriage returns. Existing sourceVisitContext helper needs no modification.

Controls: both COSMANEURA_PROVIDER_HANDOFF_ENABLED and
COSMANEURA_BILLING_HEADER_ENABLED must be true; receiver registry starts empty.
Authenticated replicas require the existing stable review signer. Current Athena
model-policy gates are unchanged. Provider Azure/secondary/TA4H/shadow admission
remains unavailable without the separate025 source reader and proof integration.
Only local synthetic fixture/offline extraction proves the full header workflow.
No provider-host project, clinical rule, frontend, vendor, credential or deployed
configuration changes belong here.

Verification: candidate import-origin checks; full original receiver/admission
and completed-note client suites; actual restricted-role PostgreSQL on a new
owned loopback database after the locked runner verifies all checksums/replay;
optional-date and no-new-model authority controls; guardrail fuzz and invariant
proof. Retain any initial failures and focused repairs. No external transport.

Recovery: leave additive schema/history intact, disable receiver intake and
roll back app code. Previously stored source/header history remains protected.
Before merge: publish exact source/evidence hashes and obtain independent review
of the final pushed commit. Provider-host button integration remains a follow-on.
