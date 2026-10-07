"""Selected-case contact activity is bounded, deterministic, and identifier-free."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from cosmaneura_billing.chase.activity import build_activity, combine_activity
from cosmaneura_billing.chase.models import Attempt, Channel


def _attempt(**overrides):
    values = {
        "channel": Channel.PHONE,
        "template_id": "PRIVATE-TEMPLATE-CANARY",
        "at": "2026-09-25T12:00:00Z",
        "actor": "PRIVATE-STAFF-CANARY",
        "outcome": "left_message",
        "minutes_spent": 2.5,
        "note": "PRIVATE-NOTE-CANARY patient@example.invalid +1 202-555-0199",
    }
    values.update(overrides)
    return Attempt(**values)


def test_empty_activity_has_exact_zero_contract():
    assert build_activity([]) == {
        "schemaVersion": "chase-activity.v1",
        "totalAttempts": 0,
        "countsByChannel": {"phone": 0, "email": 0, "sms": 0, "portal": 0},
        "staffMinutes": 0,
        "lastAttempt": None,
        "history": [],
        "truncated": False,
    }


def test_golden_activity_orders_instants_stably_and_preserves_source():
    attempts = [
        _attempt(channel=Channel.EMAIL, actor="system", outcome="accepted", minutes_spent=0),
        _attempt(at="2026-09-25T08:30:00-04:00", outcome="reached", minutes_spent=2.25),
        _attempt(channel=Channel.SMS, at="2026-09-25T13:00:00+01:00", actor="system", outcome="sent", minutes_spent=0),
        _attempt(channel=Channel.PORTAL, at="2026-09-25T11:00:00Z", outcome="declined", minutes_spent=.75),
    ]
    original = deepcopy(attempts)
    latest = {"at": "2026-09-25T12:30:00Z", "channel": "phone", "performer": "staff", "outcome": "reached", "minutes": 2.25}
    expected = {
        "schemaVersion": "chase-activity.v1",
        "totalAttempts": 4,
        "countsByChannel": {"phone": 1, "email": 1, "sms": 1, "portal": 1},
        "staffMinutes": 3,
        "lastAttempt": latest,
        "history": [
            latest,
            {"at": "2026-09-25T12:00:00Z", "channel": "email", "performer": "system", "outcome": "accepted", "minutes": 0},
            {"at": "2026-09-25T12:00:00Z", "channel": "sms", "performer": "system", "outcome": "sent", "minutes": 0},
            {"at": "2026-09-25T11:00:00Z", "channel": "portal", "performer": "staff", "outcome": "declined", "minutes": .75},
        ],
        "truncated": False,
    }
    activity = build_activity(attempts)
    assert activity == expected
    assert build_activity(attempts) == expected
    assert attempts == original
    assert "PRIVATE" not in json.dumps(activity)
    activity["history"][0]["outcome"] = "unknown"
    assert attempts == original
    assert activity["lastAttempt"] == latest


def test_history_limit_does_not_limit_totals_or_validation():
    start = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    attempts = [
        _attempt(at=(start + timedelta(minutes=index)).isoformat(), minutes_spent=1)
        for index in range(55)
    ]
    activity = build_activity(attempts)
    assert activity["totalAttempts"] == 55
    assert activity["countsByChannel"] == {"phone": 55, "email": 0, "sms": 0, "portal": 0}
    assert activity["staffMinutes"] == 55
    assert len(activity["history"]) == 50
    assert activity["truncated"] is True
    assert activity["lastAttempt"]["at"] == "2026-09-25T12:54:00Z"
    assert activity["history"][-1]["at"] == "2026-09-25T12:05:00Z"
    attempts[0].at = "invalid oldest record outside visible history"
    assert build_activity(attempts) is None


@pytest.mark.parametrize("at", [
    "PRIVATE-DATE-CANARY", "2026-09-25", "2026-09-25T12:00:00",
    "2026-02-30T12:00:00Z", "2026-09-25T12:00:00+24:00",
    "2026-09-25T12:00:00+01:99", "2026-09-25T12:00:00-00:00",
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
    "2026-09-25T12:00:00.1234567Z", "2026-09-25T12:00:00Z" + "x" * 100,
    None, 123,
])
def test_bad_timestamps_make_activity_unavailable_without_inventing_time(at):
    assert build_activity([_attempt(), _attempt(at=at)]) is None


def test_aware_fractional_timestamp_normalizes_without_losing_precision():
    activity = build_activity([_attempt(at="2026-09-25T08:00:00.123456-04:00")])
    assert activity["lastAttempt"]["at"] == "2026-09-25T12:00:00.123456Z"


@pytest.mark.parametrize("channel", ["PRIVATE-CHANNEL-CANARY", None, [], {}])
def test_unknown_channels_never_crash_or_produce_false_partial_counts(channel):
    assert build_activity([_attempt(), _attempt(channel=channel)]) is None


@pytest.mark.parametrize("minutes", [-1, float("nan"), float("inf"), -float("inf"), 1440.1, 10 ** 1000, True, "2.5", None])
def test_invalid_minutes_make_the_whole_projection_unavailable(minutes):
    assert build_activity([_attempt(minutes_spent=minutes)]) is None


@pytest.mark.parametrize("actor", ["", " ", None, 1])
def test_missing_performer_is_not_guessed(actor):
    assert build_activity([_attempt(actor=actor)]) is None


@pytest.mark.parametrize("outcome", ["PRIVATE-OUTCOME-CANARY", "queued", "permanent_failure", {}, None])
def test_unknown_outcomes_are_static_and_do_not_change_recorded_attempt_count(outcome):
    activity = build_activity([_attempt(outcome=outcome)])
    assert activity["totalAttempts"] == 1
    assert activity["history"][0]["outcome"] == "unknown"
    assert "PRIVATE" not in json.dumps(activity)


@pytest.mark.parametrize("outcome", [
    "reached", "left_message", "no_answer", "wrong_number", "declined", "sent",
    "accepted", "delivered", "bounced", "recorded", "unknown",
])
def test_known_recorded_outcomes_are_not_promoted_to_delivery(outcome):
    assert build_activity([_attempt(outcome=outcome)])["lastAttempt"]["outcome"] == outcome


@pytest.mark.parametrize("attempts", [None, "PRIVATE-CONTAINER-CANARY", [None], [{}]])
def test_invalid_record_container_is_unavailable(attempts):
    assert build_activity(attempts) is None


def test_combined_latest_fifty_preserve_full_totals_minutes_ties_and_sources():
    legacy = build_activity([_attempt(at="2026-01-01T12:00:00Z")] * 55)
    dispatch = build_activity([_attempt(channel=Channel.SMS, actor="system", minutes_spent=0,
                                       at="2026-02-01T12:00:00Z", outcome="delivered")] * 53)
    originals = deepcopy((legacy, dispatch))
    result = combine_activity(legacy, dispatch)
    assert result["totalAttempts"] == 108 and result["staffMinutes"] == 137.5
    assert result["countsByChannel"] == {"phone": 55, "email": 0, "sms": 53, "portal": 0}
    assert len(result["history"]) == 50 and result["truncated"]
    assert all(row["channel"] == "sms" for row in result["history"])
    result["history"][0]["outcome"] = "unknown"
    assert (legacy, dispatch) == originals and result["lastAttempt"]["outcome"] == "delivered"
    assert combine_activity(None, dispatch) is None


def test_worklist_dispatch_counts_batch_without_loading_history_or_contacts():
    from types import SimpleNamespace
    from cosmaneura_billing.chase.communications.activity import CommunicationsActivity
    class Database:
        calls = []
        def execute(self, query, params):
            self.calls.append((query, params))
            return SimpleNamespace(fetchall=lambda: [{"case_id": case, "total": 2} for case in params[1]])
    db = Database()
    projection = CommunicationsActivity(SimpleNamespace(practice_id="practice"))
    ids = [f"case-{index}" for index in range(205)]
    assert projection.counts(ids + ids[:1], db=db) == dict.fromkeys(ids, 2)
    assert [len(params[1]) for _, params in db.calls] == [100, 100, 5]
    assert all("provider_events" not in query and "text" not in query.split("SELECT", 1)[1].split("FROM", 1)[0]
               for query, _ in db.calls)
    assert projection.counts([], db=db) == {} and len(db.calls) == 3
