"""Explicit destination scope for synthetic SMS trials, independent of consent.

The server operator lists only consenting test recipients. Membership never
creates consent evidence and never enables transport. No values enter errors.
"""
import re

MAX_TEST_RECIPIENTS = 20


def _recipients(value):
    if (type(value) is not tuple or len(value) > MAX_TEST_RECIPIENTS
            or any(type(phone) is not str or not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone)
                   for phone in value)
            or len(set(value)) != len(value)):
        raise ValueError("invalid communications test recipients")
    return tuple(sorted(value))


def parse_test_recipients(raw):
    if not isinstance(raw, str):
        raise ValueError("invalid communications test recipients")
    return _recipients(()) if raw == "" else _recipients(tuple(part.strip() for part in raw.split(",")))


def validated_test_recipients(mode, recipients, channels):
    recipients = _recipients(recipients)
    if type(mode) is not bool or (recipients and not mode):
        raise ValueError("communications test recipients require explicit test mode")
    if mode and channels != ("sms",):
        raise ValueError("communications test mode permits SMS only")
    return recipients
