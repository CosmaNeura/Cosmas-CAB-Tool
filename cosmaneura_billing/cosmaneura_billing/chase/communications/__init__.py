"""Local, synthetic communications foundation; no transport is enabled by default."""

from .store import CommunicationsError, CommunicationsStore, Eligibility

__all__ = ["CommunicationsError", "CommunicationsStore", "Eligibility"]
