"""Measured ECI dataset in eci_tuning_data/: schema, receipts and observer path.

Offline checks only. No model runtime, server, browser or network call.
The folder is a superset of the bundled catalog: catalog runs are carried over
unchanged and bound as whole records to the catalog file they came from (its
SHA-256 is in the receipt). Every number in a measured run must be bound, field
by field, to a receipt value in eci_tuning_data/receipts.json. A fast-check
record is bound as a whole by record_id.
"""

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import eci_fast_check as fast
import eci_tuning as e

DATA = e.ROOT / "eci_tuning_data"
MEASUREMENT_KINDS = ("component_suite", "fast_check_screening")
MEASUREMENT_FIELDS = (
    "command",
    "machine",
    "requested_model",
    "served_model",
    "model_file",
    "model_sha256",
    "seed",
    "wall_time_s",
    "start_utc",
    "end_utc",
    "evidence",
)
DATASET_FIELDS = ("id", "sha256", "n")
MEASURED_RUN = "mac-glimmer-pipecfg2"


def load(name):
    with open(DATA / name, encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=fast.unique_object)


# A stdlib subset of JSON Schema 2020-12. It refuses any keyword it does not
# implement, so an unsupported schema construct fails instead of passing.
SUPPORTED = {
    "$schema",
    "$id",
    "title",
    "description",
    "$defs",
    "$ref",
    "type",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "properties",
    "required",
    "minProperties",
    "maxProperties",
    "additionalProperties",
    "anyOf",
    "oneOf",
    "allOf",
    "not",
    "if",
    "then",
    "else",
    "format",
    "$comment",
}
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def is_type(value, name):
    if name == "integer":
        return type(value) is int or (type(value) is float and value.is_integer())
    if name == "number":
        return type(value) in (int, float)
    return type(value) is TYPES[name]


def schema_errors(value, schema, document, path="$"):
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: false schema"]
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise AssertionError(f"unsupported schema keywords {sorted(unknown)} at {path}")
    errors = []
    if "$ref" in schema:
        target, _, fragment = schema["$ref"].partition("#")
        if target:
            document = json.loads((e.ROOT / target).read_text(encoding="utf-8"))
        node = document
        for part in filter(None, fragment.split("/")):
            node = node[part]
        errors += schema_errors(value, node, document, path)
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(is_type(value, name) for name in names):
            return errors + [f"{path}: type {type(value).__name__} not in {names}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not in enum")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: const mismatch")
    if type(value) in (int, float):
        for key, bad in (
            ("minimum", lambda v, b: v < b),
            ("maximum", lambda v, b: v > b),
            ("exclusiveMinimum", lambda v, b: v <= b),
            ("exclusiveMaximum", lambda v, b: v >= b),
        ):
            if key in schema and bad(value, schema[key]):
                errors.append(f"{path}: {key}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            errors.append(f"{path}: length")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: pattern")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            errors.append(f"{path}: item count")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(child, sort_keys=True) for child in value]
            if len(set(encoded)) != len(encoded):
                errors.append(f"{path}: duplicate items")
        if "items" in schema:
            for index, child in enumerate(value):
                errors += schema_errors(child, schema["items"], document, f"{path}[{index}]")
    if isinstance(value, dict):
        errors += [f"{path}: missing {key}" for key in schema.get("required", []) if key not in value]
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get("maxProperties", math.inf):
            errors.append(f"{path}: property count")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key in properties:
                errors += schema_errors(child, properties[key], document, f"{path}.{key}")
            elif "additionalProperties" in schema:
                errors += schema_errors(child, schema["additionalProperties"], document, f"{path}.{key}")
    for sub in schema.get("allOf", []):
        errors += schema_errors(value, sub, document, path)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            passed = sum(not schema_errors(value, sub, document, path) for sub in schema[key])
            if passed == 0 or (key == "oneOf" and passed != 1):
                errors.append(f"{path}: {key} matched {passed}")
    if "not" in schema and not schema_errors(value, schema["not"], document, path):
        errors.append(f"{path}: matched not")
    if "if" in schema:
        branch = schema.get("else" if schema_errors(value, schema["if"], document, path) else "then")
        if branch is not None:
            errors += schema_errors(value, branch, document, path)
    return errors


