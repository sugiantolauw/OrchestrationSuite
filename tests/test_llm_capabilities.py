"""orchestrator.llm.capabilities (independent review 2026-09-24 item 3):
never send an untested parameter -- only a param recorded exactly
"supported" for a role is ever sent; everything else is withheld with a
reason."""

from __future__ import annotations

from orchestrator.llm.capabilities import (
    filter_params,
    load_capabilities,
    role_config,
    schema_keyword_allowed,
    served_model_matches,
)

_CAPS = {
    "roles": {
        "model_gpt_oss": {
            "verified_on": "2026-09-23",
            "served_model_prefix": "gpt-oss-120b",
            "params": {
                "max_tokens": "supported",
                "temperature": "supported",
                "json_schema_strict": "supported",
                "seed": "rejected",
                "json_object": "requires_json_word",
            },
            "schema_keywords_rejected": ["pattern"],
        },
        "model_sonnet": {
            "verified_on": None,
            "served_model_prefix": None,
            "params": {},
        },
    }
}


def test_load_capabilities_reads_the_real_shipped_file():
    caps = load_capabilities()
    assert "model_gpt_oss" in caps["roles"]
    assert "model_sonnet" in caps["roles"]
    assert caps["roles"]["model_gpt_oss"]["params"]["max_tokens"] == "supported"
    assert caps["roles"]["model_sonnet"]["params"] == {}


def test_filter_params_sends_only_supported():
    sent, dropped = filter_params(_CAPS, "model_gpt_oss", {"max_tokens": 100, "temperature": 0})
    assert sent == {"max_tokens": 100, "temperature": 0}
    assert dropped == {}


def test_filter_params_withholds_rejected_with_its_reason():
    sent, dropped = filter_params(_CAPS, "model_gpt_oss", {"max_tokens": 100, "seed": 7})
    assert sent == {"max_tokens": 100}
    assert dropped == {"seed": "rejected"}


def test_filter_params_withholds_unlisted_as_untested():
    sent, dropped = filter_params(_CAPS, "model_gpt_oss", {"top_k": 5})
    assert sent == {}
    assert dropped == {"top_k": "untested"}


def test_filter_params_withholds_everything_for_an_unverified_role():
    sent, dropped = filter_params(_CAPS, "model_sonnet", {"max_tokens": 100, "temperature": 0})
    assert sent == {}
    assert dropped == {"max_tokens": "untested", "temperature": "untested"}


def test_filter_params_for_a_role_absent_from_the_matrix_entirely():
    sent, dropped = filter_params(_CAPS, "model_unknown", {"max_tokens": 100})
    assert sent == {}
    assert dropped == {"max_tokens": "untested"}


def test_role_config_returns_untested_stand_in_for_unknown_role():
    cfg = role_config(_CAPS, "model_unknown")
    assert cfg["params"] == {}
    assert cfg["verified_on"] is None


def test_schema_keyword_allowed():
    assert not schema_keyword_allowed(_CAPS, "model_gpt_oss", "pattern")
    assert schema_keyword_allowed(_CAPS, "model_gpt_oss", "enum")


def test_served_model_matches_prefix():
    assert served_model_matches(_CAPS, "model_gpt_oss", "gpt-oss-120b-080525")
    assert not served_model_matches(_CAPS, "model_gpt_oss", "claude-sonnet-5-somethingelse")


def test_served_model_matches_true_when_never_verified():
    # Nothing to mismatch against yet -- an unverified role's params are
    # already empty, so served_model_matches itself stays permissive.
    assert served_model_matches(_CAPS, "model_sonnet", "anything-at-all")
