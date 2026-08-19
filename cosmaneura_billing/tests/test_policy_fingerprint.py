"""Exact, secret-safe approval identity for chase policies."""
from dataclasses import replace

import pytest

from cosmaneura_billing.chase.models import Channel
from cosmaneura_billing.chase.policy import ChasePolicy, Step, Template
from cosmaneura_billing.chase.policy_fingerprint import (
    DeliveryConfiguration, policy_fingerprint, policy_step_records,
)
from cosmaneura_billing.chase.worker_config import ChaseWorkerConfig
from cosmaneura_billing.settings import Settings


def _fingerprint(policy: ChasePolicy, timezone: str = "America/New_York") -> str:
    return policy_fingerprint(policy, practice_timezone=timezone)


def test_approval_metadata_is_not_part_of_the_executable_policy_identity():
    original = ChasePolicy()
    approved = ChasePolicy().approve(by="admin-oid", at="2026-08-18T12:00:00Z")

    assert _fingerprint(original) == _fingerprint(approved)


@pytest.mark.parametrize("change", ["template", "url", "timezone", "cadence"])
def test_every_executable_change_invalidates_exact_policy_approval(change):
    policy = ChasePolicy()
    baseline = _fingerprint(policy)

    if change == "template":
        first = policy.templates[0]
        changed = replace(first, body=first.body + "\nPlease reply with questions.")
        policy = replace(policy, templates=(changed, *policy.templates[1:]))
        actual = _fingerprint(policy)
    elif change == "url":
        actual = _fingerprint(replace(policy, upload_url="https://secure.invalid/v2"))
    elif change == "timezone":
        actual = _fingerprint(policy, "America/Chicago")
    else:
        steps = list(policy.steps)
        steps[1] = replace(steps[1], day_offset=2)
        actual = _fingerprint(replace(policy, steps=tuple(steps)))

    assert actual != baseline


def test_template_tuple_order_does_not_change_policy_identity():
    policy = ChasePolicy()
    reordered = replace(policy, templates=tuple(reversed(policy.templates)))

    assert _fingerprint(reordered) == _fingerprint(policy)


def test_persisted_step_metadata_contains_hashes_but_no_message_or_url():
    policy = ChasePolicy(upload_url="https://secret.invalid/upload")

    rows = policy_step_records(policy)

    assert [row.ordinal for row in rows] == [0, 1, 2, 3]
    assert [row.performer for row in rows] == ["system", "system", "system", "staff"]
    rendered = repr(rows)
    assert "secret.invalid" not in rendered
    assert "We're missing" not in rendered
    assert all(len(row.template_fingerprint) == 64 for row in rows)


@pytest.mark.parametrize(
    "policy,match",
    [
        (
            ChasePolicy(templates=(
                Template("duplicate", Channel.EMAIL, "one", "body"),
                Template("duplicate", Channel.EMAIL, "two", "body"),
            )),
            "unique",
        ),
        (
            ChasePolicy(
                steps=(Step(Channel.EMAIL, "missing", 0),),
                templates=(Template("present", Channel.EMAIL, "subject", "body"),),
            ),
            "unknown template",
        ),
        (
            ChasePolicy(
                steps=(Step(Channel.EMAIL, "email.first", 2),
                       Step(Channel.SMS, "sms.first", 1)),
            ),
            "nondecreasing",
        ),
        (
            ChasePolicy(
                steps=(Step(Channel.SMS, "email.first", 0),),
            ),
            "channels must match",
        ),
    ],
)
def test_invalid_policy_shapes_fail_before_persistence(policy, match):
    with pytest.raises(ValueError, match=match):
        _fingerprint(policy)


def test_invalid_timezone_fails_before_policy_registration():
    with pytest.raises(ValueError, match="IANA timezone"):
        _fingerprint(ChasePolicy(), "Not/A_Zone")


