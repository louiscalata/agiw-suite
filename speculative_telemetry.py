"""Normalize speculative decoding evidence from llama-server and journal records."""

from typing import Any, Callable, Optional

FALLBACK_REASONS = frozenset({"disabled", "missing_draft", "vocab_mismatch", "load_failure"})


def _bounded_number(value: Any, maximum: float) -> Optional[float]:
    if type(value) in (int, float) and 0 <= value <= maximum:
        return float(value)
    return None


def _speed(value: Any) -> Optional[float]:
    return _bounded_number(value, 1e6)


def _count(value: Any) -> Optional[int]:
    return value if type(value) is int and 0 <= value <= 2**31 - 1 else None


def _first_valid(data: dict, parse: Callable, *keys: str) -> Any:
    for key in keys:
        value = parse(data.get(key))
        if value is not None:
            return value
    return None


def _draft_counts(data: dict) -> dict:
    return {
        "draftN": _first_valid(data, _count, "draft_n", "n_draft", "n_drafted"),
        "draftNAccepted": _first_valid(data, _count, "draft_n_accepted", "n_draft_accepted", "n_accepted"),
    }


def _with_draft_rate(fields: dict) -> dict:
    drafted, accepted = fields["draftN"], fields["draftNAccepted"]
    rate = accepted / drafted if drafted and accepted is not None and accepted <= drafted else None
    return {**fields, "draftAcceptanceRate": rate}


def parse_llama_timings(timings: Any) -> dict:
    """Project independently validated timings and draft counts."""
    data = timings if isinstance(timings, dict) else {}
    return _with_draft_rate(
        {
            "predictedPerSecond": _first_valid(data, _speed, "predicted_per_second", "predictedPerSecond"),
            "promptPerSecond": _first_valid(data, _speed, "prompt_per_second", "promptPerSecond"),
            **_draft_counts(data),
        }
    )


def parse_slot_speculative(raw_slot: Any) -> dict:
    """Read nested slot counts, falling back to counts on the slot itself."""
    slot = raw_slot if isinstance(raw_slot, dict) else {}
    nested = slot.get("speculative")
    fields = _draft_counts(nested if isinstance(nested, dict) else {})
    for key, value in _draft_counts(slot).items():
        if fields[key] is None:
            fields[key] = value
    return _with_draft_rate(fields)


def normalize_fallback_reason(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in FALLBACK_REASONS else None


def receipt_speculative_fields(*, timings: Any = None, record: Any = None, fallback_reason: Any = None) -> dict:
    """Use explicit timings first, then journaled timings and projected fields."""
    record = record if isinstance(record, dict) else {}
    parsed, nested = parse_llama_timings(timings), parse_llama_timings(record.get("timings"))
    fields = {}
    for key, validate in (("predictedPerSecond", _speed), ("draftN", _count), ("draftNAccepted", _count)):
        value = parsed[key] if parsed[key] is not None else nested[key]
        fields[key] = value if value is not None else validate(record.get(key))
    fields = _with_draft_rate(fields)
    if fields["draftN"] != 0 and None in (fields["draftN"], fields["draftNAccepted"]):
        fields["draftAcceptanceRate"] = _bounded_number(record.get("draftAcceptanceRate"), 1.0)
    reason = fallback_reason if fallback_reason is not None else record.get("fallbackReason")
    fields["fallbackReason"] = normalize_fallback_reason(reason)
    return fields
