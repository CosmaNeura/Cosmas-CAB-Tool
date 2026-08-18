"""Narrow Azure Communication Services email transport.

The API never constructs this adapter. The scheduled chase worker injects it
only after a durable policy approval and outbox reservation exist. Submission
means ACS accepted the long-running operation; it does not claim delivery.
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from .models import Channel
from .policy import assert_no_phi

_EMAIL = re.compile(
    r"(?=.{3,254}\Z)(?![^@]*\.\.)"
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


class EmailSubmissionError(RuntimeError):
    """ACS did not return an accepted submission response."""


def _email_address(value: str, *, field: str) -> str:
    address = (value or "").strip()
    if not _EMAIL.fullmatch(address):
        raise ValueError(f"{field} must be one valid email address")
    return address


def _message_text(value: str, *, field: str, maximum: int, multiline: bool) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{field} must contain 1-{maximum} characters")
    allowed_controls = {"\n", "\t"} if multiline else set()
    if any((ord(char) < 32 or ord(char) == 127) and char not in allowed_controls
           for char in value):
        raise ValueError(f"{field} contains unsupported control characters")
    return value


class ACSEmailTransport:
    """Submit one plain-text, single-recipient email with a stable operation ID."""

    def __init__(
        self,
        *,
        client: Any,
        sender_address: str,
        reply_to_address: str,
    ) -> None:
        self._client = client
        self._sender = _email_address(sender_address, field="ACS sender")
        self._reply_to = _email_address(reply_to_address, field="ACS reply-to")

    @classmethod
    def from_connection_string(
        cls,
        *,
        connection_string: str,
        sender_address: str,
        reply_to_address: str,
    ) -> "ACSEmailTransport":
        if not connection_string.strip():
            raise ValueError("ACS email connection string is required")
        try:
            from azure.communication.email import EmailClient
        except ImportError as exc:  # pragma: no cover - deployment wiring
            raise RuntimeError(
                "ACS email transport requires the 'messaging' package extra"
            ) from exc
        client = EmailClient.from_connection_string(
            connection_string, logging_enable=False,
        )
        return cls(
            client=client,
            sender_address=sender_address,
            reply_to_address=reply_to_address,
        )

    def __repr__(self) -> str:
        return "ACSEmailTransport(client='<redacted>', addresses='<redacted>')"

    def __call__(
        self,
        *,
        channel: Channel,
        recipient: str,
        subject: str,
        body: str,
        operation_id: str = "",
    ) -> str:
        if channel is not Channel.EMAIL:
            raise ValueError("ACS pilot transport supports email only")
        try:
            operation_id = str(UUID(operation_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("ACS email submission requires a delivery UUID") from exc
        destination = _email_address(recipient, field="email recipient")
        subject = _message_text(
            subject, field="email subject", maximum=200, multiline=False,
        )
        body = _message_text(
            body, field="email body", maximum=10_000, multiline=True,
        )
        assert_no_phi(subject)
        assert_no_phi(body)
        message = {
            "senderAddress": self._sender,
            "recipients": {"to": [{"address": destination}]},
            "content": {"subject": subject, "plainText": body},
            "replyTo": [{"address": self._reply_to}],
            "userEngagementTrackingDisabled": True,
        }
        submission_failed = False
        try:
            # polling=False still performs the initial request and requires an
            # ACS 202 response, but does not hold a worker open waiting for a
            # delivery result. The outbox owns the stable operation UUID.
            self._client.begin_send(
                message, operation_id=operation_id, polling=False,
                logging_enable=False,
            )
        except Exception:
            # SDK messages can include request details. The worker records only
            # this sanitized classification and the random delivery UUID.
            submission_failed = True
        if submission_failed:
            # Raise outside the handler so the SDK exception is not retained in
            # __context__ for an exception collector to serialize.
            raise EmailSubmissionError("ACS email submission failed") from None
        return "accepted"


__all__ = ["ACSEmailTransport", "EmailSubmissionError"]
