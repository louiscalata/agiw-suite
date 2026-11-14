"""Deterministic offline fast-check plans, statistics and callable runner seam.

No model, process, network or dataset discovery. See docs/eci-fast-check.md.
Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import sys
import time

PROTOCOL = "agiw-fast-check-v1"
# Record schema 2 excludes transport errors from unknown_finish and parsed
# counts and pairs unparsed answers as wrong. Schema 1 records keep validating
# under the rules they were written with (see the _v1 functions below).
SCHEMA_VERSION = 2
SCHEMA_VERSIONS = (1, 2)
PLAN_VERSION = "fast-check-plan-v1"
# Matched to mac-pipecfg-v2/admit.py:28,47-49. Other benchmarks must declare chance.
CHANCE = {"arc": "0.25", "gsm8k": "0", "winogrande": "0.5"}
OUTCOMES = ("correct", "wrong", "truncated_empty", "unparsed", "error")
TOKEN_LIMIT_REASONS = (
    "length",
    "max_tokens",
    "model_length",
    "token_limit",
    "sample.limit",
    "sample_limit",
)
MAX_CONSECUTIVE_ERRORS = 3
STANDARD_POST = dict(
    min_coverage="1",
    max_interval_width="0.2",
    max_error_rate="0.01",
    max_truncation_rate="0.05",
    max_unparsed_rate="0.10",
)
Z_EXACT = Fraction("1.959963984540054")
Z95 = 1.959963984540054
COMPARISON_SETTINGS = (
    "scorer_id",
    "prompt_template_id",
    "backend",
    "backend_version",
    "temperature",
    "max_tokens",
    "ctx",
    "seed",
    "stop",
    "system_message",
    "n_epochs",
)


class ValidationError(ValueError):
    pass


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValidationError("missing or unexpected fields")


def string(value, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("expected nonempty text")


def numeric(value, low=0, high=None, nullable=False, integer=False):
    if value is None and nullable:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or integer
        and not isinstance(value, int)
    ):
        raise ValidationError("expected a number")
    try:
        valid = (
            math.isfinite(value) and value >= low and (high is None or value <= high)
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValidationError("number outside allowed range")


def decimal(value, low="0", high="1", *, positive=False):
    """Thresholds must arrive as exact finite decimal strings, never floats."""
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value
    ):
        raise ValidationError("expected exact decimal string")
    result = Fraction(value)
    if (
        result < Fraction(low)
        or (high is not None and result > Fraction(high))
        or (positive and result == 0)
    ):
        raise ValidationError("decimal outside allowed range")
    return result


def significance(value):
    result = decimal(value, positive=True)
    if result >= 1:
        raise ValidationError("alpha must be below one")
    return result


def sha(value, nullable=False):
    if value is None and nullable:
        return
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValidationError("expected lowercase SHA-256")


def timestamp(value):
    string(value)
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValidationError("timestamp requires a timezone and seconds")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("invalid timestamp") from None


def json_value(value):
    # Avoid accepting Python tuples, nonstring keys, NaN, or objects JSON cannot preserve.
    if value is None or type(value) in (str, bool):
        return
    if type(value) in (int, float):
        numeric(value, low=-sys.float_info.max)
    elif isinstance(value, list):
        for item in value:
            json_value(item)
    elif isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for item in value.values():
            json_value(item)
    else:
        raise ValidationError("expected JSON data")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("duplicate JSON key")
        result[key] = value
    return result


def load(path):
    value = json.loads(
        Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object
    )
    json_value(value)
    return value


def population(rows):
    if not isinstance(rows, list) or not rows:
        raise ValidationError("dataset must be a nonempty array")
    ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationError("dataset row must be an object")
        string(row.get("id"))
        if row["id"] in ids:
            raise ValidationError("duplicate dataset item id")
        ids.add(row["id"])
        json_value(row)
    return sorted(rows, key=lambda row: row["id"])


def demonstrations_for(benchmark, source):
    """Descriptor-only training examples, disjoint from the test population."""
    demos = source.get("demonstrations", [])
    if not isinstance(demos, list) or ("demonstrations" in source and benchmark != "gsm8k"):
        raise ValidationError("demonstrations must be a GSM8K training-example list")
    questions = {
        " ".join(row["question"].split()).casefold()
        for row in source["items"] if isinstance(row.get("question"), str)
    }
    ids = {row["id"] for row in source["items"]}
    seen = set()
    seen_ids = set()
    for demo in demos:
        if (
            not isinstance(demo, dict)
            or not {"question", "answer"} <= set(demo)
            or set(demo) - {"id", "question", "answer", "rationale"}
        ):
            raise ValidationError("demonstration requires question/answer and optional id/rationale")
        for key in demo:
            string(demo[key])
        question = " ".join(demo["question"].split()).casefold()
        if (
            question in questions or question in seen
            or "id" in demo and (demo["id"] in ids or demo["id"] in seen_ids)
        ):
            raise ValidationError("demonstrations must be unique and disjoint from test items")
        seen.add(question)
        if "id" in demo:
            seen_ids.add(demo["id"])
    return copy.deepcopy(demos)


def make_plan(datasets, count=200, version=PLAN_VERSION, wall_budget_s=None):
    """datasets: benchmark -> {items, revision?, chance?, stratum_key?, demonstrations?}.

    SHA-256 seeded random priorities avoid Python random/hash version dependence.
    Simple random sampling without replacement over the whole population.
    Stratum counts are descriptive only and never change selection probability.
    """
    numeric(count, 1, integer=True)
    string(version)
    if wall_budget_s is not None:
        decimal(wall_budget_s, high=None, positive=True)
    if not isinstance(datasets, dict) or not datasets:
        raise ValidationError("at least one benchmark is required")
    benchmarks = []
    for benchmark, source in sorted(datasets.items()):
        string(benchmark)
        if not isinstance(source, dict) or set(source) - {
            "items",
            "revision",
            "chance",
            "stratum_key",
            "answer_options",
            "demonstrations",
        }:
            raise ValidationError("invalid dataset descriptor")
        rows = population(source.get("items"))
        demonstrations_for(benchmark, source)
        revision = source.get("revision")
        string(revision, nullable=True)
        chance = source.get("chance", CHANCE.get(benchmark))
        if chance is not None:
            decimal(chance)
        if chance is not None and decimal(chance) == 1:
            raise ValidationError("chance must be below one")
        if benchmark in CHANCE and chance != CHANCE[benchmark]:
            raise ValidationError("chance disagrees with admission baseline")
        choice_counts = [
            len(row["choices"]) for row in rows if isinstance(row.get("choices"), list)
        ]
        inferred_options = (
            min(choice_counts)
            if choice_counts
            else {"arc": 4, "winogrande": 2}.get(
                benchmark, 2 if chance is not None and decimal(chance) > 0 else None
            )
        )
        answer_options = source.get("answer_options", inferred_options)
        if (
            chance is not None and decimal(chance) > 0 or choice_counts
        ) and answer_options is None:
            raise ValidationError("choice benchmark requires answer_options")
        numeric(answer_options, 2, nullable=True, integer=True)
        key = source.get("stratum_key")
        if "stratum_key" not in source:
            key = next(
                (k for k in ("subject", "task") if any(k in r for r in rows)), None
            )
        string(key, nullable=True)
        groups = {}
        for row in rows:
            label = row.get(key) if key else None
            string(label, nullable=True)
            groups.setdefault(canonical(label), []).append(row["id"])
        n = min(count, len(rows))
        seed = digest([benchmark, version])
        priority = lambda item_id: (digest([seed, item_id]), item_id)
        selected = sorted((row["id"] for row in rows), key=priority)[:n]
        selected_set = set(selected)
        quotas = {
            k: sum(item_id in selected_set for item_id in group)
            for k, group in groups.items()
        }
        benchmarks.append(
            dict(
                benchmark=benchmark,
                dataset_revision=revision,
                dataset_sha256=digest(rows),
                population=len(rows),
                stratum_key=key,
                strata=[
                    dict(
                        label=json.loads(k),
                        population=len(groups[k]),
                        selected=quotas[k],
                    )
                    for k in sorted(groups)
                ],
                chance=chance,
                answer_options=answer_options,
                seed=seed,
                item_ids=selected,
                sampling="simple_random_without_replacement",
                whole_dataset=n == len(rows),
            )
        )
    plan = dict(
        kind="agiw_fast_check_plan",
        schema_version=1,
        plan_version=version,
        items_per_benchmark=count,
        wall_budget_s=wall_budget_s,
        benchmarks=benchmarks,
    )
    plan["plan_id"] = digest(plan)
    validate_plan(plan)
    return plan


def validate_plan(plan):
    fields(
        plan,
        "kind schema_version plan_version items_per_benchmark wall_budget_s benchmarks plan_id",
    )
    if (
        plan["kind"] != "agiw_fast_check_plan"
        or type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
    ):
        raise ValidationError("unsupported plan")
    string(plan["plan_version"])
    numeric(plan["items_per_benchmark"], 1, integer=True)
    if plan["wall_budget_s"] is not None:
        decimal(plan["wall_budget_s"], high=None, positive=True)
    sha(plan["plan_id"])
    if digest({k: v for k, v in plan.items() if k != "plan_id"}) != plan["plan_id"]:
        raise ValidationError("plan identity mismatch")
    if not isinstance(plan["benchmarks"], list) or not plan["benchmarks"]:
        raise ValidationError("no benchmarks")
    names = set()
    for b in plan["benchmarks"]:
        fields(
            b,
            "benchmark dataset_revision dataset_sha256 population stratum_key strata chance answer_options seed item_ids sampling whole_dataset",
        )
        string(b["benchmark"])
        if b["benchmark"] in names:
            raise ValidationError("duplicate benchmark")
        names.add(b["benchmark"])
        string(b["dataset_revision"], nullable=True)
        sha(b["dataset_sha256"])
        string(b["stratum_key"], nullable=True)
        numeric(b["population"], 1, integer=True)
        numeric(b["answer_options"], 2, nullable=True, integer=True)
        if (
            b["chance"] is not None
            and decimal(b["chance"]) > 0
            and b["answer_options"] is None
        ):
            raise ValidationError("choice benchmark requires answer_options")
        if b["chance"] is not None:
            decimal(b["chance"])
        if (
            (b["chance"] is not None and decimal(b["chance"]) == 1)
            or b["benchmark"] in CHANCE
            and b["chance"] != CHANCE[b["benchmark"]]
        ):
            raise ValidationError("invalid chance baseline")
        if b["seed"] != digest([b["benchmark"], plan["plan_version"]]):
            raise ValidationError("seed mismatch")
        ids = b["item_ids"]
        if not isinstance(ids, list):
            raise ValidationError("item_ids must be an array")
        for item_id in ids:
            string(item_id)
        if len(ids) != min(plan["items_per_benchmark"], b["population"]) or len(
            set(ids)
        ) != len(ids):
            raise ValidationError("invalid sample ids")
        if (
            b["sampling"] != "simple_random_without_replacement"
            or type(b["whole_dataset"]) is not bool
            or b["whole_dataset"] != (len(ids) == b["population"])
        ):
            raise ValidationError("sampling declaration mismatch")
        if ids != sorted(
            ids, key=lambda item_id: (digest([b["seed"], item_id]), item_id)
        ):
            raise ValidationError("sample priorities out of order")
        if not isinstance(b["strata"], list) or not b["strata"]:
            raise ValidationError("missing strata")
        labels = []
        for s in b["strata"]:
            fields(s, "label population selected")
            string(s["label"], nullable=True)
            numeric(s["population"], 1, integer=True)
            numeric(s["selected"], 0, s["population"], integer=True)
            labels.append(s["label"])
        if (
            len(set(labels)) != len(labels)
            or sum(s["population"] for s in b["strata"]) != b["population"]
            or sum(s["selected"] for s in b["strata"]) != len(ids)
        ):
            raise ValidationError("stratum counts disagree")
    return plan


def normalized(raw, chance):
    numeric(raw, 0, 1, nullable=True)
    if isinstance(chance, str):
        decimal(chance)
    else:
        numeric(chance, 0, 1, nullable=True)
    if chance is not None and Fraction(str(chance)) == 1:
        raise ValidationError("chance must be below one")
    if raw is None or chance is None:
        return None
    p, c = Decimal(str(raw)), Decimal(str(chance))
    return float(min(Decimal(1), max(Decimal(0), (p - c) / (Decimal(1) - c))))


def statistics(correct, n, chance):
    numeric(n, integer=True)
    numeric(correct, 0, n, integer=True)
    normalized(None, chance)  # Validate even when there are no completed items.
    if not n:
        return dict(
            accuracy=None,
            interval95=None,
            normalized_score=None,
            normalized_interval95=None,
        )
    p = correct / n
    z2 = Z95**2
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    interval = [
        0.0 if correct == 0 else max(0.0, centre - half),
        1.0 if correct == n else min(1.0, centre + half),
    ]
    return dict(
        accuracy=p,
        interval95=interval,
        normalized_score=normalized(p, chance),
        normalized_interval95=None
        if chance is None
        else [normalized(v, chance) for v in interval],
    )


def mcnemar(candidate, baseline, alpha="0.05", expected_ids=None):
    """Two-sided exact conditional binomial test; equality is not significant."""
    exact_alpha = significance(alpha)
    for values in (candidate, baseline):
        if not isinstance(values, dict) or any(
            type(v) is not bool and v is not None for v in values.values()
        ):
            raise ValidationError("paired outcomes must be boolean or null")
        for key in values:
            string(key)
    expected = (
        set(expected_ids)
        if expected_ids is not None
        else set(candidate) | set(baseline)
    )
    if set(candidate) - expected or set(baseline) - expected:
        raise ValidationError("unplanned paired item")
    a_ids = {k for k, v in candidate.items() if v is not None}
    b_ids = {k for k, v in baseline.items() if v is not None}
    shared = sorted(a_ids & b_ids)
    counts = dict(both_correct=0, both_wrong=0, model_only=0, baseline_only=0)
    for key in shared:
        a, b = candidate[key], baseline[key]
        counts[
            "both_correct"
            if a and b
            else "model_only"
            if a
            else "baseline_only"
            if b
            else "both_wrong"
        ] += 1
    b, c = counts["model_only"], counts["baseline_only"]
    discordant = b + c
    exact = min(
        Fraction(1),
        Fraction(
            2 * sum(math.comb(discordant, k) for k in range(min(b, c) + 1)),
            2**discordant,
        ),
    )
    complete = a_ids == b_ids == expected
    verdict = (
        ("BETTER" if b > c else "WORSE")
        if complete and b != c and exact < exact_alpha
        else "INCONCLUSIVE"
    )
    return dict(
        verdict=verdict,
        alpha=alpha,
        verification="VERIFIED",
        reason="complete pairs" if complete else "incomplete pairs",
        n_expected=len(expected),
        n_candidate=len(a_ids),
        n_baseline=len(b_ids),
        n_shared=len(shared),
        n_discordant=discordant,
        **counts,
        p_value=float(exact),
        p_exact=[str(exact.numerator), str(exact.denominator)],
    )


def wilson_exact(correct, n):
    """Centre and squared half-width with the published fixed decimal z constant."""
    p, z2 = Fraction(correct, n), Z_EXACT**2
    denominator = 1 + z2 / n
    return (
        (p + z2 / (2 * n)) / denominator,
        z2 * (p * (1 - p) / n + z2 / (4 * n * n)) / denominator**2,
    )


def wilson_upper_above(correct, n, threshold):
    centre, half2 = wilson_exact(correct, n)
    return threshold < 1 and (threshold < centre or half2 > (threshold - centre) ** 2)


def decision(base, reasons, inputs, advisory, order):
    if advisory is not None:
        fields(advisory, "decision reasons")
        if advisory["decision"] not in order or not isinstance(
            advisory["reasons"], list
        ):
            raise ValidationError("invalid advisory")
        for reason in advisory["reasons"]:
            string(reason)
    final = (
        max((base, advisory["decision"]), key=order.index)
        if advisory is not None
        else base
    )
    return dict(
        decision=final,
        local_decision=base,
        reasons=reasons,
        inputs=inputs,
        advisory=copy.deepcopy(advisory),
    )


def pre_check(
    file_size_bytes,
    quantization,
    available_memory_bytes,
    safety_margin="0.25",
    advisory=None,
):
    """File-size fit estimate only; margin reserves memory for runtime/KV/OS use."""
    numeric(file_size_bytes, 1, nullable=True, integer=True)
    string(quantization, nullable=True)
    numeric(available_memory_bytes, nullable=True, integer=True)
    reserve = decimal(safety_margin, positive=True)
    if reserve >= 1:
        raise ValidationError("reserve must be below one")
    inputs = dict(
        file_size_bytes=file_size_bytes,
        quantization=quantization,
        available_memory_bytes=available_memory_bytes,
        safety_margin=safety_margin,
    )
    if None in (file_size_bytes, quantization, available_memory_bytes):
        base, reasons = (
            "UNKNOWN",
            ["File size, quantization or available memory is unknown."],
        )
    elif Fraction(file_size_bytes) > available_memory_bytes * (1 - reserve):
        base, reasons = (
            "SKIP",
            ["Model file exceeds available memory after the safety reserve."],
        )
    else:
        base, reasons = (
            "CHECK",
            ["File fits the estimate; actual runtime/KV memory remains unverified."],
        )
    return decision(base, reasons, inputs, advisory, ["CHECK", "UNKNOWN", "SKIP"])


def binomial_upper_tail(correct, n, chance):
    """One-sided exact H0:p<=chance; the boundary maximizes the upper tail."""
    return sum(
        (
            Fraction(math.comb(n, k)) * chance**k * (1 - chance) ** (n - k)
            for k in range(correct, n + 1)
        ),
        Fraction(0),
    )


def post_check(
    evidence,
    min_coverage="1",
    max_interval_width="0.2",
    max_error_rate="0.01",
    max_truncation_rate="0.05",
    max_unparsed_rate="0.10",
    alpha="0.05",
    advisory=None,
):
    """Exact rational gates; relaxed admission policies remain unranked UNKNOWN.

    One-sided chance significance uses p<=alpha. Width equality rejects; maximum
    rate equality passes. A speed measurement never changes these decisions.
    """
    thresholds = dict(
        min_coverage=min_coverage,
        max_interval_width=max_interval_width,
        max_error_rate=max_error_rate,
        max_truncation_rate=max_truncation_rate,
        max_unparsed_rate=max_unparsed_rate,
    )
    limits = {
        k: decimal(v, positive=k == "min_coverage") for k, v in thresholds.items()
    }
    exact_alpha = significance(alpha)
    if not isinstance(evidence, list):
        raise ValidationError("post evidence must be an array")
    reasons, unknown = [], []
    transport_unknown = False

    def refuse(reason):
        if reason not in reasons:
            reasons.append(reason)

    if (
        exact_alpha > Fraction("0.05")
        or limits["min_coverage"] < Fraction(STANDARD_POST["min_coverage"])
        or any(
            limits[k] > Fraction(v)
            for k, v in STANDARD_POST.items()
            if k != "min_coverage"
        )
    ):
        unknown.append("Admission policy exceeds standard ranking safety bounds.")
    for b in evidence:
        fields(
            b,
            "planned attempted correct parsed errors truncations chance answer_options top_answer_count unknown_finish",
        )
        n = b["planned"]
        numeric(n, 1, integer=True)
        for name in (
            "attempted",
            "correct",
            "parsed",
            "errors",
            "truncations",
            "top_answer_count",
            "unknown_finish",
        ):
            numeric(b[name], 0, n, integer=True)
        numeric(b["answer_options"], 2, nullable=True, integer=True)
        if (
            not b["correct"] <= b["parsed"] <= b["attempted"]
            or b["errors"] > b["attempted"] - b["parsed"]
            or b["truncations"] > b["attempted"] - b["parsed"]
            or b["unknown_finish"] > b["attempted"]
            or b["top_answer_count"] > b["parsed"]
            or bool(b["top_answer_count"]) != bool(b["parsed"])
        ):
            raise ValidationError("post counts disagree")
        chance = None if b["chance"] is None else decimal(b["chance"])
        if chance == 1:
            raise ValidationError("chance must be below one")
        excessive_errors = Fraction(b["errors"], n) > limits["max_error_rate"]
        if excessive_errors:
            unknown.append("Errors exceed the declared maximum rate; model quality is unknown.")
            transport_unknown = True
        if b["attempted"] < n:
            unknown.append(
                "Incomplete sample: check stopped before all planned items were attempted."
            )
            continue
        if Fraction(b["truncations"], n) > limits["max_truncation_rate"]:
            refuse("answers cut off at the token limit: raise the limit and re-run")
        if excessive_errors:
            continue
        _, half2 = wilson_exact(b["correct"], n)
        if 4 * half2 >= limits["max_interval_width"] ** 2:
            refuse("A Wilson interval meets or exceeds the declared maximum width.")
        answered = b["attempted"] - b["errors"]
        unparsed = answered > 0 and (
            Fraction(answered - b["parsed"], answered) > limits["max_unparsed_rate"]
        )
        if unparsed:
            refuse(
                "runtime/model incompatibility suspected: answers could not be parsed"
            )
        if b["parsed"] == 0:
            refuse("runtime/model incompatibility suspected")
        degenerate = (
            b["answer_options"] is not None
            and b["answer_options"] >= 2
            and b["parsed"] > 0
            and Fraction(b["top_answer_count"], b["parsed"]) >= Fraction("0.90")
        )
        if chance is None:
            unknown.append("Chance baseline is unknown.")
        elif chance == 0:
            if degenerate:
                refuse("runtime/model incompatibility suspected")
        elif binomial_upper_tail(b["correct"], n, chance) > exact_alpha:
            if unparsed or degenerate:
                refuse("runtime/model incompatibility suspected")
            else:
                unknown.append(
                    "not distinguishable from chance on this sample: weak model or incompatible runtime; re-run with a larger sample or check the runtime"
                )
        if b["unknown_finish"]:
            unknown.append("Cutoff/finish evidence is unknown.")
    base = "UNKNOWN" if transport_unknown else "REJECT" if reasons else "UNKNOWN" if unknown or not evidence else "ADMIT"
    reasons = list(dict.fromkeys(reasons + unknown)) or (
        ["Required evidence is unknown."]
        if not evidence
        else ["All local admissibility thresholds are met."]
    )
    return decision(
        base,
        reasons,
        dict(evidence=copy.deepcopy(evidence), alpha=alpha, **thresholds),
        advisory,
        ["ADMIT", "UNKNOWN", "REJECT"],
    )


def hardware_known(hardware):
    return all(
        hardware.get(k) is not None
        for k in ("machine_label", "cpu", "gpu", "memory", "runtime_build_id")
    )


def identity(model, hardware, settings):
    fields(model, "name file_sha256 quantization file_size_bytes")
    string(model["name"])
    sha(model["file_sha256"], nullable=True)
    string(model["quantization"], nullable=True)
    numeric(model["file_size_bytes"], 1, nullable=True, integer=True)
    required = {"machine_label", "cpu", "gpu", "memory", "runtime_build_id"}
    if not isinstance(hardware, dict) or not required <= set(hardware):
        raise ValidationError(
            "hardware identity requires machine label, CPU, GPU, memory and runtime build id"
        )
    json_value(hardware)
    for key in ("machine_label", "runtime_build_id"):
        string(hardware[key], nullable=True)
    for key in ("cpu", "gpu", "memory"):
        if hardware[key] is not None and not (
            isinstance(hardware[key], dict)
            and hardware[key]
            or isinstance(hardware[key], str)
            and hardware[key].strip()
        ):
            raise ValidationError(
                "hardware descriptor must be nonempty text, object or null"
            )
    numeric(hardware.get("memory_bytes"), 1, nullable=True, integer=True)
    if settings is not None and not isinstance(settings, dict):
        raise ValidationError("runtime settings must be an object or null")
    json_value(settings)
    if settings is not None:
        for key in (
            "scorer_id", "parse_version", "prompt_template_id",
            "prompt_template_version", "backend", "backend_version",
        ):
            string(settings.get(key), nullable=True)
        sha(settings.get("prompt_template_sha256"), nullable=True)
        if "prompt_template" in settings:
            if (
                not isinstance(settings["prompt_template"], dict)
                or digest(settings["prompt_template"]) != settings.get("prompt_template_sha256")
            ):
                raise ValidationError("prompt template hash disagrees with manifest")
        for key in ("max_tokens", "ctx", "n_epochs"):
            numeric(settings.get(key), low=1, nullable=True, integer=True)
        numeric(settings.get("seed"), nullable=True, integer=True)
        if settings.get("n_epochs") not in (None, 1):
            raise ValidationError("fast-check v1 scores one answer per item")
        numeric(settings.get("temperature"), nullable=True)
        message = settings.get("system_message")
        if message is not None and not isinstance(message, str):
            raise ValidationError("system_message must be text or null")
        stop = settings.get("stop")
        if stop is not None and not (
            isinstance(stop, list) and all(isinstance(v, str) for v in stop)
        ):
            raise ValidationError("stop must be an array of text or null")


def settings_known(settings):
    return settings is not None and all(
        settings.get(k) is not None for k in COMPARISON_SETTINGS
    )


def validate_observation(o):
    fields(
        {k: v for k, v in o.items() if k not in ("parse_rule", "parsed_unit", "attempts")}
        if isinstance(o, dict) else o,
        "item_id outcome parsed finish_reason cut_off answer_value correct prompt_tokens completion_tokens seconds error",
    )
    string(o["item_id"])
    if (
        o["outcome"] not in OUTCOMES
        or type(o["parsed"]) is not bool
        or type(o["correct"]) is not bool
    ):
        raise ValidationError("invalid outcome, parsed state or correctness")
    if o["correct"] != (o["outcome"] == "correct"):
        raise ValidationError("correctness disagrees with outcome")
    if (
        o["outcome"] in ("correct", "wrong")
        and not o["parsed"]
        or o["outcome"] in ("truncated_empty", "unparsed")
        and o["parsed"]
    ):
        raise ValidationError("outcome disagrees with parsed state")
    string(o["finish_reason"], nullable=True)
    if type(o["cut_off"]) is not bool:
        raise ValidationError("cut_off must be an explicit boolean")
    if o["finish_reason"] in TOKEN_LIMIT_REASONS and not o["cut_off"]:
        raise ValidationError("finish reason conflicts with cut_off")
    if (o["outcome"] == "truncated_empty") != (o["cut_off"] and not o["parsed"]):
        raise ValidationError("cutoff without a parsed answer must be truncated_empty")
    value = o["answer_value"]
    if o["parsed"]:
        string(value)
        if (
            len(value) > 128
            or value != value.strip()
            or any(ord(c) < 32 for c in value)
        ):
            raise ValidationError("answer_value must be a short normalized string")
    elif value is not None:
        raise ValidationError("unparsed answer_value must be null")
    if "parse_rule" in o:
        string(o["parse_rule"], nullable=True)
        if o["parsed"] != (o["parse_rule"] is not None):
            raise ValidationError("parse rule disagrees with parsed state")
    if "parsed_unit" in o:
        string(o["parsed_unit"], nullable=True)
    string(o["error"], nullable=True)
    if (o["outcome"] == "error") != (o["error"] is not None):
        raise ValidationError("error outcome requires an error type")
    for key in ("prompt_tokens", "completion_tokens"):
        numeric(o[key], nullable=True, integer=True)
    numeric(o["seconds"], nullable=True)
    if "attempts" in o:
        numeric(o["attempts"], 1, integer=True)


def summarize(spec, observations, wall_time_s, stop_reason):
    counts = {
        name: sum(o["outcome"] == name for o in observations) for name in OUTCOMES
    }
    parsed = sum(o["parsed"] and o["outcome"] != "error" for o in observations)
    answer_counts = {}
    for o in observations:
        if o["parsed"] and o["outcome"] != "error":
            answer_counts[o["answer_value"]] = (
                answer_counts.get(o["answer_value"], 0) + 1
            )
    top_answer_count = max(answer_counts.values(), default=0)

    def total(key):
        return (
            None
            if not observations or any(o[key] is None for o in observations)
            else sum(o[key] for o in observations)
        )

    tokens, seconds = total("completion_tokens"), total("seconds")
    n = len(spec["item_ids"])
    return dict(
        benchmark=spec["benchmark"],
        planned=n,
        attempted=len(observations),
        completed=counts["correct"] + counts["wrong"],
        correct=counts["correct"],
        parsed=parsed,
        errors=counts["error"],
        retried=sum(o.get("attempts", 1) > 1 for o in observations),
        timeouts=sum(o["error"] == "TimeoutError" for o in observations),
        truncations=sum(o["cut_off"] and not o["parsed"] for o in observations),
        unknown_finish=sum(
            o["finish_reason"] is None and o["outcome"] != "error"
            for o in observations
        ),
        answer_counts=answer_counts,
        top_answer_count=top_answer_count,
        top_answer_share=top_answer_count / parsed if parsed else None,
        outcome_counts=counts,
        outcome_rates={k: v / n for k, v in counts.items()},
        accuracy_over_parsed_items=counts["correct"] / parsed if parsed else None,
        observations=observations,
        wall_time_s=wall_time_s,
        tokens=tokens,
        prompt_tokens=total("prompt_tokens"),
        answer_seconds=seconds,
        tokens_per_s=tokens / seconds if tokens is not None and seconds else None,
        stop_reason=stop_reason,
        **statistics(counts["correct"], n, spec["chance"]),
    )


def outcomes(bench):
    # A transport failure supplies no model answer and cannot be a wrong pair.
    return {o["item_id"]: None if o["outcome"] == "error" else o["correct"]
            for o in bench["observations"]}


# Schema version 1 rules, frozen at agiw-suite 46ebf83 (PR #141). They exist
# only to validate records written before schema version 2; never use them to
# write a new record.
def _summarize_v1(spec, observations, wall_time_s, stop_reason):
    counts = {
        name: sum(o["outcome"] == name for o in observations) for name in OUTCOMES
    }
    parsed = sum(o["parsed"] for o in observations)
    answer_counts = {}
    for o in observations:
        if o["parsed"]:
            answer_counts[o["answer_value"]] = (
                answer_counts.get(o["answer_value"], 0) + 1
            )
    top_answer_count = max(answer_counts.values(), default=0)

    def total(key):
        return (
            None
            if not observations or any(o[key] is None for o in observations)
            else sum(o[key] for o in observations)
        )

    tokens, seconds = total("completion_tokens"), total("seconds")
    n = len(spec["item_ids"])
    return dict(
        benchmark=spec["benchmark"],
        planned=n,
        attempted=len(observations),
        completed=counts["correct"] + counts["wrong"],
        correct=counts["correct"],
        parsed=parsed,
        errors=counts["error"],
        timeouts=sum(o["error"] == "TimeoutError" for o in observations),
        truncations=sum(o["cut_off"] and not o["parsed"] for o in observations),
        unknown_finish=sum(o["finish_reason"] is None for o in observations),
        answer_counts=answer_counts,
        top_answer_count=top_answer_count,
        top_answer_share=top_answer_count / parsed if parsed else None,
        outcome_counts=counts,
        outcome_rates={k: v / n for k, v in counts.items()},
        accuracy_over_parsed_items=counts["correct"] / parsed if parsed else None,
        observations=observations,
        wall_time_s=wall_time_s,
        tokens=tokens,
        prompt_tokens=total("prompt_tokens"),
        answer_seconds=seconds,
        tokens_per_s=tokens / seconds if tokens is not None and seconds else None,
        stop_reason=stop_reason,
        **statistics(counts["correct"], n, spec["chance"]),
    )


def _outcomes_v1(bench):
    return {
        o["item_id"]: o["correct"] if o["outcome"] in ("correct", "wrong") else None
        for o in bench["observations"]
    }


def _post_check_v1(
    evidence,
    min_coverage="1",
    max_interval_width="0.2",
    max_error_rate="0.01",
    max_truncation_rate="0.05",
    max_unparsed_rate="0.10",
    alpha="0.05",
    advisory=None,
):
    thresholds = dict(
        min_coverage=min_coverage,
        max_interval_width=max_interval_width,
        max_error_rate=max_error_rate,
        max_truncation_rate=max_truncation_rate,
        max_unparsed_rate=max_unparsed_rate,
    )
    limits = {
        k: decimal(v, positive=k == "min_coverage") for k, v in thresholds.items()
    }
    exact_alpha = decimal(alpha, positive=True)
    if not isinstance(evidence, list):
        raise ValidationError("post evidence must be an array")
    reasons, unknown = [], []

    def refuse(reason):
        if reason not in reasons:
            reasons.append(reason)

    if limits["min_coverage"] < Fraction(STANDARD_POST["min_coverage"]) or any(
        limits[k] > Fraction(v) for k, v in STANDARD_POST.items() if k != "min_coverage"
    ):
        unknown.append("Admission policy exceeds standard ranking safety bounds.")
    for b in evidence:
        fields(
            b,
            "planned attempted correct parsed errors truncations chance answer_options top_answer_count unknown_finish",
        )
        n = b["planned"]
        numeric(n, 1, integer=True)
        for name in (
            "attempted",
            "correct",
            "parsed",
            "errors",
            "truncations",
            "top_answer_count",
            "unknown_finish",
        ):
            numeric(b[name], 0, n, integer=True)
        numeric(b["answer_options"], 2, nullable=True, integer=True)
        if (
            not b["correct"] <= b["parsed"] <= b["attempted"]
            or b["errors"] > b["attempted"]
            or b["truncations"] > b["attempted"] - b["parsed"]
            or b["unknown_finish"] > b["attempted"]
            or b["top_answer_count"] > b["parsed"]
            or bool(b["top_answer_count"]) != bool(b["parsed"])
        ):
            raise ValidationError("post counts disagree")
        chance = None if b["chance"] is None else decimal(b["chance"])
        if chance == 1:
            raise ValidationError("chance must be below one")
        if Fraction(b["attempted"], n) < limits["min_coverage"]:
            refuse("Coverage is below the declared minimum.")
        _, half2 = wilson_exact(b["correct"], n)
        if 4 * half2 >= limits["max_interval_width"] ** 2:
            refuse("A Wilson interval meets or exceeds the declared maximum width.")
        if Fraction(b["errors"], n) > limits["max_error_rate"]:
            refuse("Errors exceed the declared maximum rate.")
        if Fraction(b["truncations"], n) > limits["max_truncation_rate"]:
            refuse("answers cut off at the token limit: raise the limit and re-run")
        unparsed = (
            Fraction(b["attempted"] - b["parsed"], n) > limits["max_unparsed_rate"]
        )
        if unparsed:
            refuse(
                "runtime/model incompatibility suspected: answers could not be parsed"
            )
        if b["parsed"] == 0:
            refuse("runtime/model incompatibility suspected")
        degenerate = (
            b["answer_options"] is not None
            and b["answer_options"] >= 2
            and b["parsed"] > 0
            and Fraction(b["top_answer_count"], b["parsed"]) >= Fraction("0.90")
        )
        if chance is None:
            unknown.append("Chance baseline is unknown.")
        elif chance == 0:
            if degenerate:
                refuse("runtime/model incompatibility suspected")
        elif binomial_upper_tail(b["correct"], n, chance) > exact_alpha:
            if unparsed or degenerate:
                refuse("runtime/model incompatibility suspected")
            else:
                unknown.append(
                    "not distinguishable from chance on this sample: weak model or incompatible runtime; re-run with a larger sample or check the runtime"
                )
        if b["unknown_finish"]:
            unknown.append("Cutoff/finish evidence is unknown.")
    base = "REJECT" if reasons else "UNKNOWN" if unknown or not evidence else "ADMIT"
    reasons = list(dict.fromkeys(reasons + unknown)) or (
        ["Required evidence is unknown."]
        if not evidence
        else ["All local admissibility thresholds are met."]
    )
    return decision(
        base,
        reasons,
        dict(evidence=copy.deepcopy(evidence), alpha=alpha, **thresholds),
        advisory,
        ["ADMIT", "UNKNOWN", "REJECT"],
    )


def legacy_record(record):
    """True for a schema version 1 record, validated under its original rules."""
    return record.get("schema_version") == 1


def baseline_outcomes(record):
    return {
        b["benchmark"]: {
            o["item_id"]: {
                k: o[k]
                for k in (
                    "outcome",
                    "correct",
                    "parsed",
                    "finish_reason",
                    "cut_off",
                    "answer_value",
                )
            }
            for o in b["observations"]
        }
        for b in record["benchmarks"]
    }


def baseline_snapshot(record):
    return dict(
        record_id=record["record_id"],
        outcomes_sha256=digest(baseline_outcomes(record)),
        record=copy.deepcopy(record),
    )


def verified_baseline(binding, seen=()):
    if binding is None:
        return None
    fields(binding, "record_id outcomes_sha256 record")
    sha(binding["record_id"])
    sha(binding["outcomes_sha256"])
    if binding["record"] is None:
        return None
    record = validate_record(binding["record"], _seen=seen)
    if (
        record["record_id"] != binding["record_id"]
        or digest(baseline_outcomes(record)) != binding["outcomes_sha256"]
    ):
        raise ValidationError("baseline record or per-item outcome hash mismatch")
    return record


def comparisons(record, binding, alpha, _seen=()):
    significance(alpha)
    baseline = verified_baseline(binding, _seen)
    return _comparisons_validated(record, baseline, alpha)


def _comparisons_validated(record, baseline, alpha):
    """Internal path used after checking the inputs or validating both records."""
    if baseline is not None and (
        baseline["plan"]["plan_id"] != record["plan"]["plan_id"]
        or canonical(baseline["hardware"]) != canonical(record["hardware"])
        or canonical(baseline["runtime_settings"])
        != canonical(record["runtime_settings"])
    ):
        # Full settings equality includes parse/template versions and template
        # hashes when present. Legacy records remain valid in their own cohort;
        # missing fields cannot silently match a versioned adapter result.
        raise ValidationError("baseline plan, hardware or settings mismatch")
    known = hardware_known(record["hardware"]) and settings_known(
        record["runtime_settings"]
    )
    # Pairs are formed under the rules of the record being compared; a schema
    # 1 record was written with schema 1 rules for both sides.
    pair_outcomes = _outcomes_v1 if legacy_record(record) else outcomes
    result = {}
    for spec, bench in zip(record["plan"]["benchmarks"], record["benchmarks"]):
        a = pair_outcomes(bench)
        if baseline is None or not known:
            result[bench["benchmark"]] = dict(
                verdict="INCONCLUSIVE",
                alpha=alpha,
                verification="UNVERIFIED",
                reason="baseline cannot be resolved and verified"
                if baseline is None
                else "hardware/runtime identity unknown",
                n_expected=len(spec["item_ids"]),
                n_candidate=sum(v is not None for v in a.values()),
                n_baseline=None,
                n_shared=None,
                n_discordant=None,
                both_correct=None,
                both_wrong=None,
                model_only=None,
                baseline_only=None,
                p_value=None,
                p_exact=None,
            )
        else:
            peer = next(
                b
                for b in baseline["benchmarks"]
                if b["benchmark"] == bench["benchmark"]
            )
            result[bench["benchmark"]] = mcnemar(
                a, pair_outcomes(peer), alpha, spec["item_ids"]
            )
    return result


def checkpoint_open(path, binding, plan):
    """Read a strictly bound observation prefix; fail closed on torn/corrupt rows.

    An exclusive OS lock owns the read/append lifetime, including adapter calls.
    Append and fsync each observation before another adapter call. Chained hashes
    detect accidental edits; they do not authenticate deliberate rewriting.
    The file contains no prompts or unnormalized generated text.
    """
    stream = Path(path).open("a+", encoding="utf-8")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValidationError(
                "checkpoint already locked by another writer"
            ) from exc
        stream.seek(0)
        expected = [
            (s["benchmark"], i) for s in plan["benchmarks"] for i in s["item_ids"]
        ]
        observations = []
        previous = digest(binding)
        for line in stream:
            if binding["model"]["file_sha256"] is None:
                raise ValidationError(
                    "checkpoint resume requires a stable artifact file_sha256 hash"
                )
            if not line.endswith("\n"):
                raise ValidationError(
                    "incomplete checkpoint line; preserve file for recovery"
                )
            try:
                entry = json.loads(line, object_pairs_hook=unique_object)
            except ValueError as exc:
                raise ValidationError("invalid checkpoint JSON") from exc
            json_value(entry)
            fields({k: v for k, v in entry.items() if k != "supersedes_sha256"},
                   "binding benchmark observation previous_sha256 entry_sha256")
            if canonical(entry["binding"]) != canonical(binding):
                raise ValidationError(
                    "checkpoint plan/model/hardware/settings mismatch"
                )
            if entry["previous_sha256"] != previous or entry["entry_sha256"] != digest(
                {k: v for k, v in entry.items() if k != "entry_sha256"}
            ):
                raise ValidationError("checkpoint checksum chain integrity mismatch")
            validate_observation(entry["observation"])
            key = (entry["benchmark"], entry["observation"]["item_id"])
            if "supersedes_sha256" in entry:
                # Only an error in the current trailing suffix may be replaced.
                # The exact old entry hash binds each replacement to its history.
                position = next((i for i, old in enumerate(observations)
                                 if old["entry_sha256"] == entry["supersedes_sha256"]), None)
                if (position is None or key != expected[position]
                        or any(old["observation"]["outcome"] != "error"
                               for old in observations[position:])):
                    raise ValidationError("checkpoint supersession must replace a trailing error")
                observations[position] = entry
            else:
                position = len(observations)
                if position >= len(expected) or key != expected[position]:
                    raise ValidationError("checkpoint is not the frozen item prefix")
                if (len(observations) >= MAX_CONSECUTIVE_ERRORS
                        and all(old["observation"]["outcome"] == "error"
                                for old in observations[-MAX_CONSECUTIVE_ERRORS:])):
                    raise ValidationError("checkpoint continued after consecutive error stop")
                observations.append(entry)
            previous = entry["entry_sha256"]
        stream._eci_previous_sha256 = previous
        stream._eci_supersedes = {}
        while observations and observations[-1]["observation"]["outcome"] == "error":
            old = observations.pop()
            stream._eci_supersedes[(old["benchmark"], old["observation"]["item_id"])] = old["entry_sha256"]
        return stream, observations
    except BaseException:
        stream.close()
        raise


def checkpoint_append(stream, binding, benchmark, observation):
    entry = dict(
        binding=binding,
        benchmark=benchmark,
        observation=observation,
        previous_sha256=stream._eci_previous_sha256,
    )
    supersedes = stream._eci_supersedes.pop((benchmark, observation["item_id"]), None)
    if supersedes is not None:
        entry["supersedes_sha256"] = supersedes
    entry["entry_sha256"] = digest(entry)
    stream.write(canonical(entry) + "\n")
    stream.flush()
    os.fsync(stream.fileno())
    stream._eci_previous_sha256 = entry["entry_sha256"]


def adapter_call(callback, args, spec):
    """Supply context when accepted, without retrying a callback's own TypeError.

    Legacy one-/two-argument callbacks remain usable. New adapters may accept
    either or both keyword-only ``benchmark`` and ``spec``; ``**kwargs`` also
    accepts both. Pass a copy so an adapter cannot mutate the frozen plan.
    """
    context = dict(benchmark=spec["benchmark"], spec=copy.deepcopy(spec))
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "adapter callback must expose an inspectable signature"
        ) from exc
    accepted = {
        name: value
        for name, value in context.items()
        if name in signature.parameters
        or any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in signature.parameters.values()
        )
    }
    signature.bind(*args, **accepted)
    return callback(*args, **accepted)


def run_check(
    plan,
    datasets,
    answer,
    *,
    model,
    hardware,
    runtime_settings,
    pre,
    journal=None,
    baseline=None,
    alpha="0.05",
    post_options=None,
    clock=time.monotonic,
    created_at=None,
    score=None,
    checkpoint=None,
    provenance=None,
):
    """answer(item) returns adapter-classified evidence, never an untyped tuple.

    Required: outcome, parsed, finish_reason, cut_off, answer_value, prompt_tokens, completion_tokens,
    seconds, error. Optional parse_rule records the screening parser rule;
    optional prediction is used only by a supplied score checker.
    Optional benchmark/spec keyword context is passed to callbacks that accept it.
    Exceptions may expose measured seconds/prompt_tokens/completion_tokens attrs.
    Budget checks are cooperative between calls; adapters enforce call deadlines.
    """
    validate_plan(plan)
    identity(model, hardware, runtime_settings)
    string(journal, nullable=True)
    significance(alpha)
    if provenance is not None:
        if not isinstance(provenance, dict):
            raise ValidationError("provenance must be an object")
        json_value(provenance)
    if post_options and "alpha" in post_options:
        raise ValidationError("post alpha is bound to the record alpha")
    post_check([], alpha=alpha, **(post_options or {}))
    rebuilt = make_plan(
        datasets,
        plan["items_per_benchmark"],
        plan["plan_version"],
        plan["wall_budget_s"],
    )
    if rebuilt != plan:
        raise ValidationError("dataset or selection disagrees with frozen plan")
    if pre != pre_check(**pre["inputs"], advisory=pre["advisory"]) or any(
        pre["inputs"][k] != model[k] for k in ("file_size_bytes", "quantization")
    ):
        raise ValidationError("pre decision or model inputs disagree")
    available = pre["inputs"]["available_memory_bytes"]
    if (
        available is not None
        and hardware.get("memory_bytes") is not None
        and available > hardware["memory_bytes"]
    ):
        raise ValidationError("available memory exceeds hardware memory")
    if baseline is not None:
        validate_record(baseline)
        if canonical(
            [baseline["plan"], baseline["hardware"], baseline["runtime_settings"]]
        ) != canonical([plan, hardware, runtime_settings]):
            raise ValidationError("baseline plan, hardware or settings mismatch")
        baseline = baseline_snapshot(baseline)
    stamp = created_at or datetime.now(timezone.utc).isoformat()
    timestamp(stamp)
    record = dict(
        kind="agiw_fast_check",
        schema_version=SCHEMA_VERSION,
        protocol_id=PROTOCOL,
        model=copy.deepcopy(model),
        hardware=copy.deepcopy(hardware),
        runtime_settings=copy.deepcopy(runtime_settings),
        plan=copy.deepcopy(plan),
        created_at=stamp,
        pre=copy.deepcopy(pre),
        journal=journal,
        baseline=baseline,
        alpha=alpha,
        benchmarks=[],
    )
    binding = dict(
        plan_id=plan["plan_id"],
        model=model,
        hardware=hardware,
        runtime_settings=runtime_settings,
    )
    if provenance is not None:
        record["provenance"] = copy.deepcopy(provenance)
        binding["provenance"] = copy.deepcopy(provenance)
    stream, prior = (
        (None, []) if checkpoint is None else checkpoint_open(checkpoint, binding, plan)
    )
    if (prior or stream is not None and stream._eci_supersedes) and pre["decision"] != "CHECK":
        stream.close()
        raise ValidationError("checkpoint observations conflict with pre gate")
    start = clock()
    consecutive_errors = 0
    try:
        for spec in plan["benchmarks"]:
            begin = clock()
            rows = {r["id"]: r for r in datasets[spec["benchmark"]]["items"]}
            observations = [
                copy.deepcopy(e["observation"])
                for e in prior
                if e["benchmark"] == spec["benchmark"]
            ]
            stop = "complete"
            for item_id in spec["item_ids"][len(observations) :]:
                if pre["decision"] != "CHECK":
                    stop = "pre"
                    break
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    stop = "errors"
                    break
                if plan["wall_budget_s"] is not None and Fraction(
                    str(clock())
                ) - Fraction(str(start)) >= decimal(
                    plan["wall_budget_s"], high=None, positive=True
                ):
                    stop = "budget"
                    break
                row = copy.deepcopy(rows[item_id])
                observation = dict(
                    item_id=item_id,
                    outcome="error",
                    correct=False,
                    parsed=False,
                    finish_reason=None,
                    cut_off=False,
                    answer_value=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    seconds=None,
                    error=None,
                    attempts=1,
                )
                call_start = clock()
                try:
                    supplied = adapter_call(answer, (row,), spec)
                    if not isinstance(supplied, dict):
                        raise ValidationError(
                            "adapter must supply classified outcome evidence"
                        )
                    # Retain every valid measured value even if another field or scoring fails.
                    bad_measurement = False
                    for key in ("prompt_tokens", "completion_tokens", "seconds"):
                        value = supplied.get(key)
                        try:
                            numeric(value, nullable=True, integer=key != "seconds")
                            observation[key] = value
                        except ValidationError:
                            bad_measurement = True
                    if type(supplied.get("parsed")) is bool:
                        observation["parsed"] = supplied["parsed"]
                    if (
                        isinstance(supplied.get("finish_reason"), str)
                        and supplied["finish_reason"].strip()
                    ):
                        observation["finish_reason"] = supplied["finish_reason"]
                    required = {
                        "outcome",
                        "parsed",
                        "finish_reason",
                        "cut_off",
                        "answer_value",
                        "prompt_tokens",
                        "completion_tokens",
                        "seconds",
                        "error",
                    }
                    if (
                        not required <= set(supplied)
                        or set(supplied) - required
                        - {"prediction", "parse_rule", "parsed_unit", "attempts"}
                        or bad_measurement
                    ):
                        raise ValidationError(
                            "invalid adapter evidence fields or measurements"
                        )
                    observation.update(
                        outcome=supplied["outcome"],
                        correct=supplied["outcome"] == "correct",
                        error=supplied["error"],
                        parsed=supplied["parsed"],
                        finish_reason=supplied["finish_reason"],
                        cut_off=supplied["cut_off"],
                        answer_value=supplied["answer_value"],
                    )
                    for optional in ("parse_rule", "parsed_unit", "attempts"):
                        if optional in supplied:
                            observation[optional] = supplied[optional]
                    if (
                        observation["cut_off"] is True
                        and observation["parsed"] is False
                    ):
                        observation.update(
                            outcome="truncated_empty", correct=False, error=None
                        )
                    validate_observation(observation)
                    if score is not None and observation["outcome"] in (
                        "correct",
                        "wrong",
                    ):
                        checked = adapter_call(
                            score, (supplied.get("prediction"), row), spec
                        )
                        if (
                            type(checked) is not bool
                            or checked != observation["correct"]
                        ):
                            raise ValidationError(
                                "scorer disagrees with adapter outcome"
                            )
                except ValidationError:
                    raise
                except Exception as exc:
                    observation.update(
                        outcome="error", correct=False, error=type(exc).__name__
                    )
                    attempts = getattr(exc, "attempts", 1)
                    numeric(attempts, 1, integer=True)
                    observation["attempts"] = attempts
                    for key in ("prompt_tokens", "completion_tokens", "seconds"):
                        value = getattr(exc, key, None)
                        try:
                            numeric(value, nullable=True, integer=key != "seconds")
                            if value is not None:
                                observation[key] = value
                        except ValidationError:
                            pass
                    if observation["seconds"] is None:
                        observation["seconds"] = max(0, clock() - call_start)
                if observation["cut_off"] and not observation["parsed"]:
                    observation.update(
                        outcome="truncated_empty", correct=False, error=None
                    )
                validate_observation(observation)
                if stream is not None:
                    checkpoint_append(stream, binding, spec["benchmark"], observation)
                observations.append(observation)
                consecutive_errors = (
                    consecutive_errors + 1 if observation["outcome"] == "error" else 0
                )
            record["benchmarks"].append(
                summarize(spec, observations, max(0, clock() - begin), stop)
            )
        record["wall_time_s"] = max(0, clock() - start)
    finally:
        if stream is not None:
            stream.close()
    record["comparison"] = comparisons(record, baseline, alpha)
    record["post"] = post_check(
        post_inputs(record), alpha=alpha, **(post_options or {})
    )
    record["record_id"] = digest(record)
    validate_record(record)
    return record


def post_inputs(record):
    return [
        dict(
            planned=b["planned"],
            attempted=b["attempted"],
            correct=b["correct"],
            parsed=b["parsed"],
            errors=b["errors"],
            truncations=b["truncations"],
            chance=s["chance"],
            answer_options=s["answer_options"],
            top_answer_count=b["top_answer_count"],
            unknown_finish=b["unknown_finish"],
        )
        for s, b in zip(record["plan"]["benchmarks"], record["benchmarks"])
    ]


def validate_record(record, _seen=()):
    """Recompute derived claims and recursively verify the full bound baseline."""
    fields(
        {k: v for k, v in record.items() if k != "provenance"}
        if isinstance(record, dict) else record,
        "kind schema_version protocol_id record_id created_at model hardware runtime_settings plan pre journal post baseline alpha benchmarks wall_time_s comparison",
    )
    if "provenance" in record and not isinstance(record["provenance"], dict):
        raise ValidationError("provenance must be an object")
    if len(_seen) >= 32 or record["record_id"] in _seen:
        raise ValidationError("cyclic or excessively deep baseline chain")
    if (
        record["kind"] != "agiw_fast_check"
        or type(record["schema_version"]) is not int
        or record["schema_version"] not in SCHEMA_VERSIONS
        or record["protocol_id"] != PROTOCOL
    ):
        raise ValidationError("unsupported fast-check record")
    legacy = legacy_record(record)
    if legacy and "provenance" in record:
        raise ValidationError("schema version 1 records predate provenance")
    json_value(record)
    sha(record["record_id"])
    if (
        digest({k: v for k, v in record.items() if k != "record_id"})
        != record["record_id"]
    ):
        raise ValidationError("record identity mismatch")
    timestamp(record["created_at"])
    identity(record["model"], record["hardware"], record["runtime_settings"])
    validate_plan(record["plan"])
    string(record["journal"], nullable=True)
    significance(record["alpha"])
    numeric(record["wall_time_s"])
    pre = record["pre"]
    fields(pre, "decision local_decision reasons inputs advisory")
    fields(
        pre["inputs"],
        "file_size_bytes quantization available_memory_bytes safety_margin",
    )
    if canonical(pre) != canonical(
        pre_check(**pre["inputs"], advisory=pre["advisory"])
    ) or any(
        pre["inputs"][k] != record["model"][k]
        for k in ("file_size_bytes", "quantization")
    ):
        raise ValidationError("pre decision/model disagrees with inputs")
    available = pre["inputs"]["available_memory_bytes"]
    if (
        available is not None
        and record["hardware"].get("memory_bytes") is not None
        and available > record["hardware"]["memory_bytes"]
    ):
        raise ValidationError("available memory exceeds hardware memory")
    benches, specs = record["benchmarks"], record["plan"]["benchmarks"]
    if not isinstance(benches, list) or len(benches) != len(specs):
        raise ValidationError("benchmark coverage mismatch")
    consecutive_errors = 0
    stop_reasons = ("complete", "pre", "budget") if legacy else ("complete", "pre", "budget", "errors")
    for spec, b in zip(specs, benches):
        if not isinstance(b, dict) or not isinstance(b.get("observations"), list):
            raise ValidationError("benchmark observations must be an array")
        numeric(b.get("wall_time_s"))
        if b.get("stop_reason") not in stop_reasons:
            raise ValidationError("invalid stop reason")
        ids = []
        for o in b["observations"]:
            if not legacy and consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                raise ValidationError("record continued after three consecutive errors")
            if legacy and isinstance(o, dict) and ({"parsed_unit", "attempts"} & set(o)):
                raise ValidationError("schema version 1 observations predate unit and retry evidence")
            validate_observation(o)
            ids.append(o["item_id"])
            consecutive_errors = (
                consecutive_errors + 1 if o["outcome"] == "error" else 0
            )
        if b["stop_reason"] == "errors" and consecutive_errors < MAX_CONSECUTIVE_ERRORS:
            raise ValidationError("error stop requires consecutive error evidence")
        if ids != spec["item_ids"][: len(ids)] or len(ids) > len(spec["item_ids"]):
            raise ValidationError("observations must follow frozen sample prefix")
        if pre["decision"] != "CHECK" and (ids or b["stop_reason"] != "pre"):
            raise ValidationError("pre did not authorize check")
        if legacy:
            if b["stop_reason"] == "complete" and len(ids) != len(spec["item_ids"]):
                raise ValidationError("incomplete sample cannot claim completion")
        else:
            if (b["stop_reason"] == "pre") != (pre["decision"] != "CHECK"):
                raise ValidationError("pre stop must match the pre decision")
            if (b["stop_reason"] == "complete") != (len(ids) == len(spec["item_ids"])):
                raise ValidationError("completion stop must match complete sample coverage")
        if b["stop_reason"] == "budget" and record["plan"]["wall_budget_s"] is None:
            raise ValidationError("budget stop without a budget")
        summary_rule = _summarize_v1 if legacy else summarize
        expected_summary = summary_rule(spec, b["observations"], b["wall_time_s"], b["stop_reason"])
        if canonical(b) != canonical(expected_summary):
            raise ValidationError("summary disagrees with observations")
    if sum(Fraction(str(b["wall_time_s"])) for b in benches) > Fraction(
        str(record["wall_time_s"])
    ) + Fraction("0.000001"):
        raise ValidationError("benchmark time exceeds total wall time")
    if canonical(record["comparison"]) != canonical(
        comparisons(
            record, record["baseline"], record["alpha"], _seen + (record["record_id"],)
        )
    ):
        raise ValidationError("paired comparison disagrees with verified evidence")
    post = record["post"]
    fields(post, "decision local_decision reasons inputs advisory")
    fields(
        post["inputs"],
        "evidence min_coverage max_interval_width max_error_rate max_truncation_rate max_unparsed_rate alpha",
    )
    if post["inputs"]["alpha"] != record["alpha"]:
        raise ValidationError("post alpha must match record alpha")
    post_rule = _post_check_v1 if legacy else post_check
    if canonical(post) != canonical(
        post_rule(**post["inputs"], advisory=post["advisory"])
    ) or canonical(post_inputs(record)) != canonical(post["inputs"]["evidence"]):
        raise ValidationError("post decision disagrees with evidence")
    return record


def pair_verdict(candidate, baseline, alpha="0.05"):
    validate_record(candidate)
    validate_record(baseline)
    significance(alpha)
    return _pair_verdict_validated(candidate, baseline, alpha)


def _pair_verdict_validated(candidate, baseline, alpha):
    tests = _comparisons_validated(candidate, baseline, alpha)
    if not candidate["model"]["file_sha256"] or not baseline["model"]["file_sha256"]:
        for test in tests.values():
            test.update(verdict="INCONCLUSIVE", verification="UNVERIFIED",
                        reason="candidate model artifact identity unknown", p_value=None, p_exact=None)
        return "INCONCLUSIVE", tests
    if (candidate["model"]["name"] == baseline["model"]["name"] and
            candidate["model"]["file_sha256"] != baseline["model"]["file_sha256"]):
        for test in tests.values():
            test.update(verdict="INCONCLUSIVE", verification="UNVERIFIED",
                        reason="same model name, different model artifact", p_value=None, p_exact=None)
        return "INCONCLUSIVE", tests
    # Every benchmark must have complete verified pairs, even where another wins.
    if any(
        t["verification"] != "VERIFIED" or t["reason"] == "incomplete pairs"
        for t in tests.values()
    ):
        return "INCONCLUSIVE", tests
    directions = {t["verdict"] for t in tests.values()} - {"INCONCLUSIVE"}
    return (next(iter(directions)) if len(directions) == 1 else "INCONCLUSIVE"), tests


def screening_evidence(record):
    """Project counts from validated observations, including incomplete runs."""
    return [dict(
        **{k: b[k] for k in ("benchmark", "planned", "attempted", "completed",
                              "correct", "parsed", "truncations", "errors", "timeouts",
                              "unknown_finish", "stop_reason", "normalized_score",
                              "normalized_interval95", "accuracy", "interval95")},
        unparsed=b["attempted"] - b["parsed"] - b["errors"],
        retried=b.get("retried"),
        wrong=b["outcome_counts"]["wrong"],
        cut_off=sum(o["cut_off"] for o in b["observations"]),
    ) for b in record["benchmarks"]]


def screening_baseline_evidence(record):
    """Preserve bound results while withholding claims with unknown artifact identity."""
    result = copy.deepcopy(record["comparison"])
    binding = record["baseline"]
    baseline = binding["record"] if binding else None
    if (not record["model"]["file_sha256"] or
            baseline is not None and not baseline["model"]["file_sha256"]):
        for test in result.values():
            test.update(verdict="INCONCLUSIVE", verification="UNVERIFIED",
                        reason="candidate model artifact identity unknown", p_value=None, p_exact=None)
    elif (baseline is not None and record["model"]["name"] == baseline["model"]["name"] and
          record["model"]["file_sha256"] != baseline["model"]["file_sha256"]):
        for test in result.values():
            test.update(verdict="INCONCLUSIVE", verification="UNVERIFIED",
                        reason="same model name, different model artifact", p_value=None, p_exact=None)
    return result


def screening_mismatches(first, second):
    """Explain barriers between two validated screening records."""
    reasons = []
    if not hardware_known(first["hardware"]) or not hardware_known(second["hardware"]):
        reasons.append("hardware profile unknown")
    elif canonical(first["hardware"]) != canonical(second["hardware"]):
        reasons.append("hardware profile differs")
    if first["plan"]["plan_id"] != second["plan"]["plan_id"]:
        reasons.append("frozen plan or dataset artifact differs")
    if not settings_known(first["runtime_settings"]) or not settings_known(second["runtime_settings"]):
        reasons.append("runtime settings unknown")
    elif canonical(first["runtime_settings"]) != canonical(second["runtime_settings"]):
        reasons.append("runtime settings differ")
    if not first["model"]["file_sha256"] or not second["model"]["file_sha256"]:
        reasons.append("candidate model artifact identity unknown")
    elif (first["model"]["name"] == second["model"]["name"] and
          first["model"]["file_sha256"] != second["model"]["file_sha256"]):
        reasons.append("same model name, different model artifact")
    if first["alpha"] != second["alpha"]:
        reasons.append("alpha differs")
    if any(first["post"]["inputs"][k] != second["post"]["inputs"][k] for k in STANDARD_POST):
        reasons.append("admission policy differs")
    return reasons


def rank(records):
    """Per hardware/plan/settings screening; no ECI or routing policy output."""
    groups, seen = {}, set()
    for record in records:
        validate_record(record)
        if record["record_id"] in seen:
            raise ValidationError("duplicate fast-check record")
        seen.add(record["record_id"])
        key = canonical(
            [
                record["hardware"],
                record["plan"]["plan_id"],
                record["runtime_settings"],
                record["alpha"],
                {k: record["post"]["inputs"][k] for k in STANDARD_POST},
            ]
        )
        group = groups.setdefault(
            key,
            dict(
                hardware=record["hardware"],
                plan_id=record["plan"]["plan_id"],
                runtime_settings=record["runtime_settings"],
                alpha=record["alpha"],
                admission_policy={
                    k: record["post"]["inputs"][k] for k in STANDARD_POST
                },
                records=[],
            ),
        )
        group["records"].append(record)
    result = []
    for key, group in sorted(groups.items()):
        rows, pending, eligible = [], [], []
        group_records = group.pop("records")
        group["not_comparable"] = [
            dict(record_id=record["record_id"], other_record_id=other["record_id"], reasons=reasons)
            for record in group_records for other in records
            if record["record_id"] != other["record_id"]
            for reasons in [screening_mismatches(record, other)] if reasons
        ]
        for record in group_records:
            known = (
                hardware_known(record["hardware"])
                and settings_known(record["runtime_settings"])
                and record["model"]["file_sha256"] is not None
                and all(
                    b["normalized_score"] is not None and b["attempted"] == b["planned"]
                    for b in record["benchmarks"]
                )
            )
            if (
                record["pre"]["decision"] != "CHECK"
                or record["post"]["decision"] != "ADMIT"
                or not known
            ):
                pending.append(
                    dict(
                        record_id=record["record_id"],
                        model=record["model"]["name"],
                        label="fast check",
                        pre=record["pre"]["decision"],
                        post=record["post"]["decision"],
                        reason="Not eligible: fit, admissibility, hardware, model artifact, settings, complete coverage or normalized score is unknown or refused.",
                        reasons=record["pre"]["reasons"] + record["post"]["reasons"],
                        baseline_id=record["baseline"]["record_id"] if record["baseline"] else None,
                        baseline_comparison=screening_baseline_evidence(record),
                        created_at=record["created_at"],
                        benchmarks=screening_evidence(record),
                    )
                )
            else:
                eligible.append(record)
        for record in eligible:
            tests = [
                (other, *_pair_verdict_validated(record, other, group["alpha"]))
                for other in eligible
                if other["record_id"] != record["record_id"]
                and not screening_mismatches(record, other)
            ]
            ties = [
                o["record_id"] for o, verdict, _ in tests if verdict == "INCONCLUSIVE"
            ]
            dominated = any(verdict == "WORSE" for _, verdict, _ in tests)
            n = len(record["benchmarks"])
            rows.append(
                dict(
                    record_id=record["record_id"],
                    model=record["model"]["name"],
                    artifact_sha256=record["model"]["file_sha256"],
                    baseline_id=record["baseline"]["record_id"] if record["baseline"] else None,
                    baseline_comparison=screening_baseline_evidence(record),
                    created_at=record["created_at"],
                    reasons=record["pre"]["reasons"] + record["post"]["reasons"],
                    quantization=record["model"]["quantization"],
                    label="fast check",
                    normalized_score=math.fsum(
                        b["normalized_score"] for b in record["benchmarks"]
                    )
                    / n,
                    interval=[
                        math.fsum(
                            b["normalized_interval95"][i] for b in record["benchmarks"]
                        )
                        / n
                        for i in (0, 1)
                    ],
                    interval_basis="mean of component Wilson 95% endpoints; not a joint 95% interval",
                    benchmarks=screening_evidence(record),
                    tie_with=ties,
                    verdict="INCONCLUSIVE"
                    if ties or not tests
                    else "WORSE"
                    if dominated
                    else "BETTER",
                    finalist=not dominated,
                    full_run="NOT_RUN",
                    comparisons=[
                        dict(
                            record_id=o["record_id"], model=o["model"]["name"], verdict=verdict, benchmarks=detail
                        )
                        for o, verdict, detail in tests
                    ],
                )
            )
        rows.sort(key=lambda row: (-row["normalized_score"], row["record_id"]))
        timestamps = [
            r["created_at"]
            for r in records
            if canonical(
                [
                    r["hardware"],
                    r["plan"]["plan_id"],
                    r["runtime_settings"],
                    r["alpha"],
                    {k: r["post"]["inputs"][k] for k in STANDARD_POST},
                ]
            )
            == key
        ]
        as_of = max(timestamps, key=timestamp)
        result.append(
            dict(
                **group,
                as_of=as_of,
                title=f"Screening results as of {as_of}",
                label="fast check",
                rows=rows,
                pending=pending,
                finalist_record_ids=[r["record_id"] for r in rows if r["finalist"]],
                note="Provisional screening only. INCONCLUSIVE does not establish equivalence. Full ECI NOT_RUN.",
            )
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline fast-check plan creation and record validation; no inference."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument(
        "datasets", type=Path, help="JSON benchmark descriptors with local items"
    )
    p.add_argument("--count", type=int, default=200)
    p.add_argument("--version", default=PLAN_VERSION)
    p.add_argument("--wall-budget-s", help="exact positive decimal seconds")
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("validate")
    p.add_argument("record", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            value = make_plan(
                load(args.datasets), args.count, args.version, args.wall_budget_s
            )
            # Exclusive creation protects a frozen plan from accidental replacement.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
        else:
            validate_record(load(args.record))
            print("Valid fast check; full ECI NOT_RUN.")
        return 0
    except (ValueError, OSError, TypeError, KeyError):
        print(
            "Fast-check operation failed; check the local input contract.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
