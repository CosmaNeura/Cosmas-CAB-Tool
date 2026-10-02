# Dormant SMS dispatch integration

Tier 2: outreach authorization, tenant isolation and durable send evidence.
Base `95809e6a38a970490c28fc9a33be5242f7e770b8`; stack on SMS storage.
Port final `8d0caca` library and complete original tests; keep SMS defaults.
API, worker, deployed grants and configuration remain unmounted/unchanged.
Preserve consent/direct-contract/current-evidence gates, touch caps, signed
route/SID callbacks, cancellation locks, permanent starts and no uncertain retry.
Existing001–013 +015–018 schema suffices; no migration or external dependency.
First PG run exposed ~69ms cross-clock failure; default eligibility now uses
the same transaction's DB clock after its case lock. Explicit clocks remain.
Test complete suites, recipient/default-clock boundaries and anti-inflation
with real PG and fake vendors; grant final worker permissions only to its test role.
Retain initial failures, corrected evidence and source pins in sms-dispatch-integration.
No model/vendor/cloud calls. Recovery reverts the unmounted library, retaining
all evidence/starts. Keep shared PG for same-chat consumers. Budget≤2,000/50.
Commit/push a draft stack; independent review binds the final substantive push.
