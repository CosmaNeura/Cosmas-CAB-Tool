# Athena connection ownership across Cosmas products

Status: reviewed 2026-08-23. This boundary is intentionally credential-free.

This review compared the billing repository at `0bebce4` with the actual
GitHub `main` of `CosmaNeura/cosmaneura-sovereign` at `c570560`. The local
`cosmaneura-sovereign-main` checkout was behind its remote and must not be used
as the release reference.

## Decision

There are two different Athena workloads. They must not share an OAuth client,
client secret, token cache, runtime identity, or Key Vault grant.

| Boundary | Cosmas provider assistant (`cosmaneura-sovereign`) | Cosmas billing/Card Chase (this repository) |
|---|---|---|
| Product responsibility | Interactive provider lab review and lab-grounded chat | Scheduled import of encounters, documentation, coverage, claims, and outcomes used by billing and Card Chase |
| Athena API family | Certified FHIR R4 `DiagnosticReport` and `Observation` only | Certified reads where sufficient; otherwise reviewed GET-only AthenaOne operations |
| Expected Preview scopes | `system/DiagnosticReport.read` and `system/Observation.read` | Current portal candidate is `athena/service/Athenanet.MDP.*`; it remains unusable until the least-privilege exception and literal GET allowlist are approved |
| Invocation | One provider request bound to the host-open synthetic record and a short-lived record grant | Manual, no-ingress batch job with a durable per-feed checkpoint |
| Runtime variables | `ATHENA_*` in the Sovereign provider mount | `COSMANEURA_ATHENA_*` in the dedicated billing import job |
| Secret boundary | Sovereign provider-only vault and runtime identity | Billing vault secret granted only to the import-job identity |
| Persistence | Read-through response for the governed provider turn; no importer/checkpoint | Atomic normalized import, source-version receipts, and checkpoints |
| Patient lookup | Host resolves the open record; name search is forbidden | Adapter resolves only contract-approved synthetic records; request input never selects the target practice |
| Production state | Feature-flagged, first platform activation keeps labs off | Live mode and endpoint allowlist remain disabled/empty |

The shared import/checkpoint layer in this repository is source-agnostic so
future MCD insurance feeds can reuse it. That does **not** move lab ownership
into the billing app. If lab persistence is later needed, Sovereign should
publish its normalized lab contract through a reviewed platform-owned internal
boundary; it must not connect directly to the billing database or reuse the
billing importer credential.

## What already exists in Sovereign

The reviewed Sovereign commit already contains:

- a vendor-neutral `LabSource` adapter interface;
- an athenahealth FHIR R4 implementation over `DiagnosticReport` and
  `Observation`;
- fixed Preview practice `195900`, bounded date windows, bounded pagination,
  same-origin next-link validation, and entered-in-error filtering;
- the governed `labs.read` request/response contract and deterministic mock
  results;
- provider-only authorization tied to the host-open record;
- BAA-gated model use, value-free error handling, and before/after audit;
- the lab-review agent and provider panel/dashboard surface; and
- Key Vault and deployment gates that keep labs out of the public mount and
  disabled during the first activation.

Rebuilding lab models, an Athena lab client, or a fourth billing tab here would
create two authorities and guaranteed rework. This repository therefore owns
no lab endpoint, lab UI, or FHIR lab credential.

## Remaining deconfliction gate

The repositories cannot prove from source alone whether an operator has placed
the same Athena client ID in both vaults. Before either live path is enabled,
record a credential-free control result showing that the client IDs differ.
Do not print or persist either client secret or access token.

If only one Preview OAuth application currently exists, do not broaden or
repurpose the credential already used by the historically verified Sovereign
lab smoke. Create a second application for the other workload. Names are less
important than distinct client IDs and non-overlapping grants; recommended
names are:

- `Cosmas Provider Labs Preview` — FHIR lab scopes only; and
- `Cosmas Billing Import Preview` — billing/coverage read operations only.

The existing portal application named `Cosmaneura CAB Pilot` should be mapped
to one of those responsibilities only after its current client ID and exact
scope list are checked. Never infer ownership from the display name.

## Operator checklist for the next Athena session

1. Open each Preview application and record, outside source control, its client
   ID, owner, expiration, environment, and exact scopes. Do not open or copy a
   secret until the intended workload is identified.
2. Confirm the two client IDs are different. If only one exists, create the
   missing least-privilege application rather than adding both scope families
   to the existing one.
3. Keep the Sovereign app limited to the two documented FHIR lab scopes. Do not
   add patient search, document binary, proprietary AthenaOne, or write scopes.
4. For billing, ask Athena to provide a vendor-enforced read-only AthenaOne
   grant. If unavailable, obtain explicit approval for the code-enforced
   GET-only exception before retrieving that client's secret.
5. Store each secret in its product's own vault. Prove the Sovereign public
   mount and the billing API/worker identities cannot read either credential.
6. Run each product's synthetic Preview acceptance separately. A successful
   lab read does not validate billing imports, and a successful billing import
   does not authorize provider lab access.

## No-conflict conclusion

At the reviewed commits there is no code, route, environment-variable, Azure
resource-name, or database ownership collision. Both products intentionally
share only Athena's Preview origin and public synthetic Practice `195900`.
The one unresolved operational risk is accidental reuse of the same OAuth
client through external secret stores; the checklist above is the required
gate. No existing Sovereign lab code needs to be copied or rewritten for the
billing pilot.
