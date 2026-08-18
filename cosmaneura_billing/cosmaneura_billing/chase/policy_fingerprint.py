"""Canonical fingerprints for durable chase-policy approval.

Only hashes and typed cadence metadata cross the persistence boundary. The
approved upload URL and complete template text remain process secrets, but are
still covered by the fingerprint so a deployment-time change fails closed and
requires a new named approval.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Channel
from .policy import ChasePolicy, Template, assert_no_phi

if TYPE_CHECKING:
    from ..settings import Settings


@dataclass(frozen=True)
class PolicyStepRecord:
    """Non-message metadata persisted for one approved cadence step."""

    ordinal: int
    channel: Channel
    template_id: str
    day_offset: int
    performer: str
    template_fingerprint: str


@dataclass(frozen=True, repr=False)
class DeliveryConfiguration:
    """Operational inputs covered by approval but never persisted in cleartext."""

    provider: str = "record_only"
    provider_resource: str = ""
    enabled_channels: tuple[Channel, ...] = ()
    sender_address: str = ""
    reply_to_address: str = ""
    contact_snapshot_max_age_hours: int = 24
    scheduler_enabled: bool = False
    operational_enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.scheduler_enabled) is not bool or type(self.operational_enabled) is not bool:
            raise ValueError("scheduler and delivery operational flags must be boolean")
        if self.operational_enabled and not self.scheduler_enabled:
            raise ValueError("operational delivery requires the scheduler")
        if self.provider not in {
            "record_only", "azure_communication_services_email",
        }:
            raise ValueError("unsupported chase delivery provider")
        if (
            isinstance(self.contact_snapshot_max_age_hours, bool)
            or not isinstance(self.contact_snapshot_max_age_hours, int)
            or not 1 <= self.contact_snapshot_max_age_hours <= 168
        ):
            raise ValueError("contact snapshot maximum age must be 1-168 hours")
        if len(self.enabled_channels) != len(set(self.enabled_channels)):
            raise ValueError("enabled delivery channels must be unique")
        if self.provider == "record_only":
            if any((
                self.provider_resource,
                self.enabled_channels,
                self.sender_address,
                self.reply_to_address,
                self.operational_enabled,
            )):
                raise ValueError("record-only delivery cannot configure a provider route")
        elif (
            self.enabled_channels != (Channel.EMAIL,)
            or not self.provider_resource
            or not self.sender_address
            or not self.reply_to_address
        ):
            raise ValueError("the ACS pilot delivery configuration is email-only")

    def __repr__(self) -> str:
        return (
            f"DeliveryConfiguration(provider={self.provider!r}, "
            "provider_resource='<redacted>', addresses='<redacted>', "
            f"enabled_channels={tuple(c.value for c in self.enabled_channels)!r}, "
            f"contact_snapshot_max_age_hours={self.contact_snapshot_max_age_hours!r}, "
            f"scheduler_enabled={self.scheduler_enabled!r}, "
            f"operational_enabled={self.operational_enabled!r})"
        )

    @classmethod
    def from_settings(cls, settings: "Settings") -> "DeliveryConfiguration":
        if settings.chase_transport == "record_only":
            return cls(
                contact_snapshot_max_age_hours=settings.contact_snapshot_max_age_hours,
                scheduler_enabled=settings.chase_scheduler_enabled,
            )
        segments = {
            key.strip().lower(): value
            for segment in settings.acs_email_connection_string.split(";")
            if "=" in segment
            for key, value in (segment.split("=", 1),)
        }
        endpoint = segments.get("endpoint") or ""
        parsed = urlsplit(endpoint)
        resource = f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}"
        return cls(
            provider="azure_communication_services_email",
            provider_resource=resource,
            enabled_channels=(Channel.EMAIL,),
            sender_address=settings.acs_email_sender_address,
            reply_to_address=settings.acs_email_reply_to_address,
            contact_snapshot_max_age_hours=settings.contact_snapshot_max_age_hours,
            scheduler_enabled=settings.chase_scheduler_enabled,
            operational_enabled=bool(
                settings.patient_messaging_enabled
                and settings.patient_messaging_operationally_activated
                and settings.chase_scheduler_enabled
            ),
        )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_text(
    value: str, *, field: str, minimum: int, maximum: int, multiline: bool = False,
) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field} must contain {minimum}-{maximum} characters")
    allowed_controls = {"\n", "\t"} if multiline else set()
    if any((ord(char) < 32 or ord(char) == 127) and char not in allowed_controls
           for char in value):
        raise ValueError(f"{field} contains unsupported control characters")
    return value


def template_fingerprint(template: Template) -> str:
    """Bind the exact template without returning or persisting its contents."""
    template_id = _bounded_text(
        template.template_id, field="template ID", minimum=1, maximum=128,
    )
    subject = _bounded_text(
        template.subject, field="template subject", minimum=0, maximum=200,
    )
    body = _bounded_text(
        template.body, field="template body", minimum=1, maximum=10_000,
        multiline=True,
    )
    assert_no_phi(template.subject)
    assert_no_phi(template.body)
    return _digest({
        "schema": "cosmaneura.chase-template.v1",
        "template_id": template_id,
        "channel": template.channel.value,
        "subject": subject,
        "body": body,
    })


def policy_step_records(policy: ChasePolicy) -> tuple[PolicyStepRecord, ...]:
    """Validate and reduce cadence steps to their safe persisted shape."""
    if not 1 <= len(policy.steps) <= 64:
        raise ValueError("a chase policy requires 1-64 cadence steps")
    if not 1 <= policy.max_touches <= 64:
        raise ValueError("a chase policy max_touches must be between 1 and 64")

    template_ids = [template.template_id for template in policy.templates]
    if len(template_ids) != len(set(template_ids)):
        raise ValueError("chase policy template IDs must be unique")
    templates = {template.template_id: template for template in policy.templates}

    rows: list[PolicyStepRecord] = []
    previous_offset = -1
    for ordinal, step in enumerate(policy.steps):
        if type(step.day_offset) is not int or not 0 <= step.day_offset <= 365:
            raise ValueError("cadence day offsets must be integer days from 0 through 365")
        if step.day_offset < previous_offset:
            raise ValueError("cadence day offsets are absolute and must be nondecreasing")
        previous_offset = step.day_offset
        try:
            template = templates[step.template_id]
        except KeyError as exc:
            raise ValueError(
                f"cadence step references unknown template {step.template_id!r}"
            ) from exc
        if template.channel is not step.channel:
            raise ValueError("cadence step and template channels must match")
        performer = "staff" if step.channel is Channel.PHONE else "system"
        rows.append(PolicyStepRecord(
            ordinal=ordinal,
            channel=step.channel,
            template_id=step.template_id,
            day_offset=step.day_offset,
            performer=performer,
            template_fingerprint=template_fingerprint(template),
        ))
    return tuple(rows)


def policy_fingerprint(
    policy: ChasePolicy,
    *,
    practice_timezone: str,
    delivery: DeliveryConfiguration | None = None,
) -> str:
    """Return the exact approval identity for a complete executable policy."""
    _bounded_text(policy.name, field="policy name", minimum=1, maximum=128)
    _bounded_text(
        policy.practice_name, field="practice name", minimum=1, maximum=256,
    )
    _bounded_text(policy.upload_url, field="upload URL", minimum=1, maximum=2_048)
    try:
        zone = ZoneInfo(practice_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("practice timezone must be a valid IANA timezone") from exc
    if len(policy.quiet_hours) != 2 or any(
        type(hour) is not int or not 0 <= hour <= 23 for hour in policy.quiet_hours
    ):
        raise ValueError("quiet hours must contain two integer hours from 0 through 23")
    if policy.quiet_hours[0] == policy.quiet_hours[1]:
        raise ValueError("quiet hours must define a non-empty window")

    steps = policy_step_records(policy)
    templates = sorted(policy.templates, key=lambda item: item.template_id)
    delivery = delivery or DeliveryConfiguration()
    canonical = {
        "schema": "cosmaneura.chase-policy.v1",
        "name": policy.name,
        "practice_name": policy.practice_name,
        "upload_url": policy.upload_url,
        "approval_mode": policy.approval_mode.value,
        "practice_timezone": zone.key,
        "quiet_hours": list(policy.quiet_hours),
        "max_touches": policy.max_touches,
        "delivery": {
            "provider": delivery.provider,
            "provider_resource": delivery.provider_resource,
            "enabled_channels": [
                channel.value for channel in delivery.enabled_channels
            ],
            "sender_address": delivery.sender_address,
            "reply_to_address": delivery.reply_to_address,
            "contact_snapshot_max_age_hours": (
                delivery.contact_snapshot_max_age_hours
            ),
            "scheduler_enabled": delivery.scheduler_enabled,
            "operational_enabled": delivery.operational_enabled,
        },
        "steps": [
            {
                "ordinal": row.ordinal,
                "channel": row.channel.value,
                "template_id": row.template_id,
                "day_offset": row.day_offset,
                "performer": row.performer,
                "template_fingerprint": row.template_fingerprint,
            }
            for row in steps
        ],
        "templates": [
            {
                "template_id": template.template_id,
                "channel": template.channel.value,
                "subject": template.subject,
                "body": template.body,
            }
            for template in templates
        ],
    }
    return _digest(canonical)


__all__ = [
    "DeliveryConfiguration", "PolicyStepRecord", "policy_fingerprint",
    "policy_step_records", "template_fingerprint",
]
