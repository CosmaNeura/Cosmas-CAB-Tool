"""Compose an explicit coder review with the exact current header revision.

This does not adjudicate codes, attest the clinical note, or submit a claim.
The API must first verify the server-signed complete recommendation manifest.
"""
from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from . import billing_header as h
from .coder_feedback import (
    EncounterFeedback, FeedbackConflict, feedback_payload_digest, validate_feedback,
    validate_feedback_shape,
)


def header_proposals(header, revision):
    original = {row["id"]: h.code_fields(row) for row in header["suggestions"]}
    current = {row["id"]: row for row in h.create_draft(header, revision)["codes"]}
    return [{"id": key, "original": original.get(key), "proposed": current.get(key)}
            for key in dict.fromkeys((*original, *current))
            if original.get(key) != current.get(key)]


def accepted_diagnosis_order(header, revision, decisions, outcomes):
    """Resolve only the explicitly accepted human sequence, never inferred order.

    A rejected primary cannot be replaced by a surviving secondary. Every final
    diagnosis must be represented; changed rows also need an accepted proposal.
    """
    order = h.diagnosis_order(h.create_draft(header, revision))
    choice = outcomes.get(h.DIAGNOSIS_ORDER_DECISION)
    if not choice or choice["decision"] != "accept":
        return []
    final = {d.final_code for d in decisions if d.kind == "ICD10"
             and d.action in ("accept", "modify", "add")}
    h.require(bool(order) and order[0]["code"] in final)
    h.require(final <= {row["code"] for row in order})
    retained = [row for row in order if row["code"] in final]
    proposals = {row["id"] for row in header_proposals(header, revision)}
    h.require(all(row["id"] not in proposals
                  or outcomes.get(row["id"], {}).get("decision") == "accept" for row in retained))
    return retained


def review_manifest(feedback):
    import hashlib
    import json
    rows = sorted([[d.kind, d.engine_code] for d in feedback.decisions if d.action != "add"])
    return len(rows), hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def reconcile_baseline(header, feedback, decisions):
    """Account for originals absent from the current, separately signed manifest.

    Retaining an earlier suggestion requires an ordinary human feedback outcome;
    reconciliation never adds an engine recommendation or changes a header.
    """
    current = {(d.kind, d.engine_code) for d in feedback.decisions if d.action != "add"}
    missing = {row["id"]: row for row in header["suggestions"]
               if (row["kind"], row["code"]) not in current}
    final = {(d.kind, d.final_code) for d in feedback.decisions if d.action != "reject"}
    h.sequence(decisions, 20)
    outcomes = {}
    for decision in decisions:
        h.closed(decision, ("id", "decision", "reason"))
        h.identifier(decision["id"])
        h.require(decision["id"] in missing and decision["id"] not in outcomes)
        h.require(decision["decision"] in ("retain", "reject"))
        h.text(decision["reason"], 1000)
        row = missing[decision["id"]]
        h.require(((row["kind"], row["code"]) in final) == (decision["decision"] == "retain"))
        outcomes[decision["id"]] = h.detached(decision)
    h.require(set(outcomes) == set(missing))
    return [outcomes[key] for key in sorted(outcomes)]


def _validate_context(feedback, context, acknowledged):
    h.require(acknowledged is True)
    count, digest = review_manifest(feedback)
    h.require(type(context) is dict and context.get("recommendationCount") == count
              and context.get("recommendationDigest") == digest
              and context.get("engineVersion") == feedback.engine_version)


def validate_signoff(header, revision, feedback, context, decisions, acknowledged,
                     baseline_decisions=()):
    """Human proposal decisions must agree with the recorded feedback outcomes."""
    try:
        validate_feedback(feedback)
        from .billing_header_mapping import validate_mappings
        validate_mappings(header, h.create_draft(header, revision)["codes"])
        _validate_context(feedback, context, acknowledged)
        proposals = {row["id"]: row for row in header_proposals(header, revision)}
        h.sequence(decisions, 41)
        expected = set(proposals)
        if h.diagnosis_order(h.create_draft(header, revision)):
            expected.add(h.DIAGNOSIS_ORDER_DECISION)
        outcomes = {}
        final = {(d.kind, d.final_code) for d in feedback.decisions if d.action != "reject"}
        for decision in decisions:
            h.closed(decision, ("id", "decision", "reason"))
            h.require(decision["id"] in expected and decision["id"] not in outcomes)
            h.require(decision["decision"] in ("accept", "reject"))
            h.text(decision["reason"], 1000)
            if decision["id"] == h.DIAGNOSIS_ORDER_DECISION:
                outcomes[decision["id"]] = h.detached(decision)
                continue
            proposal = proposals[decision["id"]]
            before, after = proposal["original"], proposal["proposed"]
            before_key = (before["kind"], before["code"]) if before else None
            after_key = (after["kind"], after["code"]) if after else None
            accepted = decision["decision"] == "accept"
            if accepted:
                h.require((after_key in final) if after else before_key not in final)
                if before_key and before_key != after_key:
                    h.require(before_key not in final)
            elif after_key and after_key != before_key:
                h.require(after_key not in final)
            elif after is None:
                h.require(before_key in final)
            outcomes[decision["id"]] = h.detached(decision)
        h.require(set(outcomes) == expected)
        accepted_diagnosis_order(header, revision, feedback.decisions, outcomes)
        baseline = reconcile_baseline(header, feedback, list(baseline_decisions)
                                      if baseline_decisions == () else baseline_decisions)
        return [outcomes[key] for key in sorted(outcomes)], baseline
    except (h.BillingHeaderError, ValueError, TypeError, AttributeError, KeyError):
        raise h.BillingHeaderError("invalid_signoff") from None


