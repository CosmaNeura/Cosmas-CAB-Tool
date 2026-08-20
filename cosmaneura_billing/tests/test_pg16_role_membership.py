"""Fail-closed role-membership classification for PostgreSQL 16."""
from __future__ import annotations

import pytest

from cosmaneura_billing.migrations import _membership_rows_are_unsafe


ROLE_OID = 101
CURRENT_USER_OID = 202
SAFE_CREATOR_EDGE = (ROLE_OID, CURRENT_USER_OID, True, False, False)


def _unsafe(rows) -> bool:
    return _membership_rows_are_unsafe(
        rows,
        role_oid=ROLE_OID,
        current_user_oid=CURRENT_USER_OID,
    )


def test_no_membership_or_one_pg16_creator_admin_edge_is_safe():
    assert _unsafe([]) is False
    assert _unsafe([SAFE_CREATOR_EDGE]) is False


@pytest.mark.parametrize(
    "rows",
    [
        [(303, ROLE_OID, True, False, False)],
        [(ROLE_OID, 303, True, False, False)],
        [(ROLE_OID, CURRENT_USER_OID, False, False, False)],
        [(ROLE_OID, CURRENT_USER_OID, True, True, False)],
        [(ROLE_OID, CURRENT_USER_OID, True, False, True)],
        [SAFE_CREATOR_EDGE, SAFE_CREATOR_EDGE],
    ],
    ids=[
        "runtime-is-member",
        "foreign-inbound-member",
        "missing-admin",
        "inherit-enabled",
        "set-role-enabled",
        "duplicate-creator-edge",
    ],
)
def test_every_other_membership_shape_is_unsafe(rows):
    assert _unsafe(rows) is True