def numbers(value, pointer=""):
    """Every numeric leaf as (JSON pointer, value). Booleans are not numbers."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from numbers(child, f"{pointer}/{key.replace('~', '~0').replace('/', '~1')}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from numbers(child, f"{pointer}/{index}")
    elif type(value) in (int, float):
        yield pointer, value


def resolve(document, pointer):
    node = document
    for part in filter(None, pointer.split("/")):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def receipt_value(receipts, reference):
    receipt_id, _, pointer = reference.partition("#")
    return resolve(receipts["receipts"][receipt_id], pointer)


def catalog_runs(receipt):
    """The catalog's runs as the suite's own ingest normalizes them, or None when the file changed."""
    path = e.ROOT / receipt["file"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != receipt["sha256"]:
        return None
    return {row["run_id"]: row for row in e.ingest([path])["runs"]}


def unreceipted_numbers(dataset, receipts):
    """Problems for numbers without a matching receipt value; empty when every number is bound."""
    problems = []
    for run in dataset["runs"]:
        catalog_id = receipts["bindings"].get("catalog_runs", {}).get(run["run_id"])
        if catalog_id is not None:
            # A catalog run is bound as a whole record to the catalog file it came from.
            source = catalog_runs(receipts["receipts"][catalog_id])
            if source is None:
                problems.append(f"{run['run_id']}: catalog file changed since this folder was built; rebuild it")
            elif source.get(run["run_id"]) != run:
                problems.append(f"{run['run_id']}: differs from its catalog record")
            continue
        bindings = receipts["bindings"]["runs"].get(run["run_id"])
        if bindings is None:
            problems.append(f"{run['run_id']}: run has no receipt bindings")
            continue
        present = dict(numbers(run))
        for pointer, value in present.items():
            if pointer not in bindings:
                problems.append(f"{run['run_id']}{pointer}: number {value!r} has no receipt")
                continue
            try:
                bound = receipt_value(receipts, bindings[pointer])
            except (KeyError, IndexError, ValueError):
                problems.append(f"{run['run_id']}{pointer}: receipt {bindings[pointer]} does not exist")
                continue
            if type(bound) not in (int, float) or bound != value:
                problems.append(f"{run['run_id']}{pointer}: {value!r} differs from receipt value {bound!r}")
        for pointer in set(bindings) - set(present):
            problems.append(f"{run['run_id']}{pointer}: binding for a value that is null or absent")
    for record in dataset.get("fast_checks", []):
        receipt_id = receipts["bindings"]["fast_checks"].get(record["record_id"])
        if receipt_id is None or receipts["receipts"].get(receipt_id, {}).get("record_id") != record["record_id"]:
            problems.append(f"fast check {record['record_id']}: no receipt")
    return problems


class MeasuredDatasetTests(unittest.TestCase):
    def setUp(self):
        self.dataset = load("dataset.json")
        self.receipts = load("receipts.json")
        self.measured = [r["run_id"] for r in self.dataset["runs"]].index(MEASURED_RUN)

    def test_dataset_validates_against_the_published_schema(self):
        schema = json.loads((e.ROOT / "eci_tuning.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema_errors(self.dataset, schema, schema), [])

    def test_schema_check_rejects_a_broken_record(self):
        schema = json.loads((e.ROOT / "eci_tuning.schema.json").read_text(encoding="utf-8"))
        broken = copy.deepcopy(self.dataset)
        broken["runs"][self.measured]["components"]["Winogrande"]["score"] = 1.5
        broken["runs"][self.measured]["unreviewed_field"] = 1
        self.assertEqual(len(schema_errors(broken, schema, schema)), 2)

    def test_suite_ingest_accepts_the_folder_unchanged(self):
        again = e.ingest([DATA / "dataset.json"], require_normalized=True)
        self.assertEqual(again["runs"], self.dataset["runs"])
        self.assertEqual(again["fast_checks"], self.dataset["fast_checks"])
        self.assertEqual(again["rejected_fast_checks"], [])
        self.assertEqual(again["as_of"], self.dataset["as_of"])
        for record in self.dataset["fast_checks"]:
            fast.validate_record(copy.deepcopy(record))

    def test_every_number_has_a_matching_receipt(self):
        self.assertTrue(self.dataset["runs"] or self.dataset["fast_checks"])
        self.assertEqual(unreceipted_numbers(self.dataset, self.receipts), [])

    def test_receipt_guard_catches_unreceipted_or_changed_numbers(self):
        index = self.measured
        added = copy.deepcopy(self.dataset)
        added["runs"][index]["tokens_per_s"] = 15.5
        self.assertEqual(
            unreceipted_numbers(added, self.receipts), [f"{MEASURED_RUN}/tokens_per_s: number 15.5 has no receipt"]
        )
        changed = copy.deepcopy(self.dataset)
        changed["runs"][index]["components"]["GSM8K"]["score"] = 0.5
        self.assertIn("differs from receipt value", " ".join(unreceipted_numbers(changed, self.receipts)))
        unbound = copy.deepcopy(self.receipts)
        unbound["bindings"]["fast_checks"].clear()
        self.assertTrue(unreceipted_numbers(self.dataset, unbound))
        new_run = copy.deepcopy(self.dataset)
        new_run["runs"].append(dict(new_run["runs"][index], run_id="unreceipted"))
        self.assertIn("unreceipted: run has no receipt bindings", unreceipted_numbers(new_run, self.receipts))

    def test_catalog_runs_match_the_catalog_they_came_from(self):
        bound = self.receipts["bindings"]["catalog_runs"]
        receipt = self.receipts["receipts"][set(bound.values()).pop()]
        self.assertEqual(receipt["kind"], "bundled_catalog")
        source = catalog_runs(receipt)
        self.assertIsNotNone(source, "catalog file changed since this folder was built; rebuild it")
        self.assertEqual(set(bound), set(source))
        self.assertEqual(sorted(receipt["run_ids"]), sorted(source))
        edited = copy.deepcopy(self.dataset)
        index = [r["run_id"] for r in edited["runs"]].index("pc-llama31-baseline-max1024")
        edited["runs"][index]["eci_posterior_mean"] = 120.0
        self.assertEqual(
            unreceipted_numbers(edited, self.receipts), ["pc-llama31-baseline-max1024: differs from its catalog record"]
        )
        moved = copy.deepcopy(self.receipts)
        moved["receipts"][set(bound.values()).pop()]["sha256"] = "0" * 64
        self.assertIn("catalog file changed", " ".join(unreceipted_numbers(self.dataset, moved)))

    def test_measurement_receipts_record_identity_data_and_timing(self):
        measured = [r for r in self.receipts["receipts"].values() if r["kind"] in MEASUREMENT_KINDS]
        self.assertTrue(measured)
        for receipt in measured:
            with self.subTest(receipt=receipt["kind"]):
                for field in MEASUREMENT_FIELDS:
                    self.assertIsNotNone(receipt.get(field), field)
                # Requested and served model stay separate fields, even when equal.
                self.assertIn("requested_model", receipt)
                self.assertIn("served_model", receipt)
                self.assertRegex(receipt["model_sha256"], r"^[0-9a-f]{64}$")
                for evidence in receipt["evidence"]:
                    self.assertRegex(evidence["sha256"], r"^[0-9a-f]{64}$")
                    self.assertNotIn("/", evidence["file"])
                parts = receipt["components"] if receipt["kind"] == "component_suite" else receipt["benchmarks"]
                self.assertTrue(parts)
                for name, part in parts.items():
                    for field in DATASET_FIELDS:
                        self.assertIsNotNone(part["dataset"].get(field), f"{name} dataset {field}")
                    self.assertRegex(part["dataset"]["sha256"], r"^[0-9a-f]{64}$")
                    for field in ("n", "wall_time_s", "tokens_per_s"):
                        self.assertIsNotNone(part.get(field), f"{name} {field}")

    def test_fast_check_receipt_matches_the_record(self):
        for record in self.dataset["fast_checks"]:
            receipt = self.receipts["receipts"][self.receipts["bindings"]["fast_checks"][record["record_id"]]]
            self.assertEqual(receipt["kind"], "fast_check_screening")
            self.assertEqual(receipt["served_model"], record["model"]["name"])
            self.assertEqual(receipt["model_sha256"], record["model"]["file_sha256"])
            self.assertEqual(receipt["wall_time_s"], record["wall_time_s"])
            self.assertEqual(receipt["plan_id"], record["plan"]["plan_id"])
            self.assertEqual(receipt["seed"], record["runtime_settings"]["seed"])
            samples = {spec["benchmark"]: spec for spec in record["plan"]["benchmarks"]}
            results = {row["benchmark"]: row for row in record["benchmarks"]}
            self.assertEqual(set(receipt["benchmarks"]), set(results))
            for name, part in receipt["benchmarks"].items():
                self.assertEqual(part["dataset"]["sha256"], samples[name]["dataset_sha256"])
                self.assertEqual(part["dataset"]["population"], samples[name]["population"])
                self.assertEqual(part["n"], len(samples[name]["item_ids"]))
                for field in ("correct", "parsed", "wall_time_s", "tokens_per_s", "accuracy"):
                    self.assertEqual(part[field], results[name][field], f"{name} {field}")

    def test_no_eci_improvement_or_release_claim(self):
        catalog = self.receipts["bindings"]["catalog_runs"]
        # Only catalog runs may carry an ECI; the measured runs here computed none.
        self.assertEqual(
            {r["run_id"] for r in self.dataset["runs"] if r["status"] == "COMPUTED"},
            {"pc-llama31-baseline-max1024", "pc-llama31-pipeline-lenient"},
        )
        for run in self.dataset["runs"]:
            if run["run_id"] in catalog:
                continue
            with self.subTest(run=run["run_id"]):
                self.assertNotEqual(run["status"], "COMPUTED")
                self.assertEqual([run[k] for k in ("eci_posterior_mean", "eci_p05", "eci_p95")], [None] * 3)
                gates = [
                    self.receipts["receipts"][r]
                    for r in self.receipts["run_receipts"][run["run_id"]]
                    if self.receipts["receipts"][r]["kind"] == "eci_fit_gate"
                ]
                self.assertEqual([g["result"] for g in gates], ["ECI_NOT_COMPUTED"])
        for record in self.dataset["fast_checks"]:
            self.assertEqual({c["verdict"] for c in record["comparison"].values()}, {"INCONCLUSIVE"})
        self.assertIn("no release claim", self.receipts["claims"].lower())

    def test_receipts_hold_no_private_paths(self):
        text = (DATA / "receipts.json").read_text(encoding="utf-8") + (DATA / "dataset.json").read_text(
            encoding="utf-8"
        )
        for marker in ("/Users/", "/Volumes/", "/home/", "\\\\Users\\\\", "/private/"):
            self.assertNotIn(marker, text)

    def test_configured_observer_reads_this_folder(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ,
                {"AGIW_ECI_TUNING_DATASET_DIR": str(DATA), "AGIW_ECI_TUNING_CONFIG": str(Path(temp) / "absent.json")},
            ),
        ):
            response = e.status()
        self.assertEqual(response["dataset"]["status"], "configured", response["dataset"])
        self.assertEqual(response["dataset"]["as_of"], self.dataset["as_of"])
        self.assertEqual(
            {r["run_id"] for r in response["pending"]},
            {r["run_id"] for r in self.dataset["runs"] if r["status"] != "COMPUTED"},
        )
        views = response["rankings"]["fast_check"]["hardware_views"]
        self.assertEqual(
            [v["hardware"]["machine_label"] for v in views],
            [r["hardware"]["machine_label"] for r in self.dataset["fast_checks"]],
        )
        self.assertNotIn("eci_posterior_mean", json.dumps(response["rankings"]["fast_check"]))
        self.assertEqual(response["runtime_activation"]["status"], "NOT_APPLIED")

    def test_configured_view_keeps_the_bundled_catalog(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(os.environ, {"AGIW_ECI_TUNING_CONFIG": str(Path(temp) / "absent.json")}),
        ):
            os.environ.pop("AGIW_ECI_TUNING_DATASET_DIR", None)
            bundled = e.status()
            os.environ["AGIW_ECI_TUNING_DATASET_DIR"] = str(DATA)
            configured = e.status()
        self.assertEqual(bundled["dataset"]["status"], "bundled")
        self.assertEqual(configured["dataset"]["status"], "configured", configured["dataset"])
        for cls in ("baseline_protocol", "pipeline_configured"):
            self.assertEqual(configured["rankings"][cls]["eci"], bundled["rankings"][cls]["eci"], cls)
        baseline = {r["run_id"]: r for r in configured["rankings"]["baseline_protocol"]["eci"]}
        self.assertEqual(round(baseline["pc-llama31-baseline-max1024"]["eci_posterior_mean"], 2), 105.38)
        pending = {r["run_id"]: r for r in configured["pending"]}
        self.assertLessEqual({r["run_id"] for r in bundled["pending"]}, set(pending))
        self.assertEqual(pending[MEASURED_RUN]["status"], "NOT_COMPUTED")
        self.assertIsNone(pending[MEASURED_RUN]["eci_posterior_mean"])


if __name__ == "__main__":
    unittest.main()
