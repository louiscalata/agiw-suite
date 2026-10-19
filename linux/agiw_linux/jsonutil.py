"""Strict, bounded JSON parsing shared by local and reported status readers."""
from __future__ import annotations

import json
import math
from typing import Any

JSON_MAX_DEPTH = 64


def _reject_constant(token: str):
    raise ValueError(f"non-finite JSON number {token}")


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def loads_strict(text: str) -> Any:
    """Refuse non-finite numbers, duplicate keys and more than 64 nested containers."""
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > JSON_MAX_DEPTH:
                raise ValueError("JSON nested too deeply")
        elif char in "]}":
            depth -= 1
    try:
        return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float,
                          object_pairs_hook=_unique_object)
    except RecursionError as error:
        raise ValueError("JSON nested too deeply") from error
