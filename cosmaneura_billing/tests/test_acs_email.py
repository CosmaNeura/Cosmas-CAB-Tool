"""Narrow, PHI-minimal contract for the ACS email adapter."""
from __future__ import annotations

from uuid import uuid4

import pytest

from cosmaneura_billing.chase.acs_email import (
    ACSEmailTransport, EmailSubmissionError,
)
from cosmaneura_billing.chase.models import Channel
from cosmaneura_billing.chase.policy import MessageContainsPHI


class FakeEmailClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[dict, dict]] = []

    def begin_send(self, message, **kwargs):
        self.calls.append((message, kwargs))
        if self.error is not None:
            raise self.error
        return object()


def _transport(client=None) -> ACSEmailTransport:
    return ACSEmailTransport(
        client=client or FakeEmailClient(),
        sender_address="DoNotReply@synthetic.azurecomm.net",
        reply_to_address="billing@example.invalid",
    )


def test_submits_one_plain_text_recipient_with_stable_operation_id():
    client = FakeEmailClient()
    transport = _transport(client)
    operation_id = str(uuid4())

    outcome = transport(
        channel=Channel.EMAIL,
        recipient="patient@example.invalid",
        subject="Insurance information needed",
        body="Please use the secure upload link: https://example.invalid/upload",
        operation_id=operation_id,
    )

    assert outcome == "accepted"
    message, options = client.calls[0]
    assert message == {
        "senderAddress": "DoNotReply@synthetic.azurecomm.net",
        "recipients": {"to": [{"address": "patient@example.invalid"}]},
        "content": {
            "subject": "Insurance information needed",
            "plainText": "Please use the secure upload link: https://example.invalid/upload",
        },
        "replyTo": [{"address": "billing@example.invalid"}],
        "userEngagementTrackingDisabled": True,
    }
    assert options == {
        "operation_id": operation_id,
        "polling": False,
        "logging_enable": False,
    }
    assert "html" not in message["content"]
    assert set(message["recipients"]) == {"to"}


@pytest.mark.parametrize("channel", [Channel.SMS, Channel.PORTAL, Channel.PHONE])
def test_refuses_every_non_email_channel(channel):
    client = FakeEmailClient()
    with pytest.raises(ValueError, match="email only"):
        _transport(client)(
            channel=channel,
            recipient="patient@example.invalid",
            subject="subject",
            body="body",
            operation_id=str(uuid4()),
        )
    assert client.calls == []


@pytest.mark.parametrize("recipient", [
    "", "not-an-email", "a@b@example.com", "a\n@b.com", "a,b@example.com",
    "a..b@example.com",
])
def test_invalid_or_injectable_recipient_fails_before_sdk_call(recipient):
    client = FakeEmailClient()
    with pytest.raises(ValueError, match="recipient"):
        _transport(client)(
            channel=Channel.EMAIL,
            recipient=recipient,
            subject="subject",
            body="body",
            operation_id=str(uuid4()),
        )
    assert client.calls == []


def test_requires_a_uuid_owned_by_the_durable_outbox():
    client = FakeEmailClient()
    with pytest.raises(ValueError, match="delivery UUID"):
        _transport(client)(
            channel=Channel.EMAIL,
            recipient="patient@example.invalid",
            subject="subject",
            body="body",
            operation_id="caller-chosen-text",
        )
    assert client.calls == []


@pytest.mark.parametrize("subject,body", [
    ("subject\nBcc: attacker@example.com", "body"),
    ("subject", "x" * 10_001),
])
def test_message_bounds_and_header_controls_fail_before_sdk_call(subject, body):
    client = FakeEmailClient()
    with pytest.raises(ValueError, match="email (subject|body)"):
        _transport(client)(
            channel=Channel.EMAIL,
            recipient="patient@example.invalid",
            subject=subject,
            body=body,
            operation_id=str(uuid4()),
        )
    assert client.calls == []


def test_phi_guard_is_rechecked_at_the_transport_boundary():
    client = FakeEmailClient()
    with pytest.raises(MessageContainsPHI):
        _transport(client)(
            channel=Channel.EMAIL,
            recipient="patient@example.invalid",
            subject="Your member ID",
            body="Please contact us.",
            operation_id=str(uuid4()),
        )
    assert client.calls == []


def test_sdk_failure_is_sanitized_and_does_not_claim_delivery():
    transport = _transport(FakeEmailClient(
        RuntimeError("patient@example.invalid leaked detail")
    ))
    with pytest.raises(EmailSubmissionError) as caught:
        transport(
            channel=Channel.EMAIL,
            recipient="patient@example.invalid",
            subject="subject",
            body="body",
            operation_id=str(uuid4()),
        )
    assert str(caught.value) == "ACS email submission failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_repr_redacts_client_and_addresses():
    rendered = repr(_transport())
    assert "DoNotReply" not in rendered
    assert "billing@" not in rendered
    assert "<redacted>" in rendered
