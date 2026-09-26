from __future__ import annotations

import re

_ASSET_KEY = re.compile(r"^(?:char|scene|prop)_[A-Za-z0-9_]+$")
_ASSET_KEY_IN_TEXT = re.compile(r"\b(?:char|scene|prop)_[A-Za-z0-9_]+\b")
_COMBO_TOKEN = re.compile(r"\bcombo__[A-Za-z0-9_]+\b")
_PLUS = re.compile(r"[+＋]")


def canonical_combination_key(source_ids: list[str] | tuple[str, ...]) -> str:
    return "combo__" + "__".join(str(x).strip() for x in source_ids if str(x).strip())


def source_ids_from_combination_key(value: str) -> list[str]:
    token = str(value or "").strip()
    if not token.startswith("combo__"):
        return []
    parts = [part.strip() for part in token.split("__")[1:] if part.strip()]
    if len(parts) < 2 or not all(_ASSET_KEY.fullmatch(part) for part in parts):
        return []
    return parts


def extract_explicit_reference_combinations(text: str) -> list[list[str]]:
    """Return only explicitly requested canonical relationship combinations.

    Two generic, machine-readable/user-readable forms are supported:
    - plus-delimited canonical relationships: ``char_a + scene_b + prop_c``
    - canonical combination IDs: ``combo__char_a__scene_b__prop_c``

    Category rollups, prose lists, counts and comma-separated asset mentions are
    deliberately ignored so they cannot accidentally create combinations.
    """
    result: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def add(parts: list[str]) -> None:
        signature = tuple(parts)
        if len(signature) < 2 or signature in seen:
            return
        seen.add(signature)
        result.append(list(signature))

    raw = str(text or "")

    # Canonical combo IDs are the safest structured form and may appear anywhere
    # in a line (bullets, acceptance criteria, generated plans, etc.).
    for token in _COMBO_TOKEN.findall(raw):
        parts = source_ids_from_combination_key(token)
        if parts:
            add(parts)

    # Human-authored relation form. Requiring a plus sign and canonical asset keys
    # avoids interpreting generic prose/category summaries as relation requests.
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or not _PLUS.search(line):
            continue
        keys: list[str] = []
        for match in _ASSET_KEY_IN_TEXT.findall(line):
            if match not in keys:
                keys.append(match)
        if len(keys) >= 2:
            add(keys)

    return result
