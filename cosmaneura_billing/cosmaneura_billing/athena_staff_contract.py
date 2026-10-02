"""Immutable server binding data for the staff connector, without vendor I/O.

These values neither authorize a request nor attest a patient match. A server
must obtain them from its approved connection and case mapping, never from an
untrusted request's claimed practice or data classification. Vendor URLs and
wire serialization remain in athena_client.py.
"""
from dataclasses import dataclass
import hashlib
import json
import re


class AthenaStaffScopeError(ValueError):
    def __init__(self):
        super().__init__("invalid Athena staff scope")


def _text(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or value != value.strip() or any(ord(c) < 32 or ord(c) == 127
                or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise AthenaStaffScopeError()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,17}", value):
        raise AthenaStaffScopeError()


def _revision(value):
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise AthenaStaffScopeError()


def _digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class AthenaStaffProfile:
    server_practice_id: str
    environment: str
    vendor_practice_id: str
    connection_revision: int

    def __post_init__(self):
        _text(self.server_practice_id)
        if type(self.environment) is not str or self.environment not in ("preview", "production"):
            raise AthenaStaffScopeError()
        _id(self.vendor_practice_id)
        _revision(self.connection_revision)

    @property
    def fingerprint(self):
        self.__post_init__()
        return _digest(["athena-staff-profile.v1", self.server_practice_id,
                        self.environment, self.vendor_practice_id, self.connection_revision])

    def __repr__(self):
        return f"AthenaStaffProfile(environment={self.environment!r}, binding='<redacted>')"


@dataclass(frozen=True, repr=False)
class AthenaStaffScope:
    profile: AthenaStaffProfile
    case_id: str
    patient_ref: str
    source_patient_id: str
    binding_revision: int

    def __post_init__(self):
        if type(self.profile) is not AthenaStaffProfile:
            raise AthenaStaffScopeError()
        self.profile.__post_init__()
        _text(self.case_id)
        _text(self.patient_ref)
        _id(self.source_patient_id)
        _revision(self.binding_revision)

    @property
    def fingerprint(self):
        self.__post_init__()
        return _digest(["athena-staff-scope.v1", self.profile.fingerprint,
                        self.case_id, self.patient_ref, self.source_patient_id,
                        self.binding_revision])

    def __repr__(self):
        return "AthenaStaffScope(binding='<redacted>')"
