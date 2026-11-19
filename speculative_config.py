"""Speculative decoding configuration schema and plain-load fallback policy.

This module provides strict validation for the optional speculative decoding
configuration that may appear on registry machines and lanes. The feature is
default OFF: missing or omitted configuration resolves to disabled.

Schema (on machine or lane):
    "speculative": {
      "enabled": false,           # default false; feature is off unless true
      "backend": "llama.cpp",     # required when enabled: "llama.cpp" | "lmstudio-dflash"
      "type": "draft-dflash",     # optional: "draft-dflash" | "draft-simple"
      "modelKey": "...",          # exactly one of modelKey or modelPath required when enabled
      "modelPath": "...",         # local path string; not validated for existence
      "nMax": 8,                  # optional: 1 <= nMin <= nMax <= 32
      "nMin": 1,
      "deviceDraft": "cpu",       # optional: non-empty string
      "threadsDraft": 4,          # optional: >= 1
      "gpuLayersDraft": 0,        # optional: >= 0
      "fallback": "plain"         # optional: only "plain" allowed; defaults to "plain"
    }

Lane-level config overrides machine-level defaults. Field inheritance is shallow:
if a lane has any speculative object, it does not inherit individual fields from
the machine's speculative object.
"""

from __future__ import annotations

_BACKENDS = frozenset({"llama.cpp", "lmstudio-dflash"})
_TYPES = frozenset({"draft-dflash", "draft-simple"})
_FALLBACKS = frozenset({"plain"})

_SPECULATIVE_FIELDS = frozenset(
    {
        "enabled",
        "backend",
        "type",
        "modelKey",
        "modelPath",
        "nMax",
        "nMin",
        "deviceDraft",
        "threadsDraft",
        "gpuLayersDraft",
        "fallback",
    }
)


def _invalid_spec(machine_id, field, value, rule, lane_id=None):
    """Raise ValueError with enough context to repair a speculative config entry."""
    location = f"machine {machine_id!r}"
    if lane_id is not None:
        location += f", lane {lane_id!r}"
    raise ValueError(f"{location}, speculative field {field!r}, value {value!r}: {rule}")


def _validate_enum_field(spec, machine_id, lane_id, field, allowed):
    """Validate that a field, if present, is one of the allowed values."""
    value = spec.get(field)
    if value is not None and value not in allowed:
        _invalid_spec(machine_id, field, value, f"must be one of {sorted(allowed)}", lane_id)
    return value


def _validate_nonempty_string(spec, machine_id, lane_id, field):
    """Validate that a field, if present, is a non-empty string."""
    value = spec.get(field)
    if value is not None and (not isinstance(value, str) or not value):
        _invalid_spec(machine_id, field, value, "must be a non-empty string", lane_id)
    return value


def _validate_bounded_int(spec, machine_id, lane_id, field, low, high):
    """Validate that a field, if present, is an integer in [low, high]."""
    value = spec.get(field)
    if value is not None and (type(value) is not int or not low <= value <= high):
        _invalid_spec(machine_id, field, value, f"must be an integer from {low} to {high}", lane_id)
    return value


def _validate_speculative_fields(spec, machine_id, lane_id):
    """Validate individual fields of the speculative config."""
    enabled = spec.get("enabled", False)
    if type(enabled) is not bool:
        _invalid_spec(machine_id, "enabled", enabled, "must be a boolean", lane_id)

    backend = _validate_enum_field(spec, machine_id, lane_id, "backend", _BACKENDS)
    _validate_enum_field(spec, machine_id, lane_id, "type", _TYPES)

    model_key = _validate_nonempty_string(spec, machine_id, lane_id, "modelKey")
    model_path = _validate_nonempty_string(spec, machine_id, lane_id, "modelPath")

    n_max = _validate_bounded_int(spec, machine_id, lane_id, "nMax", 1, 32)
    n_min = _validate_bounded_int(spec, machine_id, lane_id, "nMin", 1, 32)
    if n_max is not None and n_min is not None and n_min > n_max:
        _invalid_spec(machine_id, "nMin", n_min, "must not exceed nMax", lane_id)

    _validate_nonempty_string(spec, machine_id, lane_id, "deviceDraft")

    threads_draft = spec.get("threadsDraft")
    if threads_draft is not None and (type(threads_draft) is not int or threads_draft < 1):
        _invalid_spec(machine_id, "threadsDraft", threads_draft, "must be an integer >= 1", lane_id)

    gpu_layers_draft = spec.get("gpuLayersDraft")
    if gpu_layers_draft is not None and (type(gpu_layers_draft) is not int or gpu_layers_draft < 0):
        _invalid_spec(machine_id, "gpuLayersDraft", gpu_layers_draft, "must be a nonnegative integer", lane_id)

    _validate_enum_field(spec, machine_id, lane_id, "fallback", _FALLBACKS)

    return enabled, backend, model_key, model_path


