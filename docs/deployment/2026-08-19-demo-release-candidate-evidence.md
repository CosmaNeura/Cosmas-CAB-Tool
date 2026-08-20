# Demo release candidate — deployment and acceptance evidence

Recorded: 2026-08-19

Scope: synthetic/de-identified Azure pilot only. This record does not authorize
real PHI, patient messaging, a public endpoint, or unattended coding.

## Read this snapshot correctly

Two source identities are intentionally different:

| Boundary | Source identity | Status |
|---|---|---|
| Release-candidate GitHub baseline | `de2b16fd5431fc08b2d3c66e0aa4478f3239de41` | CI passed; not deployed |
| Current live image | `git-1c5d7e7b6c12c260f77e4870e47b1d1aa7fd61a1` | Accepted, then returned to the disabled state |

`1c5d7e7` is the first parent of merge commit `de2b16f`; PR #4 is the other
parent. Consequently, the live environment did not yet contain PR #4 or the
canonical acceptance harness at this record's baseline. The eventual merge SHA
of this release candidate must be added to a post-merge deployment record; do
not describe a repository state as live until its manifest digest is deployed
and accepted.

## Current live state

The final operator audit observed:

- Container Apps revision suffix `--0000008`: `Healthy`, `Running`, and receiving
  100% of traffic.
- External ingress: `false`.
- Azure OpenAI application latch: `false`; no `AZURE_OPENAI_*` variables were
  present on the final revision.
- The TA4H/Language lane is not present in the accepted `1c5d7e7` image. Current
  `main` contains it default-off, but it has not been deployed or accepted.
- Chase scheduler, patient messaging, and patient-messaging operational
  activation: all `false`.
- Chase worker: `Manual`; transport: `record_only`.
- ACR public network access: `Disabled`; admin user: disabled.
- Azure OpenAI account provisioning state: `Succeeded`; local authentication:
  disabled; public network access: disabled; network default action: `Deny`.
- Model deployment: `gpt-5.6-sol`, version `2026-07-09`,
  `DataZoneStandard`, capacity 200, `NoAutoUpgrade`.
- Private endpoint connection: approved for group `account`; private DNS
  resolved only to an RFC1918 address from the approved network.

The resources remain provisioned while application inference is disabled. The
private endpoint/DNS standing charges and any metered use remain governed by the
approved cost plan; the idle model deployment itself has no fixed token charge.

## Synthetic acceptance evidence

The temporary inference-on acceptance revision suffix `--0000007` used only
fixed synthetic inputs. The accepted log sample contained:

- two successful, closed-schema `llm_request` events;
- zero event-schema or domain-allowlist violations; and
- zero matches for the checked prompt, response, and synthetic PHI canaries.

After the latch was returned to `false`, final revision `--0000008` emitted zero
LLM events during the final observation window. No prompt, model response,
credential, endpoint, resource identifier, or raw application log is reproduced
in this evidence record.

GitHub Actions run
[`32244337843`](https://github.com/CosmaNeura/Cosmas-CAB-Tool/actions/runs/32244337843)
completed successfully for the accepted-image source `1c5d7e7`. Post-merge run
[`32317475914`](https://github.com/CosmaNeura/Cosmas-CAB-Tool/actions/runs/32317475914)
completed successfully for the release-candidate baseline `de2b16f`. CI success
demonstrates that repository state; it does not demonstrate deployment.
CodeQL setup run
[`32317672236`](https://github.com/CosmaNeura/Cosmas-CAB-Tool/actions/runs/32317672236)
passed for both configured languages.

`main` protection requires seven strict contexts: `infra`, Python 3.10/3.12
`test`, `frontend`, `container`, `Analyze (python)`, and
`Analyze (javascript-typescript)`. It includes administrators, requires
conversation resolution, and disallows force-pushes and deletions. The
anti-inflation proof runs inside both required Python contexts and invokes both
invariant harnesses.

## Reproducible acceptance contract

The repository now carries the fixed synthetic harness at
`cosmaneura_billing/cosmaneura_billing/azure_openai_acceptance.py`. From a
reviewed private Azure execution environment, temporarily enable only the
synthetic acceptance revision and run:

```bash
python -m cosmaneura_billing.azure_openai_acceptance > /tmp/azure-openai-acceptance.jsonl
python -m cosmaneura_billing.log_scanner /tmp/azure-openai-acceptance.jsonl
```

The harness accepts no prompt or chart input. It verifies private DNS,
managed-identity routing, absence of direct credentials, strict Structured
Outputs, the JSON-object compatibility path, terminal truncation, and a safe
safety-control outcome. Its stdout contract contains only fixed acceptance
check names/status/counts and the existing closed numeric LLM metric events;
tests pin that contract and verify it with the same fail-closed log scanner.

The operator must then restore the Azure OpenAI latch to `false`, confirm no
Azure OpenAI variables remain on the final revision, and verify the final
observation window contains zero LLM events. Never place a captured log,
credential, endpoint, prompt, or response in Git.

## Authorization caveat

The runtime application assignment remains the account-scoped inference role on
the API user-assigned managed identity; migration and worker identities have no
Azure OpenAI configuration. Separately, the signed-in human operator inherits a
subscription-level Foundry User role. That inherited human entitlement is not a
runtime credential and did not change the application result, but it means the
statement “only the API identity can use the account” is not yet true across the
whole subscription. Complete a minimum-necessary entitlement review before any
real-PHI decision.

## Still pending

- Merge the reviewed release candidate, build its commit-derived tag, resolve
  and record the immutable manifest digest, deploy that digest privately with inference, Language,
  scheduler, and messaging latches disabled, and rerun the repository-owned
  synthetic harness.
- Reconfirm private DNS, managed-identity scope, closed log schemas, and the
  final zero-LLM-event disabled state on that exact image.
- Review and minimize the inherited human Foundry User entitlement.
- Record measured p50/p95 input, cached-input, and output tokens before replacing
  the illustrative cost scenarios with a forecast.
- Verify the executed Microsoft agreement/BAA, service-specific data processing,
  retention, abuse-monitoring, and selected US data-zone posture.
- Complete the 40–60 case de-identified/certified-coder evaluation before any
  broader inference-on milestone. TA4H shadow evaluation remains deferred until
  primary Azure OpenAI inference is enabled under a separately approved plan.
- Keep real chart text, real recipients, scheduling, and patient messaging
  blocked until their separate contractual, technical, and operational gates
  are documented and approved.
