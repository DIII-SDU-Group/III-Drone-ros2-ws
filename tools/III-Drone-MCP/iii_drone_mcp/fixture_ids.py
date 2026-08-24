from __future__ import annotations

import re
from difflib import get_close_matches
from typing import Iterable


FIXTURE_ID_ALIASES = {
    "inside_low": "low_inside_corridor",
    "low_inside": "low_inside_corridor",
    "inside_high": "high_inside_corridor",
    "high_inside": "high_inside_corridor",
    "entry_low": "low_entry_side",
    "low_entry": "low_entry_side",
    "entry_high": "high_entry_side",
    "high_entry": "high_entry_side",
    "opposite_low": "low_opposite_side",
    "low_opposite": "low_opposite_side",
    "opposite_high": "high_opposite_side",
    "high_opposite": "high_opposite_side",
    "mid_above": "above_mid",
    "above_middle": "above_mid",
    "entry_above": "above_entry_side",
    "above_entry": "above_entry_side",
    "opposite_above": "above_opposite_side",
    "above_opposite": "above_opposite_side",
}


def normalize_fixture_id(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "_", value.strip()).strip("_").lower()
    return re.sub(r"_+", "_", normalized)


def canonical_fixture_id(value: str) -> str:
    normalized = normalize_fixture_id(value)
    return FIXTURE_ID_ALIASES.get(normalized, normalized)


def fixture_id_suggestions(value: str, candidates: Iterable[str], *, limit: int = 5) -> list[str]:
    normalized = normalize_fixture_id(value)
    canonical = canonical_fixture_id(normalized)
    candidate_list = sorted({normalize_fixture_id(candidate) for candidate in candidates if candidate})
    suggestions: list[str] = []
    if canonical in candidate_list:
        suggestions.append(canonical)
    suggestions.extend(get_close_matches(normalized, candidate_list, n=limit, cutoff=0.45))

    seen: set[str] = set()
    unique: list[str] = []
    for suggestion in suggestions:
        if suggestion in seen:
            continue
        seen.add(suggestion)
        unique.append(suggestion)
        if len(unique) >= limit:
            break
    return unique
