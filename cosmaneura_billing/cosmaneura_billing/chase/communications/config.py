"""Configuration for the separate SMS/call worker and callback service.

This boundary never reads the API database URL, review signing key, identity,
Athena credentials, or inference configuration.
"""
from dataclasses import dataclass, field
import re
from urllib.parse import urlsplit

from ...settings import (SettingsError, _database_url_uses_role, _parse_communications_channels,
                         _parse_communications_test_recipients, _parse_communications_test_max_dispatch_starts)
from ...communications_channels import enabled_channels
from ...communications_test_policy import validated_test_recipients, validated_test_max_dispatch_starts
from ..worker_config import _worker_bool, _worker_bounded_int
from .dispatch import DispatchPolicy


@dataclass(frozen=True, repr=False)
class TwilioServiceConfig:
    environment: str
    practice_id: str
    practice_name: str
    practice_timezone: str
    chase_worker_database_role: str
    chase_worker_database_url: str = field(repr=False)
    communications_enabled: bool
    chase_transport: str
    chase_scheduler_enabled: bool
    patient_messaging_enabled: bool
    patient_messaging_operationally_activated: bool
    twilio_account_sid: str
    twilio_from_number: str = field(repr=False)
    twilio_callback_base_url: str
    direct_contract_list: str = field(repr=False)
    chase_gate_hmac_key: str = field(repr=False)
    contact_snapshot_max_age_hours: int
    communications_enabled_channels: tuple[str, ...] = ("sms",)
    communications_test_mode: bool = False
    communications_test_max_dispatch_starts: int = 0
    communications_test_recipients: tuple[str, ...] = field(default=(), repr=False)
    twilio_messaging_service_sid: str = ""

    def __repr__(self):
        return "TwilioServiceConfig(practice='<bound>', credentials='<redacted>')"

    def __post_init__(self):
        try:
            object.__setattr__(self, "communications_enabled_channels", enabled_channels(self.communications_enabled_channels))
        except ValueError:
            raise SettingsError("invalid communications enabled channels") from None
        try:
            object.__setattr__(self, "communications_test_recipients", validated_test_recipients(
                self.communications_test_mode, self.communications_test_recipients, self.communications_enabled_channels))
            validated_test_max_dispatch_starts(self.communications_test_max_dispatch_starts, self.communications_test_mode)
        except ValueError as exc:
            raise SettingsError(str(exc)) from None
        if not isinstance(self.twilio_messaging_service_sid, str) or (
                self.twilio_messaging_service_sid and not re.fullmatch(r"MG[0-9a-fA-F]{32}", self.twilio_messaging_service_sid)):
            raise SettingsError("invalid Twilio Messaging Service SID")
        role = self.chase_worker_database_role
        if (not isinstance(role, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,62}", role)
                or role.casefold().startswith("pg_") or role.casefold() in {"postgres", "azure_pg_admin"}
                or not _database_url_uses_role(self.chase_worker_database_url, role=role)):
            raise SettingsError("Twilio service requires its bound least-privilege worker database URL")
        if (self.environment not in {"development", "test", "staging", "production"}
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.practice_id)
                or not self.direct_contract_list.strip()
                or self.communications_enabled is not True or self.chase_transport != "twilio"):
            raise SettingsError("Twilio service configuration is incomplete")
        try:
            origin = urlsplit(self.twilio_callback_base_url)
            if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                    or origin.query or origin.fragment or origin.path not in {"", "/"}
                    or origin.port not in {None, 443} or any(c.isspace() for c in self.twilio_callback_base_url)):
                raise ValueError()
            DispatchPolicy(self.practice_name, self.practice_timezone,
                contact_max_age_hours=self.contact_snapshot_max_age_hours,
                scheduler_enabled=self.chase_scheduler_enabled,
                patient_messaging_enabled=self.patient_messaging_enabled,
                operationally_activated=self.patient_messaging_operationally_activated,
                enabled_channels=self.communications_enabled_channels,
                test_mode=self.communications_test_mode, test_recipients=self.communications_test_recipients,
                test_max_dispatch_starts=self.communications_test_max_dispatch_starts)
        except (ValueError, TypeError):
            raise SettingsError("Twilio service policy or HTTPS callback origin is invalid") from None
        if self.patient_messaging_operationally_activated and not self.patient_messaging_enabled:
            raise SettingsError("Twilio operational activation requires messaging enabled")
        if self.patient_messaging_enabled and (self.environment not in {"staging", "production"}
                or not self.chase_scheduler_enabled or not self.patient_messaging_operationally_activated):
            raise SettingsError("Twilio messaging requires reviewed deployed activation and its worker")
        if self.environment in {"staging", "production"} and self.chase_scheduler_enabled and len(self.chase_gate_hmac_key.encode()) < 32:
            raise SettingsError("A deployed Twilio worker requires a 32-byte chase gate key")

    @classmethod
    def from_env(cls, env):
        return cls(environment=env.get("COSMANEURA_ENV", "development").strip().lower(),
            practice_id=env.get("COSMANEURA_PRACTICE_ID", "").strip(),
            practice_name=env.get("COSMANEURA_PRACTICE_NAME", "your care team").strip(),
            practice_timezone=env.get("COSMANEURA_PRACTICE_TIMEZONE", "UTC").strip(),
            chase_worker_database_role=env.get("COSMANEURA_CHASE_WORKER_DB_ROLE", "cosmaneura_chase_worker").strip(),
            chase_worker_database_url=env.get("COSMANEURA_CHASE_WORKER_DATABASE_URL", "").strip(),
            communications_enabled=_worker_bool(env, "COSMANEURA_COMMUNICATIONS_ENABLED", False),
            communications_enabled_channels=_parse_communications_channels(env),
            communications_test_mode=_worker_bool(env, "COSMANEURA_COMMUNICATIONS_TEST_MODE", False),
            communications_test_max_dispatch_starts=_parse_communications_test_max_dispatch_starts(env),
            communications_test_recipients=_parse_communications_test_recipients(env),
            chase_transport=env.get("COSMANEURA_CHASE_TRANSPORT", "record_only").strip().lower(),
            chase_scheduler_enabled=_worker_bool(env, "COSMANEURA_CHASE_SCHEDULER_ENABLED", False),
            patient_messaging_enabled=_worker_bool(env, "COSMANEURA_PATIENT_MESSAGING_ENABLED", False),
            patient_messaging_operationally_activated=_worker_bool(env, "COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED", False),
            twilio_account_sid=env.get("COSMANEURA_TWILIO_ACCOUNT_SID", ""),
            twilio_from_number=env.get("COSMANEURA_TWILIO_FROM_NUMBER", ""),
            twilio_callback_base_url=env.get("COSMANEURA_TWILIO_CALLBACK_BASE_URL", ""),
            twilio_messaging_service_sid=env.get("COSMANEURA_TWILIO_MESSAGING_SERVICE_SID", ""),
            direct_contract_list=env.get("COSMANEURA_DIRECT_CONTRACT_LIST", "direct_contract_patients.csv"),
            chase_gate_hmac_key=env.get("COSMANEURA_CHASE_GATE_HMAC_KEY", ""),
            contact_snapshot_max_age_hours=_worker_bounded_int(env, "COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS", 24, 1, 168))
