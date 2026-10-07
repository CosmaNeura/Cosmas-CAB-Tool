# Core bridge async deployment inputs — Tier 2

Base: merged `b94708fb8e8ae52e68e8e316259c3b83bd79271e`. This outcome exposes
the existing async/continuation receiver settings in the parent and dedicated
Core Bicep templates. Both default false; continuation requires async, and either
requires explicit Core provisioning. Inference, source admission, identity,
ingress, staff API and communications authority remain separate inputs.

Async selects a bounded 1 CPU / 2 GiB candidate; disabled retains 0.5 CPU / 1 GiB.
This is a resource declaration, not capacity acceptance. Image05 reached 95% of
2 GiB with one child and did not prove terminal completion. Require the reviewed
compact-NCCI image06 installed-image two-worker and expiry proof before rollout.

Rollout uses the existing receiver-first plans: reconcile the target image,
configuration and ledger; apply canonical CAB 028 with the locked owner runner;
grant only `EXECUTE ON FUNCTION public.cosmaneura_header_generation(jsonb)` to
the verified dedicated Core runtime. Check its exact current Core connection,
practice binding and unprivileged login first. Never run the general API
provisioner on this login or grant direct generation-table access. Existing
accounts are not changed by these templates. New Core registration already adds
that capability only when its async setting is true.

Deploy CAB with both options off, then enable async after the grant/readiness and
capacity checks. Apply Core's async/continuation migrations and enable its
matching client settings only after the receiver supports them. Continuation
requires its separate explicit provider consent and continuous authorization.
Workload/source/model policies, executed service/data agreements and any hosted
activation remain separately approved; no flag here supplies those approvals.

Recovery disables new Core client requests before replacing the receiver with
both options off. Allow existing bounded work to finish or stop its owned
children through normal shutdown. Preserve 028, claims, unknown outcomes,
headers and audits; do not infer retry permission or drop expand structures.

Validation is local Bicep compilation and focused compiled-template checks for
defaults, parent/child rejection paths, exact environment wiring, conditional
resources and unchanged authority boundaries. No schema, database, credential,
cloud, vendor, model or deployment action is part of this outcome. Independent
review binds the final local commit before publication.
