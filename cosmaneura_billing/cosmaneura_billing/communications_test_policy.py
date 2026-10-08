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


MAX_TEST_DISPATCH_STARTS = 5


def parse_test_max_dispatch_starts(raw):
    if type(raw) is not str or not re.fullmatch(r"[0-5]", raw):
        raise ValueError("communications test dispatch ceiling must be 0 through 5")
    return int(raw)


def validated_test_max_dispatch_starts(value, mode):
    if type(value) is not int or not 0 <= value <= MAX_TEST_DISPATCH_STARTS:
        raise ValueError("communications test dispatch ceiling must be 0 through 5")
    if value and mode is not True:
        raise ValueError("communications test dispatch ceiling requires test mode")
    return value


# GSM 03.38 basic characters use one septet; extension-table characters use two.
# Keep the original body intact and reject non-GSM text instead of transliterating.
_GSM_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ "
    "!\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM_EXTENSION = frozenset("\f^{}\\[~]|€")


def single_gsm_segment(text):
    if type(text) is not str or not text or len(text) > 160:
        return False
    septets = 0
    for character in text:
        if character in _GSM_BASIC:
            septets += 1
        elif character in _GSM_EXTENSION:
            septets += 2
        else:
            return False
    return septets <= 160
