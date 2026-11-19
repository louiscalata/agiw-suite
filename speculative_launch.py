"""Per-backend launch and load hooks for speculative decoding.

This module provides pure functions that generate backend-specific flags and
fields from a normalized speculative config. The feature is default OFF: when
the plan is not speculative, functions return empty lists and do not alter
load behavior.

llama.cpp: command-line args (--spec-*) for draft model configuration.
LM Studio DFlash: load config fields for the SDK's loadConfigStack channel.
"""

from __future__ import annotations

from speculative_config import resolve_speculative_plan


def llama_cpp_draft_args(effective_config: dict | None) -> list[str]:
    """Return llama.cpp CLI args for speculative decoding, or empty if disabled.

    When the plan is not speculative or the backend is not llama.cpp, returns [].
    When enabled with backend llama.cpp, emits the current --spec-* flags.

    Args:
        effective_config: Normalized speculative config from normalize_speculative.

    Returns:
        List of string arguments for llama-server or llama.cpp CLI.
    """
    if effective_config is None:
        return []

    mode, _reason = resolve_speculative_plan(effective_config)
    if mode != "speculative":
        return []

    backend = effective_config.get("backend")
    if backend != "llama.cpp":
        return []

    args = []

    model_path = effective_config.get("modelPath")
    model_key = effective_config.get("modelKey")
    if model_path:
        args.extend(["--spec-draft-model", model_path])
    elif model_key:
        args.extend(["--spec-draft-model", model_key])

    n_max = effective_config.get("nMax")
    if n_max is not None:
        args.extend(["--spec-draft-n-max", str(n_max)])

    n_min = effective_config.get("nMin")
    if n_min is not None:
        args.extend(["--spec-draft-n-min", str(n_min)])

    spec_type = effective_config.get("type")
    if spec_type is not None:
        args.extend(["--spec-type", spec_type])

    device_draft = effective_config.get("deviceDraft")
    if device_draft is not None:
        args.extend(["--spec-draft-device", device_draft])

    gpu_layers_draft = effective_config.get("gpuLayersDraft")
    if gpu_layers_draft is not None:
        args.extend(["--spec-draft-ngl", str(gpu_layers_draft)])

    return args


def lmstudio_dflash_load_fields(effective_config: dict | None) -> list[dict]:
    """Return LM Studio load config fields for speculative decoding.

    When the plan is not speculative or the backend is not lmstudio-dflash,
    returns []. The returned list contains {key, value} dicts suitable for
    the loadConfigStack field list in the LM Studio SDK load channel.

    Args:
        effective_config: Normalized speculative config from normalize_speculative.

    Returns:
        List of {key: str, value: ...} dicts for speculative decoding fields only.
    """
    if effective_config is None:
        return []

    mode, _reason = resolve_speculative_plan(effective_config)
    if mode != "speculative":
        return []

    backend = effective_config.get("backend")
    if backend != "lmstudio-dflash":
        return []

    fields = []
    spec_type = effective_config.get("type")

    if spec_type == "draft-simple":
        fields.append({"key": "llm.load.speculativeDecoding.draftSimple", "value": True})
        fields.append({"key": "llm.load.speculativeDecoding.draftDflashSidecar", "value": False})
    else:
        fields.append({"key": "llm.load.speculativeDecoding.draftDflashSidecar", "value": True})
        fields.append({"key": "llm.load.speculativeDecoding.draftSimple", "value": False})

    # Explicitly disable other draft kinds that are not supported by this config
    fields.append({"key": "llm.load.speculativeDecoding.draftMtp", "value": False})
    fields.append({"key": "llm.load.speculativeDecoding.draftDspark", "value": False})
    fields.append({"key": "llm.load.speculativeDecoding.draftDsparkSidecar", "value": False})
    fields.append({"key": "llm.load.speculativeDecoding.draftMtpSidecar", "value": False})

    model_key = effective_config.get("modelKey")
    model_path = effective_config.get("modelPath")
    if model_key:
        fields.append({"key": "llm.load.speculativeDecoding.draftModel", "value": model_key})
    elif model_path:
        fields.append({"key": "llm.load.speculativeDecoding.draftModel", "value": model_path})

    return fields


def load_with_optional_draft(
    action: str,
    model_id: str,
    effective_config: dict | None,
    *,
    run_plain,
    run_dflash=None,
) -> tuple[str, str | None]:
    """Execute a model action with optional DFlash speculative load and fallback.

    For unload and non-load actions, always calls run_plain and ignores draft config.
    For load actions:
    - If the plan is not speculative, calls run_plain only.
    - If the plan is speculative with lmstudio-dflash backend:
      - Attempts run_dflash with generated load fields if available.
      - On any failure, falls back to run_plain.
    - Other speculative backends (llama.cpp) call run_plain only since they use
      args, not SDK load fields.

    Args:
        action: "load" or "unload".
        model_id: The model identifier.
        effective_config: Normalized speculative config (or None).
        run_plain: Callable to perform standard load/unload (required).
        run_dflash: Callable(fields) for DFlash load attempt (optional).

    Returns:
        (mode, reason) tuple:
        - ("plain", None) for unload, non-speculative load, or non-dflash backend.
        - ("dflash", None) for successful DFlash load.
        - ("plain", "load_failure") when DFlash failed or run_dflash was not provided.
    """
    if action != "load":
        run_plain(action, model_id)
        return ("plain", None)

    if effective_config is None:
        run_plain(action, model_id)
        return ("plain", None)

    mode, reason = resolve_speculative_plan(effective_config)
    if mode != "speculative":
        run_plain(action, model_id)
        return ("plain", None)

    backend = effective_config.get("backend")
    if backend != "lmstudio-dflash":
        run_plain(action, model_id)
        return ("plain", None)

    if run_dflash is None:
        run_plain(action, model_id)
        return ("plain", "load_failure")

    fields = lmstudio_dflash_load_fields(effective_config)
    try:
        run_dflash(action, model_id, fields)
        return ("dflash", None)
    except Exception:  # noqa: BLE001 - SDK boundary; any failure falls back to plain load
        run_plain(action, model_id)
        return ("plain", "load_failure")
