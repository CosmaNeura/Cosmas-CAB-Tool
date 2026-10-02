"""Bounded staff review of immutable replies/outcomes; no dispatch authority.

The source view counts every pending record before the display limit. Decisions
never mutate original callbacks, suppressions, contact snapshots or chase state.
"""
import base64
from datetime import datetime
import hashlib
import json
import re

from .postgres import _iso, _uuid
from .store import CommunicationsError, _hash

_HEX = re.compile(r"[0-9a-f]{64}\Z")


def review_ref(kind, account, source_id):
    value = f"operation:{source_id}" if kind == "operation" else f"{kind}:{account}:{source_id}"
    return hashlib.sha256(value.encode()).hexdigest()


def _fail(code, status=409):
    raise CommunicationsError(status, "communications_" + code)


def _page_limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        _fail("invalid_inbox_page", 422)
    return value


def _page_cursor(tag, key):
    return base64.urlsafe_b64encode(json.dumps([1, tag, key], separators=(",", ":")).encode()).decode().rstrip("=")


def _read_cursor(value, tag, *, cases=False):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,700}", value):
            raise ValueError
        version, stored_tag, key = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if type(version) is not int or version != 1 or stored_tag != tag:
            raise ValueError
        if cases:
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", key):
                raise ValueError
        else:
            if type(key) is not list or len(key) != 2 or not isinstance(key[0], str) or not _HEX.fullmatch(key[1]):
                raise ValueError
            stamp = datetime.fromisoformat(key[0])
            if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
                raise ValueError
        return key
    except (ValueError, TypeError, UnicodeError, OverflowError):
        _fail("invalid_inbox_page", 422)