def validate_speculative(spec, machine_id, lane_id=None):
    """Validate a speculative config object; raises ValueError on invalid input.

    Returns the validated spec unchanged if valid. When enabled is true, enforces
    that backend is present and exactly one of modelKey/modelPath is set.
    """
    if spec is None:
        return None
    if not isinstance(spec, dict):
        _invalid_spec(machine_id, "speculative", spec, "must be an object", lane_id)

    for key in sorted(set(spec) - _SPECULATIVE_FIELDS, key=repr):
        _invalid_spec(machine_id, key, spec[key], "field is not allowed", lane_id)

    enabled, backend, model_key, model_path = _validate_speculative_fields(spec, machine_id, lane_id)

    if enabled:
        if backend is None:
            _invalid_spec(machine_id, "backend", None, "required when enabled", lane_id)
        has_key = model_key is not None
        has_path = model_path is not None
        if has_key == has_path:
            if has_key:
                _invalid_spec(machine_id, "modelKey", model_key, "cannot specify both modelKey and modelPath", lane_id)
            else:
                _invalid_spec(machine_id, "modelKey", None, "exactly one of modelKey or modelPath is required", lane_id)

    return spec


def effective_speculative(machine_spec, lane_spec):
    """Return the effective speculative config for a lane.

    If the lane has a speculative object, it is used directly (no field-level
    inheritance from machine). Otherwise the machine's speculative object is
    used. Returns None if neither is set.
    """
    if lane_spec is not None:
        return lane_spec
    return machine_spec


def normalize_speculative(spec):
    """Return a normalized copy of the speculative config with defaults applied.

    Returns a dict with at least {"enabled": bool, "fallback": "plain"}.
    If spec is None or empty, returns {"enabled": False, "fallback": "plain"}.
    """
    if spec is None:
        return {"enabled": False, "fallback": "plain"}
    result = dict(spec)
    result.setdefault("enabled", False)
    result.setdefault("fallback", "plain")
    return result


def resolve_lane_speculative(machine, lane):
    """Return the normalized effective speculative config for a lane.

    Combines machine and lane configs, applies defaults, and returns a dict.
    Does NOT validate; call validate_speculative first on each object.
    """
    machine_spec = machine.get("speculative") if isinstance(machine, dict) else None
    lane_spec = lane.get("speculative") if isinstance(lane, dict) else None
    effective = effective_speculative(machine_spec, lane_spec)
    return normalize_speculative(effective)


def resolve_speculative_plan(effective_config, *, draft_present=None, vocab_compatible=None, load_error=None):
    """Determine whether to use speculative decoding or fall back to plain load.

    Args:
        effective_config: Normalized speculative config dict (from normalize_speculative).
        draft_present: True if draft model is available, False if missing, None if unknown.
        vocab_compatible: True if vocab matches, False if mismatch, None if unknown.
        load_error: Truthy if a load failure occurred, None/falsy otherwise.

    Returns:
        (mode, reason) tuple:
        - ("plain", "disabled") when feature is off
        - ("plain", "missing_draft") when draft is not present
        - ("plain", "vocab_mismatch") when vocab is incompatible
        - ("plain", "load_failure") when load failed
        - ("speculative", None) when enabled and checks pass or are unknown
    """
    if effective_config is None:
        return ("plain", "disabled")

    enabled = effective_config.get("enabled", False)
    if not enabled:
        return ("plain", "disabled")

    if draft_present is False:
        return ("plain", "missing_draft")

    if vocab_compatible is False:
        return ("plain", "vocab_mismatch")

    if load_error:
        return ("plain", "load_failure")

    return ("speculative", None)
