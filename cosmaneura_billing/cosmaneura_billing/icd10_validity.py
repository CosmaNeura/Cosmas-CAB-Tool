"""Affirmative current-catalogue billability for engine and human outcomes."""
from __future__ import annotations

from functools import lru_cache

from .knowledge.icd10_ccsr import ICD10CCSR


def billability(code: str, catalogue) -> bool | None:
    """True permits use; False is invalid; None means validation unavailable."""
    if catalogue is None or getattr(catalogue, "has_current_fy", False) is not True:
        return None
    result = catalogue.is_active(code)
    return result if type(result) is bool else None


@lru_cache(maxsize=1)
def _current_catalogue():
    catalogue = ICD10CCSR()
    if catalogue.has_current_fy is not True:
        # An unavailable catalogue is not cached as an affirmative lookup.
        raise ValueError("current ICD-10 catalogue unavailable")
    return catalogue


def valid_human_icd10(code: str) -> bool:
    """Human edits require valid codes, without adjudicating clinical meaning."""
    try:
        return billability(code, _current_catalogue()) is True
    except (OSError, ValueError):
        return False