class CommunicationsEventReview:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.store = dispatcher.store

    def counts(self, case_ids, *, db=None):
        if db is None:
            with self.store._db() as connection:
                return self.counts(case_ids, db=connection)
        rows = db.execute("""SELECT case_id,count(*) AS pending,count(*) FILTER (WHERE held) AS held
            FROM chase_communication_review_sources WHERE practice_id=%s AND case_id=ANY(%s::text[])
              AND completed_decision IS NULL GROUP BY case_id""", (self.store.practice_id, case_ids)).fetchall()
        counts = {r["case_id"]: {"pending": r["pending"], "held": r["held"]} for r in rows}
        return {case: counts.get(case, {"pending": 0, "held": 0}) for case in case_ids}

    def read(self, case_id, *, db=None):
        if db is None:
            with self.store._db() as connection:
                return self.read(case_id, db=connection)
        self.store._case(db, case_id)
        counts = self.counts([case_id], db=db)[case_id]
        rows = db.execute("""SELECT * FROM chase_communication_review_sources
            WHERE practice_id=%s AND case_id=%s AND completed_decision IS NULL
            ORDER BY at,source_ref LIMIT 50""", (self.store.practice_id, case_id)).fetchall()
        return {"schemaVersion": "communications-review.v1", "chaseId": case_id,
            "totalPending": counts["pending"], "heldPending": counts["held"],
            "items": [self._item(row) for row in rows], "truncated": counts["pending"] > len(rows)}

    def unassigned_count(self, *, db=None):
        if db is None:
            with self.store._db() as connection:
                return self.unassigned_count(db=connection)
        return db.execute("""SELECT count(*) AS n FROM chase_communication_unassigned_sources
            WHERE practice_id=%s AND review_state='pending'""", (self.store.practice_id,)).fetchone()["n"]

    def unassigned(self, *, state="pending", cursor=None, limit=50):
        if state not in ("pending", "reviewed"):
            _fail("invalid_inbox_page", 422)
        _page_limit(limit, 50)
        tag = "inbox:" + state
        after = _read_cursor(cursor, tag)
        with self.store._db() as db:
            # SQL fragments are fixed alternatives; patient/cursor values stay bound.
            stamp = "at" if state == "pending" else "reviewed_at"
            predicate = "review_state='pending'" if state == "pending" else "review_state<>'pending'"
            rows = db.execute(f"""SELECT * FROM chase_communication_unassigned_sources
                WHERE practice_id=%s AND {predicate}
                AND (%s::timestamptz IS NULL OR ({stamp},source_ref)>(%s::timestamptz,%s))
                ORDER BY {stamp},source_ref LIMIT %s""", (self.store.practice_id,
                    after[0] if after else None, after[0] if after else None, after[1] if after else None, limit+1)).fetchall()
            items = [{"ref": row["source_ref"], "sourceHash": row["source_hash"], "kind": "sms_reply", "channel": "sms",
                "text": row["text"], "at": _iso(row["at"]), "status": row["status"], "disposition": row["disposition"],
                "mediaCount": row["media_count"], "senderHint": "Ending " + row["sender"][-4:],
                "reviewState": row["review_state"], "reviewedAt": _iso(row["reviewed_at"]) if row["reviewed_at"] else None,
                "attributedCaseId": row["attributed_case_id"]} for row in rows[:limit]]
            more = len(rows) > limit
            return {"schemaVersion": "communications-unassigned.v1", "state": state, "pendingCount": self.unassigned_count(db=db),
                "items": items, "hasMore": more,
                "nextCursor": _page_cursor(tag, [_iso(rows[limit-1][stamp]), rows[limit-1]["source_ref"]]) if more else None}

    def _unassigned_source(self, db, event_ref, source_hash):
        if not isinstance(event_ref, str) or not _HEX.fullmatch(event_ref) or not isinstance(source_hash, str) or not _HEX.fullmatch(source_hash):
            _fail("invalid_review_decision", 422)
        row = db.execute("""SELECT * FROM chase_communication_unassigned_sources
            WHERE practice_id=%s AND source_ref=%s AND source_hash=%s AND review_state='pending'""",
            (self.store.practice_id, event_ref, source_hash)).fetchone()
        if row is None:
            _fail("review_source_stale")
        return row

    def _matching_cases(self, db, source, *, after=None, limit=21, case_id=None):
        return db.execute("""SELECT i.chase_id,i.patient_ref,i.state,i.bucket,s.contact_snapshot_id,s.verified_at
            FROM chase_items i
            JOIN LATERAL (SELECT contact_snapshot_id,verified_at FROM chase_contact_snapshots s
                WHERE s.practice_id=i.practice_id AND s.chase_id=i.chase_id
                ORDER BY verified_at DESC,created_at DESC,contact_snapshot_id DESC LIMIT 1) s ON true
            JOIN chase_contact_routes r ON r.practice_id=i.practice_id AND r.contact_snapshot_id=s.contact_snapshot_id
                AND r.channel='sms' AND r.destination=%s
            WHERE i.practice_id=%s AND (%s::text IS NULL OR i.chase_id>%s)
              AND (%s::text IS NULL OR i.chase_id=%s)
              AND NOT EXISTS (SELECT 1 FROM chase_communication_event_reviews d
                WHERE d.practice_id=i.practice_id AND d.case_id=i.chase_id AND d.source_ref=%s
                  AND d.source_hash=%s AND d.decision IN ('handled','leave_unassigned'))
            ORDER BY i.chase_id LIMIT %s""", (source["sender"], self.store.practice_id, after, after,
                case_id, case_id, source["source_ref"], source["source_hash"], limit)).fetchall()

    def unassigned_cases(self, *, event_ref, source_hash, cursor=None, limit=20):
        _page_limit(limit, 20)
        tag = "cases:" + str(event_ref) + ":" + str(source_hash)
        after = _read_cursor(cursor, tag, cases=True)
        with self.store._db() as db:
            source = self._unassigned_source(db, event_ref, source_hash)
            rows = self._matching_cases(db, source, after=after, limit=limit+1)
            return {"schemaVersion": "communications-unassigned-cases.v1", "eventRef": event_ref, "sourceHash": source_hash,
                "items": [{"chaseId": r["chase_id"], "patientLabel": r["patient_ref"][:160], "state": r["state"],
                    "bucket": r["bucket"], "contactSnapshotId": str(r["contact_snapshot_id"]), "verifiedAt": _iso(r["verified_at"])} for r in rows[:limit]],
                "hasMore": len(rows)>limit, "nextCursor": _page_cursor(tag, rows[limit-1]["chase_id"]) if len(rows)>limit else None}

    def decide_unassigned(self, *, action_id, event_ref, source_hash, decision, actor_tenant_id, actor_id,
                          case_id=None, contact_snapshot_id=None):
        action_id = _uuid(action_id)
        if decision not in {"attribute", "leave_unassigned"} or (decision == "attribute") != (case_id is not None and contact_snapshot_id is not None):
            _fail("invalid_review_decision", 422)
        if decision == "leave_unassigned" and (case_id is not None or contact_snapshot_id is not None):
            _fail("invalid_review_decision", 422)
        snapshot = _uuid(contact_snapshot_id) if contact_snapshot_id is not None else None
        if not isinstance(event_ref, str) or not _HEX.fullmatch(event_ref) or not isinstance(source_hash, str) or not _HEX.fullmatch(source_hash):
            _fail("invalid_review_decision", 422)
        with self.store._db() as db:
            tenant, actor = self.store._staff(db, actor_tenant_id, actor_id)
            if case_id is not None:
                self.store._case(db, case_id)
            body_hash = _hash(["unassigned", case_id, event_ref, source_hash, decision, snapshot, tenant, actor])
            prior = db.execute("SELECT * FROM chase_communication_event_reviews WHERE practice_id=%s AND action_id=%s",
                               (self.store.practice_id, action_id)).fetchone()
            if prior:
                if prior["body_hash"] != body_hash:
                    _fail("idempotency_conflict")
            else:
                source = self._unassigned_source(db, event_ref, source_hash)
                if decision == "attribute":
                    # The trigger repeats this after locking the selected case.
                    choices = self._matching_cases(db, source, case_id=case_id, limit=1)
                    if not choices or str(choices[0]["contact_snapshot_id"]) != snapshot:
                        _fail("contact_stale")
                prior = db.execute("""INSERT INTO chase_communication_event_reviews
                    (practice_id,action_id,case_id,source_ref,source_hash,decision,contact_snapshot_id,
                     actor_tenant_id,actor_id,body_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (self.store.practice_id, action_id, case_id, event_ref, source_hash, decision, snapshot, tenant, actor, body_hash)).fetchone()
            self.dispatcher._audit(db, "communications_review_unassigned", case_id)
            return {"schemaVersion": "communications-unassigned-decision.v1", "pendingCount": self.unassigned_count(db=db),
                "receipt": {"actionId": action_id, "eventRef": event_ref, "sourceHash": source_hash,
                    "decision": decision, "chaseId": case_id, "reviewedAt": _iso(prior["created_at"])}}

    @staticmethod
    def _item(row):
        snapshot = row["attributable_snapshot_id"]
        return {"ref": row["source_ref"], "sourceHash": row["source_hash"], "kind": row["kind"],
            "channel": row["channel"], "text": row["text"], "at": _iso(row["at"]),
            "status": row["status"], "disposition": row["disposition"], "held": row["held"],
            "canAttribute": snapshot is not None, "contactSnapshotId": str(snapshot) if snapshot else None,
            "reviewState": "attributed" if row["attributed"] else "pending", "mediaCount": row["media_count"]}

    def decide(self, case_id, *, action_id, event_ref, source_hash, decision,
               actor_tenant_id, actor_id, contact_snapshot_id=None):
        action_id = _uuid(action_id)
        if (not isinstance(event_ref, str) or not _HEX.fullmatch(event_ref)
                or not isinstance(source_hash, str) or not _HEX.fullmatch(source_hash)
                or decision not in {"handled", "attribute", "leave_unassigned"}):
            _fail("invalid_review_decision", 422)
        snapshot = _uuid(contact_snapshot_id) if contact_snapshot_id is not None else None
        if (decision == "attribute") != (snapshot is not None):
            _fail("invalid_review_decision", 422)
        body_hash = _hash([case_id, event_ref, source_hash, decision, snapshot])
        with self.store._db() as db:
            tenant, actor = self.store._staff(db, actor_tenant_id, actor_id)
            self.store._case(db, case_id)
            prior = db.execute("SELECT * FROM chase_communication_event_reviews WHERE practice_id=%s AND action_id=%s",
                               (self.store.practice_id, action_id)).fetchone()
            if prior:
                if prior["body_hash"] != body_hash:
                    _fail("idempotency_conflict")
            else:
                row = db.execute("""SELECT * FROM chase_communication_review_sources
                    WHERE practice_id=%s AND case_id=%s AND source_ref=%s AND source_hash=%s""",
                    (self.store.practice_id, case_id, event_ref, source_hash)).fetchone()
                if row is None or row["completed_decision"] is not None:
                    _fail("review_source_stale")
                if ((decision == "handled" and row["held"])
                        or (decision != "handled" and not row["held"])):
                    _fail("review_decision_unavailable")
                if decision == "attribute" and (row["attributable_snapshot_id"] is None
                        or str(row["attributable_snapshot_id"]) != snapshot):
                    _fail("contact_stale")
                db.execute("""INSERT INTO chase_communication_event_reviews
                    (practice_id,action_id,case_id,source_ref,source_hash,decision,contact_snapshot_id,
                     actor_tenant_id,actor_id,body_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (self.store.practice_id, action_id, case_id, event_ref, source_hash, decision, snapshot,
                     tenant, actor, body_hash))
            self.dispatcher._audit(db, "communications_review_event", case_id)
            return self.read(case_id, db=db)

    def apply_to_history(self, db, case_id, view):
        """Keep historic evidence visible after handling; remove only review flags."""
        refs = [e.get("reviewRef") for e in view["events"] if e.get("reviewRef")]
        refs += [review_ref("operation", None, o["operationId"]) for o in view["operations"]]
        rows = db.execute("""SELECT source_ref,completed_decision FROM chase_communication_review_sources
            WHERE practice_id=%s AND case_id=%s AND source_ref=ANY(%s::text[])""",
            (self.store.practice_id, case_id, refs)).fetchall()
        pending = {r["source_ref"] for r in rows if r["completed_decision"] is None}
        for event in view["events"]:
            event["needsStaffReview"] = event.get("reviewRef") in pending
        for op in view["operations"]:
            if op["status"] not in {"unknown", "blocked"}:
                op["needsStaffReview"] = review_ref("operation", None, op["operationId"]) in pending
        # Original unassigned rows remain unchanged; only the attributed case's
        # authorized projection gains their body. Fetch a bounded recent window.
        attributed = db.execute("""SELECT * FROM chase_communication_review_sources
            WHERE practice_id=%s AND case_id=%s AND attributed
            ORDER BY at DESC,source_ref DESC LIMIT 201""", (self.store.practice_id, case_id)).fetchall()
        for row in reversed(attributed[:200]):
            view["events"].append({"eventId": row["source_id"], "reviewRef": row["source_ref"],
                "operationId": None, "providerSid": None, "kind": "sms_inbound", "direction": "inbound",
                "channel": row["channel"], "text": row["text"], "status": row["status"],
                "disposition": row["disposition"], "occurredAt": _iso(row["at"]), "receivedAt": _iso(row["at"]),
                "needsStaffReview": row["completed_decision"] is None, "held": False,
                "mediaCount": row["media_count"], "actorId": None, "actorTenantId": None})
        count = self.counts([case_id], db=db)[case_id]
        view["heldInboundCount"] = count["held"]
        view["reviewPendingCount"] = count["pending"]
        view["truncated"] = view["truncated"] or len(attributed) > 200
