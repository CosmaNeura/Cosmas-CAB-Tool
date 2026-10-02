# Source-bound human diagnosis mapping

Risk: Tier 2 (evidence-linked billing proposals and durable review).
Base: `0b456328f5473521c5bd06bb17f8351e40a8cad5`.
Source: selected final `8d0caca` behavior originating in `055e9de`.

## One outcome

A provider or coder can explicitly browse the current local ICD-10-CM catalog
for an evidence-backed held diagnosis and save a chosen code as an attributed
human proposal. Catalog pages use stable code order and expose every matching
result through bounded pagination. Catalog matches are terminology options,
not machine code suggestions or coverage decisions.

The proposal binds to its original candidate, exact evidence references and
catalog snapshot. Source/revision checks, active permissions, current billability
and audit remain required. Coder code acceptance, mapping acceptance and explicit
primary/order confirmation remain distinct. Original notes, machine holds,
primary intent and coverage are never changed. Exact historical receipt replay
remains available after catalog changes; new work revalidates the catalog.

## Bounded integration

Reuse header v1 JSON revisions and existing signoff; no migration or settings
change. Preserve C8 validity/order rules and both parent refresh/retry repairs.
Port focused source-only mapping fixtures, not the old browser/demo host or
MDM fixtures. Keep the fixed twelve-note training package unchanged. No full
MDM wiring, external source, vendor/model request, workflow or deployment work.

## Verification and recovery

Run candidate-origin focused mapping/ordering/signoff and frontend contracts,
then one isolated cached build. One owned local sparse 001–009+011+013 PG
rehearsal covers mapped save, replica reload, coder acceptance and export,
including actor/audit/source/CAS and historical replay. Do not repeat the
foundation matrix. Freeze exact sources/evidence for independent review; keep
under 2,000 changed lines and 50 files including review.

Disable the header workflow before code rollback; retain all append-only rows.
Older readers must not be used to confirm this mapping contract. Human catalog
selection does not establish clinical applicability or authorize an EHR/claim write.

## Observed local checks

129 inherited header/order/replay checks and 12 mapping checks passed. The first
mapping setup run exposed the older mock's nonempty-problem support heuristic;
only the authored test extraction was corrected to include the documented
migraine problem name without an E/M evidence quote. E/M remains held.
47 Node checks, seven actual component render assertions, an isolated cached
build, and one restricted PG16/pgvector mapping workflow passed. PG proved
actor/revision/audit persistence across replicas, coder order/signoff/export,
catalog-unavailable exact replay and new-edit rollback; its container was removed.
No model/vendor request or twelve-note replay was performed.

Checks ran on parent `6498d95` plus these C9 implementation/test bytes. The
parent advance retained all 18 C9 hashes; only this plan's base/scope note was
updated afterward. Inherited changes are disjoint dormant-data and test repairs.
