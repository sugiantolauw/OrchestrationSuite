"""Word/phrase lexicons and per-field constants for the narration validator
(P6 narration design §3.3-§3.4). Pure data plus small scanning helpers --
no business logic about a specific run's metrics lives here.

Every regex is case-insensitive and word/phrase-bounded with `\\b`, so e.g.
the vague-magnitude ban on "rare" does not also flag "prepare".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "FIELD_LENGTH_CAPS",
    "OBSERVATION_TYPE_FIELDS",
    "TITLE_FIELDS",
    "RATIONALE_FIELD",
    "COUNT_NOUNS",
    "LexiconHit",
    "find_number_words",
    "find_ordinal_words",
    "find_vague_magnitude_words",
    "find_universal_quantifiers",
    "find_causation_language",
    "find_policy_assertions",
    "find_causal_connectives",
    "find_hedge_markers",
    "find_code_like_text",
    "find_count_noun_after",
    "find_currency_symbols",
    "find_percent_words",
]


@dataclass(frozen=True)
class LexiconHit:
    """One match of a banned word/phrase: `text` is the exact matched
    substring (for error messages), `start`/`end` its span in the text that
    was scanned (post placeholder-stripping, unless noted otherwise)."""

    text: str
    start: int
    end: int


def _find_all(pattern: re.Pattern[str], text: str) -> list[LexiconHit]:
    return [LexiconHit(m.group(0), m.start(), m.end()) for m in pattern.finditer(text)]


# --------------------------------------------------------------------------
# §3.4 N-L1: length caps in characters, by field key.
# --------------------------------------------------------------------------
FIELD_LENGTH_CAPS: dict[str, int] = {
    "observation": 900,
    "recommendation": 600,
    "question": 250,
    "theme_title": 80,
    "theme_summary": 600,
    "root_cause": 400,
    "review_observation": 300,
    "rationale": 220,  # priority rationale
    "exec_paragraph": 700,
    "caption": 200,
    "candidate_title": 100,
    "profile_paragraph": 600,
}

# §3.4 N-Q1/N-Q2 scope: "observation-type fields".
OBSERVATION_TYPE_FIELDS: frozenset[str] = frozenset(
    {
        "observation",
        "theme_summary",
        "root_cause",
        "review_observation",
        "exec_paragraph",
        "caption",
        "profile_paragraph",
        "rationale",
    }
)

# §3.4 N-L2 scope: titles carry no placeholders.
TITLE_FIELDS: frozenset[str] = frozenset({"theme_title", "candidate_title"})

# §3.3 N-D3 scope: ordinal/ranking words are rejected only in priority rationale.
RATIONALE_FIELD = "rationale"

# §3.4 N-U2: count nouns that may not immediately follow a money/pct placeholder.
COUNT_NOUNS: frozenset[str] = frozenset(
    {
        "claim",
        "claims",
        "report",
        "reports",
        "booking",
        "bookings",
        "employee",
        "employees",
        "transaction",
        "transactions",
        "line",
        "lines",
        "item",
        "items",
        "request",
        "requests",
        "day",
        "days",
    }
)
_COUNT_NOUN_AFTER_RE = re.compile(
    r"^\s*(" + "|".join(sorted(COUNT_NOUNS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# §3.3 N-D2: number words. Bare "one" and "single" are deliberately absent --
# the spec allows them standalone; only the phrases below ban a numeric
# reading of "one".
# --------------------------------------------------------------------------
_CARDINAL_0_19_NO_ONE = (
    "zero two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_ONES_1_9 = "one two three four five six seven eight nine".split()

_NUMBER_WORD_RE = re.compile(
    r"\b("
    + "|".join(_CARDINAL_0_19_NO_ONE)
    + r"|(?:" + "|".join(_TENS) + r")(?:-(?:" + "|".join(_ONES_1_9) + r"))?"
    + r"|hundreds?|thousands?|millions?|billions?|dozens?"
    + r"|halves|half|quarters?"
    + r"|doubled?|twice|tripled?|treble"
    + r"|\w+fold"
    + r")\b",
    re.IGNORECASE,
)
_NUMBER_PHRASE_RE = re.compile(
    r"\b(one in|one of every|one out of|a third of|two-thirds)\b", re.IGNORECASE
)


def find_number_words(text: str) -> list[LexiconHit]:
    return _find_all(_NUMBER_WORD_RE, text) + _find_all(_NUMBER_PHRASE_RE, text)


# --------------------------------------------------------------------------
# §3.3 N-D3: ordinal/ranking words -- priority rationale only.
# --------------------------------------------------------------------------
_ORDINAL_RE = re.compile(
    r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth"
    r"|top|bottom|highest|lowest|largest|smallest|greatest|least)\b",
    re.IGNORECASE,
)


def find_ordinal_words(text: str) -> list[LexiconHit]:
    return _find_all(_ORDINAL_RE, text)


# --------------------------------------------------------------------------
# §3.4 N-Q1: vague magnitude words.
# --------------------------------------------------------------------------
_VAGUE_MAGNITUDE_RE = re.compile(
    r"\b(many|numerous|most|majority|minority|few|handful|several"
    r"|widespread|pervasive|systemic"
    r"|significant(?:ly)?|substantial(?:ly)?|material(?:ly)?"
    r"|vast|overwhelming(?:ly)?|rampant|endemic"
    r"|frequent(?:ly)?|rare(?:ly)?)\b",
    re.IGNORECASE,
)


def find_vague_magnitude_words(text: str) -> list[LexiconHit]:
    return _find_all(_VAGUE_MAGNITUDE_RE, text)


# --------------------------------------------------------------------------
# §3.4 N-Q2: universal quantifiers.
# --------------------------------------------------------------------------
_UNIVERSAL_QUANTIFIER_RE = re.compile(
    r"\b(all|every|none|entire(?:ly)?|always|never)\b", re.IGNORECASE
)


def find_universal_quantifiers(text: str) -> list[LexiconHit]:
    return _find_all(_UNIVERSAL_QUANTIFIER_RE, text)


# --------------------------------------------------------------------------
# §3.4 N-S1: intent/causation language. Independent narration-content review
# 2026-09-25 (live example: T6.1d's "could suggest bulk personal purchases")
# added "personal purchase(s)" here, in the blanket, NO-hedge-exception list,
# not the hedge-aware N-S4 connectives below: attributing a specific
# transaction to the claimant's PERSONAL (non-business) use is a judgment
# about that person's conduct, the same category as "fraud"/"intentional" --
# softening it with "could suggest" does not make it an appropriate thing for
# a model to write into a workpaper. Contrast "elevated risk of misuse", a
# standard, hedged audit-risk phrase this codebase's own live output also
# produced (T4.4) and which is correctly NOT banned here -- see N-S4 instead
# for the "X results in/caused Y" shape that phrase does not use.
# --------------------------------------------------------------------------
_CAUSATION_RE = re.compile(
    r"\b(fraud\w*|deliberate\w*|intentional\w*|on purpose|circumvent\w*"
    r"|evad\w*|evasion|conceal\w*|misconduct|dishonest\w*|theft|steal\w*"
    r"|abuse\w*|collu\w*|manipulat\w*|personal purchases?)\b",
    re.IGNORECASE,
)


def find_causation_language(text: str) -> list[LexiconHit]:
    return _find_all(_CAUSATION_RE, text)


# --------------------------------------------------------------------------
# G12-lite / N-S4 (independent narration-content review 2026-09-25, live
# examples: T5.2 "Duplicate claims result in ... direct financial loss" and
# T3.2a "... thereby increasing travel costs"): a causal CONNECTIVE asserting
# that one thing in this run's data definitely produced another is banned
# UNLESS the same sentence also carries a hedge marker (§3.4's own
# CLAUDE.md-quoted rule: "A cause is a hypothesis, never a fact"). Unlike
# N-S1 above, this rule is hedge-AWARE by design: "may result in a loss" and
# "could indicate a risk of increased travel costs" are legitimate audit
# prose this must not flag, so the connective and the hedge marker are two
# separate finders -- `validate.py` decides, per sentence, whether a
# connective hit is accompanied by a hedge marker anywhere in that same
# sentence (mirroring how N-Q2's universal-quantifier check already works
# per-sentence against a rendered percentage) before raising N-S4. This is
# deliberately narrower than banning "cause" outright: "root cause
# hypothesis" is this system's OWN required synthesis vocabulary (§4.6,
# synthesis_user.md) and must never trip this rule -- only the verb forms
# "caused"/"causes" do, never the bare noun "cause".
# --------------------------------------------------------------------------
_CAUSAL_CONNECTIVE_RE = re.compile(
    r"\b(result(?:s|ed)?\s+in|thereby|caused|causes|leads?\s+to|led\s+to)\b",
    re.IGNORECASE,
)
_HEDGE_MARKER_RE = re.compile(
    r"\b(may|might|could|possibly|potential(?:ly)?|risk\s+of|indicat\w*\s+a\s+risk\s+of)\b",
    re.IGNORECASE,
)


def find_causal_connectives(text: str) -> list[LexiconHit]:
    return _find_all(_CAUSAL_CONNECTIVE_RE, text)


def find_hedge_markers(text: str) -> list[LexiconHit]:
    return _find_all(_HEDGE_MARKER_RE, text)


# --------------------------------------------------------------------------
# §3.4 N-S2: policy assertions.
# --------------------------------------------------------------------------
_POLICY_RE = re.compile(
    r"\b(policy requires|policy states|breach\w*|violat\w*|non-compliant with policy)\b",
    re.IGNORECASE,
)


def find_policy_assertions(text: str) -> list[LexiconHit]:
    return _find_all(_POLICY_RE, text)


# --------------------------------------------------------------------------
# §3.4 N-S3: code-like text.
# --------------------------------------------------------------------------
_CODE_LIKE_RE = re.compile(
    r"(https?://|\bhttp\b|SELECT\s|\bimport\s|\blambda\b|__|exec\(|eval\(|```|<script)",
    re.IGNORECASE,
)


def find_code_like_text(text: str) -> list[LexiconHit]:
    return _find_all(_CODE_LIKE_RE, text)


# --------------------------------------------------------------------------
# §3.4 N-U2 helper: is the text immediately following a placeholder span a
# banned count noun? `pct` followed by "of" is explicitly fine (spec), so
# the caller only invokes this for the cases that are not "... of ...".
# --------------------------------------------------------------------------
def find_count_noun_after(text_after: str) -> LexiconHit | None:
    m = _COUNT_NOUN_AFTER_RE.match(text_after)
    if not m:
        return None
    return LexiconHit(m.group(1), m.start(1), m.end(1))


# --------------------------------------------------------------------------
# §3.3 N-D1 helpers: currency symbols and "per cent"/"percent" words. Digit
# scanning itself lives in validate.py (it needs the per-run identifier
# allow-list, which is item-specific, not lexicon-wide data).
# --------------------------------------------------------------------------
_CURRENCY_SYMBOL_RE = re.compile(r"[$€£¥]")
_PERCENT_WORD_RE = re.compile(r"\b(per\s?cent|percent)\b", re.IGNORECASE)


def find_currency_symbols(text: str) -> list[LexiconHit]:
    return _find_all(_CURRENCY_SYMBOL_RE, text)


def find_percent_words(text: str) -> list[LexiconHit]:
    return _find_all(_PERCENT_WORD_RE, text)
