"""Language-qualified metadata values and the shared display-language resolver.

A :class:`LocalizedValue` pairs a Dublin Core text value with the BCP 47
language tag that applied to it in the source XML (``xml:lang`` resolved by
XML inheritance).  ``language=None`` means the value is *untagged*: either no
``xml:lang`` was declared on any ancestor, or an explicit ``xml:lang=""``
reset the inherited language.  Original tag spelling (including case) is kept
for evidence; all matching compares tags case-insensitively.

The resolver :func:`select_localized` implements the two documented
selection policies used by reports:

``exact``
    1. values whose tag equals the requested tag (case-insensitive);
    2. untagged values;
    3. all source values in document order ("first source value" tier).

``language-range``
    1. values whose tag equals the requested tag;
    2. values whose primary subtag matches the requested primary subtag
       (for example a request for ``zh-CN`` may select a value tagged
       ``zh``);
    3. untagged values;
    4. all source values in document order.

Neither policy changes stored metadata; they only choose which text is
displayed.  For repeated fields every value in the winning tier is kept (in
source order); scalar callers display the first text of the winning tier.
"""

from __future__ import annotations

from typing import Iterable, NamedTuple, Sequence

from .util import collapse_ws

POLICY_EXACT = "exact"
POLICY_LANGUAGE_RANGE = "language-range"
SELECTION_POLICIES = (POLICY_EXACT, POLICY_LANGUAGE_RANGE)

# Fallback tiers, reported so consumers can explain the displayed value.
MATCH_EXACT = "exact"
MATCH_PRIMARY_SUBTAG = "primary-subtag"
MATCH_UNTAGGED = "untagged"
MATCH_FIRST = "first"
MATCH_NONE = "none"


class LocalizedValue(NamedTuple):
    """One Dublin Core value with its resolved ``xml:lang`` (None = untagged)."""

    text: str
    language: str | None = None


class Selection(NamedTuple):
    """Outcome of resolving a preferred display language for one field."""

    texts: tuple[str, ...]
    matched: str
    languages: tuple[str | None, ...]


def _fold(tag: str | None) -> str:
    return (tag or "").casefold()


def _primary_subtag(tag: str | None) -> str:
    return _fold(tag).split("-", 1)[0]


def _coerce_entries(entries: Iterable[Sequence[object]]) -> list[LocalizedValue]:
    values: list[LocalizedValue] = []
    for entry in entries:
        if isinstance(entry, (str, bytes)) or not hasattr(entry, "__len__"):
            continue  # malformed entry, not a (text, language) pair
        text = collapse_ws(entry[0]) if len(entry) >= 1 else ""
        if not text:
            continue
        language = entry[1] if len(entry) > 1 else None
        language = collapse_ws(language) or None
        values.append(LocalizedValue(text, language))
    return values


def select_localized(
    entries: Iterable[Sequence[object]],
    preferred: str | None,
    policy: str = POLICY_EXACT,
) -> Selection:
    """Choose display text(s) for one field under ``policy``.

    ``entries`` are ``(text, language)`` pairs in source order.  Returns a
    :class:`Selection` naming the winning tier so reports can explain the
    fallback decision.  With no ``preferred`` tag the source values pass
    through unchanged (tier ``first``).
    """
    if policy not in SELECTION_POLICIES:
        raise ValueError(f"未知语言选择策略: {policy}")
    values = _coerce_entries(entries)
    if not values:
        return Selection((), MATCH_NONE, ())

    def finish(chosen: list[LocalizedValue], tier: str) -> Selection:
        return Selection(
            tuple(item.text for item in chosen),
            tier,
            tuple(item.language for item in chosen),
        )

    wanted = collapse_ws(preferred)
    if not wanted:
        return finish(values, MATCH_FIRST)
    folded = _fold(wanted)

    exact = [item for item in values if _fold(item.language) == folded]
    if exact:
        return finish(exact, MATCH_EXACT)
    if policy == POLICY_LANGUAGE_RANGE:
        primary = _primary_subtag(wanted)
        ranged = [
            item
            for item in values
            if item.language is not None and _primary_subtag(item.language) == primary
        ]
        if ranged:
            return finish(ranged, MATCH_PRIMARY_SUBTAG)
    untagged = [item for item in values if item.language is None]
    if untagged:
        return finish(untagged, MATCH_UNTAGGED)
    return finish(values, MATCH_FIRST)


def localized_to_json_values(
    mapping: dict[str, tuple[LocalizedValue, ...]],
) -> dict[str, list[list[str | None]]]:
    """Serialize ``{field: (LocalizedValue, ...)}`` to a JSON-ready mapping."""
    return {
        field: [[value.text, value.language] for value in entries]
        for field, entries in sorted(mapping.items())
        if entries
    }


__all__ = [
    "LocalizedValue",
    "Selection",
    "SELECTION_POLICIES",
    "POLICY_EXACT",
    "POLICY_LANGUAGE_RANGE",
    "MATCH_EXACT",
    "MATCH_PRIMARY_SUBTAG",
    "MATCH_UNTAGGED",
    "MATCH_FIRST",
    "MATCH_NONE",
    "select_localized",
    "localized_to_json_values",
]
