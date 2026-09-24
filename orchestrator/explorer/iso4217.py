"""Pinned ISO-4217 alphabetic currency codes (docs/specs/P6_P8_explorer_llm_
design.md §4.3: "checked locally against a pinned list ... never a network
lookup"). Used only to help classify a string column's `semantic_type` as
`currency_code` during Explorer profiling -- it never decides a run's
currency (CLAUDE.md §0.5's contract-level currency rule is unrelated and
unaffected by this list)."""

from __future__ import annotations

# Active ISO-4217 codes as of this writing. A code missing from this list is
# classified conservatively (not `currency_code`) rather than guessed --
# under-classifying only costs the planner a slightly less specific semantic
# type, never a wrong currency figure.
ISO4217_CODES: frozenset[str] = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN
    BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD
    CAD CDF CHF CLP CNY COP CRC CUP CVE CZK
    DJF DKK DOP DZD
    EGP ERN ETB EUR
    FJD FKP
    GBP GEL GHS GIP GMD GNF GTQ GYD
    HKD HNL HTG HUF
    IDR ILS INR IQD IRR ISK
    JMD JOD JPY
    KES KGS KHR KMF KPW KRW KWD KYD KZT
    LAK LBP LKR LRD LSL LYD
    MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN
    NAD NGN NIO NOK NPR NZD
    OMR
    PAB PEN PGK PHP PKR PLN PYG
    QAR
    RON RSD RUB RWF
    SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SYP SZL
    THB TJS TMT TND TOP TRY TTD TWD TZS
    UAH UGX USD UYU UZS
    VES VND VUV
    WST
    XAF XCD XOF XPF
    YER
    ZAR ZMW ZWL
    """.split()
)