@pytest.mark.parametrize("policy,match", [
    (ChasePolicy(name=""), "policy name"),
    (ChasePolicy(quiet_hours=(8, 8)), "non-empty window"),
    (
        ChasePolicy(templates=(
            Template("email.first", Channel.EMAIL, "subject", "x" * 10_001),
        )),
        "template body",
    ),
])
def test_runtime_message_bounds_fail_before_policy_approval(policy, match):
    with pytest.raises(ValueError, match=match):
        _fingerprint(policy)


def _acs_delivery(**changes):
    values = {
        "provider": "azure_communication_services_email",
        "provider_resource": "https://pilot.communication.azure.com",
        "enabled_channels": (Channel.EMAIL,),
        "sender_address": "sender@pilot.example",
        "reply_to_address": "billing@pilot.example",
        "contact_snapshot_max_age_hours": 24,
        "scheduler_enabled": True,
        "operational_enabled": True,
    }
    values.update(changes)
    return DeliveryConfiguration(**values)


@pytest.mark.parametrize("change", ["sender", "reply", "resource", "age"])
def test_operational_delivery_drift_invalidates_policy_approval(change):
    baseline_config = _acs_delivery()
    changes = {
        "sender": {"sender_address": "other@pilot.example"},
        "reply": {"reply_to_address": "other@pilot.example"},
        "resource": {
            "provider_resource": "https://other.communication.azure.com",
        },
        "age": {"contact_snapshot_max_age_hours": 12},
    }

    baseline = policy_fingerprint(
        ChasePolicy(), practice_timezone="UTC", delivery=baseline_config,
    )
    changed = policy_fingerprint(
        ChasePolicy(), practice_timezone="UTC", delivery=_acs_delivery(**changes[change]),
    )

    assert changed != baseline


def test_delivery_configuration_repr_redacts_addresses_and_provider_resource():
    rendered = repr(_acs_delivery())

    assert "sender@" not in rendered
    assert "billing@" not in rendered
    assert "communication.azure.com" not in rendered
    assert rendered.count("<redacted>") == 2


def test_api_and_worker_build_the_same_delivery_identity_without_sharing_secret():
    api_settings = Settings(
        chase_transport="acs_email",
        acs_email_endpoint="HTTPS://PILOT.communication.azure.com/",
        acs_email_sender_address="sender@pilot.example",
        acs_email_reply_to_address="billing@pilot.example",
    )
    worker_settings = ChaseWorkerConfig(
        chase_transport="acs_email",
        acs_email_connection_string=(
            "Endpoint=https://pilot.communication.azure.com;"
            "AccessKey=worker-only-secret="
        ),
        acs_email_endpoint="https://pilot.communication.azure.com",
        acs_email_sender_address="sender@pilot.example",
        acs_email_reply_to_address="billing@pilot.example",
    )

    api_delivery = DeliveryConfiguration.from_settings(api_settings)
    worker_delivery = worker_settings.build_delivery_configuration()

    assert api_settings.acs_email_connection_string == ""
    assert api_delivery == worker_delivery
    assert api_delivery.provider_resource == (
        "https://pilot.communication.azure.com"
    )
    assert policy_fingerprint(
        ChasePolicy(), practice_timezone="UTC", delivery=api_delivery,
    ) == policy_fingerprint(
        ChasePolicy(), practice_timezone="UTC", delivery=worker_delivery,
    )


def test_delivery_configuration_never_reads_the_worker_connection_secret():
    class MetadataOnlySettings:
        chase_transport = "acs_email"
        acs_email_endpoint = "https://pilot.communication.azure.com"
        acs_email_sender_address = "sender@pilot.example"
        acs_email_reply_to_address = "billing@pilot.example"
        contact_snapshot_max_age_hours = 24
        chase_scheduler_enabled = False
        patient_messaging_enabled = False
        patient_messaging_operationally_activated = False

        @property
        def acs_email_connection_string(self):
            raise AssertionError("worker secret must not be read")

    delivery = DeliveryConfiguration.from_settings(MetadataOnlySettings())

    assert delivery.provider_resource == "https://pilot.communication.azure.com"