class SignoffStoreMixin:
    def signoff(self, encounter_id, *, header_id, expected_source, expected_revision,
                feedback, verified_context, human_proposal_decisions, acknowledged,
                actor, feedback_store, audit_completion=None, baseline_decisions=()):
        h.require(isinstance(actor, h.HeaderActor) and actor.role in ("Coder", "Admin"), "not_authorized")
        h.require(isinstance(feedback, EncounterFeedback), "invalid_signoff")
        try:
            validate_feedback_shape(feedback)
            _validate_context(feedback, verified_context, acknowledged)
        except (ValueError, TypeError, AttributeError):
            raise h.BillingHeaderError("invalid_signoff") from None
        actor = h.HeaderActor(**asdict(actor))
        h.uuid(header_id)
        h.uuid(feedback.review_id)
        h.integer(expected_revision, 0, h.MAX_REVISIONS)
        h.require(feedback.encounter_id == encounter_id
                  and type(feedback.encounter_version) is int, "invalid_signoff")
        if self.durable:
            h.require((feedback.coder_tenant_id, feedback.coder_id) == (actor.tenant_id, actor.user_id), "not_authorized")
        intent_payload = {"headerId": header_id, "revision": expected_revision,
                           "source": expected_source, "feedback": feedback_payload_digest(feedback),
                           "proposals": human_proposal_decisions, "acknowledged": acknowledged,
                           "actor": asdict(actor)}
        # Preserve exact replays of pre-reconciliation, fully covered receipts.
        if baseline_decisions:
            intent_payload["baseline"] = baseline_decisions
        intent = h.digest(intent_payload)
        with self._unit(encounter_id, actor, feedback.review_id) as unit:
            h.check_source(unit.stored, expected_source)
            h.require(unit.stored.version == feedback.encounter_version, "source_changed")
            record = unit.header(header_id)
            h.require(record is not None, "header_not_found")
            current = self._view(unit, record)
            h.require(current["revisionNumber"] == expected_revision, "revision_conflict")
            h.require(h.canonical(record["header"]["expectedSource"]) == h.canonical(expected_source), "source_changed")
            prior = unit.signoff_receipt(review_id=feedback.review_id)
            if prior:
                h.require(prior["requestDigest"] == intent, "operation_conflict")
                created = False
            else:
                h.require(current["signoff"] is None, "review_conflict")
                decisions, baseline = validate_signoff(record["header"], current["revision"], feedback,
                    verified_context, human_proposal_decisions, acknowledged, baseline_decisions)
                # A historical feedback-only receipt cannot be promoted later.
                h.require(unit.append_feedback(feedback, feedback_store), "review_conflict")
                receipt = {"id": str(uuid4()), "reviewId": feedback.review_id,
                    "headerId": header_id, "headerRevision": expected_revision,
                    "expectedSource": h.detached(expected_source), "actor": actor.to_api(),
                    "createdAt": self.clock(), "status": "recorded",
                    "recommendationDigest": verified_context["recommendationDigest"],
                    "humanProposalDecisions": decisions,
                    "baselineDecisions": baseline,
                    "codeDecisions": [asdict(d) for d in feedback.decisions],
                    "reviewerNote": feedback.overall_note}
                h.timestamp(receipt["createdAt"])
                h.require(h.timestamp(receipt["createdAt"]) >= h.timestamp(
                    current["revision"]["createdAt"] if current["revision"] else record["header"]["generatedAt"]), "invalid_signoff")
                unit.insert_signoff(receipt, intent, actor, feedback, audit_completion)
                created = True
            self._finish(unit, record, actor, expected_source, audit_completion, "billing_header.signoff")
            return self._view(unit, record), created


class MemoryHeaderFeedbackAdapter:
    """Explicit synthetic demo only: feedback and header receipt share one lock.

    Entire state is transient memory. No JSONL write or crash-durability claim.
    The host injects this same adapter into its feedback/preference paths.
    """
    def __init__(self, header_store):
        h.require(getattr(header_store, "durable", True) is False, "signoff_unavailable")
        self.header_store = header_store

    @staticmethod
    def _append(data, feedback):
        validate_feedback_shape(feedback)
        key = feedback.review_id
        prior = data["feedback"].get(key)
        if prior:
            if feedback_payload_digest(EncounterFeedback.from_dict(prior)) != feedback_payload_digest(feedback):
                raise FeedbackConflict("review ID conflicts with an existing review")
            return False
        validate_feedback(feedback)
        data["feedback"][key] = h.detached(feedback.to_dict())
        return True

    def append(self, feedback):
        with self.header_store._lock:
            data = h.detached(self.header_store._data)
            created = self._append(data, feedback)
            self.header_store._data = data
            return created

    def exact_replay(self, feedback):
        with self.header_store._lock:
            data = h.detached(self.header_store._data)
            return not self._append(data, feedback)

    def load(self):
        with self.header_store._lock:
            return [EncounterFeedback.from_dict(h.detached(row))
                    for row in self.header_store._data["feedback"].values()]

    def revision(self):
        return ("synthetic-header-memory", self.count())

    def count(self):
        with self.header_store._lock:
            return len(self.header_store._data["feedback"])

    def healthcheck(self):
        return None
