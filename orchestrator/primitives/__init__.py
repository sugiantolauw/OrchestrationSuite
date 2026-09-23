from __future__ import annotations

import jsonschema

from orchestrator.primitives import (
    anti_join_gap,
    attribute_missing,
    date_lag,
    duplicate_detection,
    list_membership,
    ratio_per_group,
    split_detection,
    threshold_exceedance,
)
from orchestrator.primitives.common import PrimitiveContext, PrimitiveParamsError, PrimitiveResult

PRIMITIVES: dict[str, object] = {
    "threshold_exceedance": threshold_exceedance,
    "duplicate_detection": duplicate_detection,
    "split_detection": split_detection,
    "anti_join_gap": anti_join_gap,
    "list_membership": list_membership,
    "date_lag": date_lag,
    "ratio_per_group": ratio_per_group,
    "attribute_missing": attribute_missing,
}


def run_primitive(name: str, ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    if name not in PRIMITIVES:
        raise PrimitiveParamsError(f"unknown primitive: {name!r} (known: {sorted(PRIMITIVES)})")
    module = PRIMITIVES[name]
    try:
        jsonschema.validate(params, module.PARAMS_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise PrimitiveParamsError(f"{name}: invalid params: {exc.message}") from exc
    return module.run(ctx, params)


__all__ = ["PRIMITIVES", "run_primitive", "PrimitiveContext", "PrimitiveResult", "PrimitiveParamsError"]
