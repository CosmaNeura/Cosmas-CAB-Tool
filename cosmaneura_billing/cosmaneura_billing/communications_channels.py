"""Closed outbound channel configuration, independent of provider credentials."""

CHANNELS = ("sms", "automated_voice")
DEFAULT_CHANNELS = ("sms",)


def enabled_channels(value):
    if (type(value) is not tuple or not 1 <= len(value) <= 2
            or any(type(channel) is not str or channel not in CHANNELS for channel in value)
            or len(set(value)) != len(value)):
        raise ValueError("invalid communications enabled channels")
    return tuple(channel for channel in CHANNELS if channel in value)
