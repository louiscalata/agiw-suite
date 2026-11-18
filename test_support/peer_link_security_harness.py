"""Run deterministic peer input differentials and in-memory guard mutations.

Run from the repository root with ``python -B -m test_support.peer_link_security_harness``.
The transport fixtures never bind or connect sockets, and mutations never write production files.
"""

import hashlib
import io
import json
import random
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

import peer_link as pl
import test_peer_link_security as regression

SAMPLES = 4096


def _legacy_models(payload):
    """The earlier valid-inventory projection, frozen for compatibility comparison."""
    models = []
    for item in (payload.get("data") or [])[:32]:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("type") in (None, "llm", "vlm"):
            state = {"loaded": "loaded", "not-loaded": "inactive"}.get(item.get("state"), "unknown")
            models.append({"id": item["id"][:256], "host": "mac", "loadedState": state})
    return models


def inventory_differential():
    rng = random.Random(617)
    count = 0
    for index in range(SAMPLES):
        entries = [
            {
                "id": "m" + chr(97 + rng.randrange(26)) * rng.randrange(1, 300),
                "type": rng.choice([None, "llm", "vlm", "embeddings"]),
                "state": rng.choice([None, "loaded", "not-loaded", "other"]),
            }
            for _ in range(rng.randrange(49))
        ]
        payload = {"data": entries}
        raw = json.dumps(payload).encode()
        with mock.patch.object(pl.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
            actual = pl.lmstudio_status()()
        if actual["health"] != "ok" or actual["models"] != _legacy_models(payload):
            raise AssertionError(("valid inventory compatibility", index))
        count += 1
    return count


def invalid_inventory_differential():
    rng = random.Random(829)
    count = 0
    for index in range(SAMPLES):
        bad = rng.choice(
            [None, False, "model", {}, {"id": 42}, {"id": ""}, {"id": "   "}, {"id": "model\n"}, {"id": "\u200b"}]
        )
        branch = index % 5
        if branch == 0:
            payload = {"error": "backend unavailable", "data": []}
        elif branch == 1:
            payload = {"data": [{"id": "valid"}] * rng.randrange(33, 49) + [bad]}
        elif branch == 2:
            payload = {"data": rng.choice([None, False, "", "bad", {}, 0])}
        elif branch == 3:
            payload = {"metadata": {"sequence": index}}
        else:
            payload = rng.choice([[], None, "invalid", index])
        with mock.patch.object(pl.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            try:
                pl.lmstudio_status()()
            except ValueError:
                count += 1
            else:
                raise AssertionError(("malformed inventory accepted", index))
    return count


def uuid_differential():
    rng = random.Random(947)
    count = 0
    for index in range(SAMPLES):
        valid = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        text = rng.choice([valid, valid + "\n", "x" + valid, valid.upper(), valid[:-1], "{" + valid + "}"])
        try:
            value = uuid.UUID(text)
            expected = str(value) == text and value.version == 4
        except ValueError:
            expected = False
        actual = pl.read_hello(pl.make_hello(text, "windows", "peer", 8768, 100.0))
        if (actual is not None) != expected:
            raise AssertionError(("UUID grammar", index, repr(text)))
        count += 1
    return count


def file_differential():
    rng = random.Random(1093)
    count = 0
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "status.json"
        provider = pl.file_status(path)
        for index in range(SAMPLES):
            payload = {"health": rng.choice(["ok", "down", "unknown"]), "models": [], "sequence": index}
            branch = index % 4
            if branch == 0:
                raw = json.dumps(payload).encode()
            elif branch == 1:
                raw = json.dumps(payload).encode().ljust(65536, b" ")
            elif branch == 2:
                raw = json.dumps(payload).encode().ljust(65537 + rng.randrange(16), b" ")
            else:
                raw = ('{"health":"down","health":"ok","sequence":' + str(index) + "}").encode()
            path.write_bytes(raw)
            try:
                actual = provider()
            except ValueError:
                if branch not in (2, 3):
                    raise
            else:
                if branch in (2, 3) or actual != payload:
                    raise AssertionError(("status file byte/key policy", index))
            count += 1
    return count


def _replace_in_function(source, name, before, after, occurrence=0):
    start = source.index("def " + name + "(")
    following = source.find("\ndef ", start + 1)
    if following < 0:
        following = len(source)
    segment = source[start:following]
    positions = []
    offset = 0
    while True:
        position = segment.find(before, offset)
        if position < 0:
            break
        positions.append(position)
        offset = position + len(before)
    if occurrence >= len(positions):
        raise AssertionError(("mutation anchor missing", name, before, occurrence))
    position = positions[occurrence]
    segment = segment[:position] + after + segment[position + len(before) :]
    return source[:start] + segment + source[following:]


def _mutations():
    return [
        (
            "LAN provider wait bypass",
            "_status",
            "status = link._bounded_self_status()",
            "status = project_status(validate_status(link.status_provider(), link.host, time.time()))",
            0,
            "StatusDeadlineSecurity.test_blocked_provider_releases_request_slots_and_recovers",
        ),
        (
            "inventory error envelope accepted",
            "lmstudio_status",
            'or "error" in payload',
            "or False",
            0,
            "StatusInputsSecurity.test_lmstudio_rejects_error_responses",
        ),
        (
            "blank model identity accepted",
            "lmstudio_status",
            'or not item["id"].strip()',
            "or False",
            0,
            "StatusInputsSecurity.test_lmstudio_rejects_blank_or_control_character_model_ids",
        ),
        (
            "control characters in model identity accepted",
            "lmstudio_status",
            'or not item["id"].isprintable()',
            "or False",
            0,
            "StatusInputsSecurity.test_lmstudio_rejects_blank_or_control_character_model_ids",
        ),
        (
            "duplicate inventory keys accepted",
            "lmstudio_status",
            "payload = strict_json(raw)",
            "payload = json.loads(raw)",
            0,
            "StatusInputsSecurity.test_lmstudio_rejects_duplicate_inventory_keys",
        ),
        (
            "inventory byte ceiling bypass",
            "lmstudio_status",
            "if len(raw) > MAX_STATUS_BYTES:",
            "if False:",
            0,
            "StatusInputsSecurity.test_lmstudio_rejects_oversized_json_whitespace",
        ),
        (
            "inventory overflow sentinel omitted",
            "lmstudio_status",
            "response.read(MAX_STATUS_BYTES + 1)",
            "response.read(MAX_STATUS_BYTES)",
            0,
            "StatusInputsSecurity.test_lmstudio_rejects_oversized_reply_with_valid_prefix",
        ),
        (
            "unpublished inventory tail unchecked",
            "lmstudio_status",
            'for item in payload["data"]',
            'for item in payload["data"][:MAX_MODELS]',
            0,
            "StatusInputsSecurity.test_lmstudio_checks_entries_beyond_published_slice",
        ),
        (
            "duplicate file keys accepted",
            "file_status",
            "return strict_json(raw)",
            "return json.loads(raw)",
            0,
            "StatusInputsSecurity.test_file_status_rejects_duplicate_health_keys",
        ),
        (
            "file overflow sentinel omitted",
            "file_status",
            "source.read(MAX_STATUS_BYTES + 1)",
            "source.read(MAX_STATUS_BYTES)",
            0,
            "StatusInputsSecurity.test_file_status_rejects_oversized_file_with_valid_prefix",
        ),
        (
            "file byte ceiling bypass",
            "file_status",
            "if len(raw) > MAX_STATUS_BYTES:",
            "if False:",
            0,
            "StatusInputsSecurity.test_file_status_rejects_oversized_json_whitespace",
        ),
        (
            "hello UUID end match relaxed",
            "read_hello",
            "UUID_RE.fullmatch(node)",
            "UUID_RE.match(node)",
            0,
            "PairingRevocationSecurity.test_hello_rejects_uuid_with_trailing_newline",
        ),
        (
            "invite UUID end match relaxed",
            "on_notify",
            "UUID_RE.fullmatch(node)",
            "UUID_RE.match(node)",
            0,
            "PairingRevocationSecurity.test_invite_rejects_uuid_with_trailing_newline",
        ),
        (
            "initial finish trust recheck omitted",
            "on_finish",
            "if self._pairing_current(done):",
            "if True:",
            1,
            "PairingRevocationSecurity.test_first_finish_revoked_while_notifying_waiters_is_refused",
        ),
        (
            "retry trust recheck omitted",
            "on_finish",
            "if self._pairing_current(done):",
            "if True:",
            0,
            "PairingRevocationSecurity.test_finish_retry_revoked_while_waiting_is_refused",
        ),
        (
            "retry accepts replacement link key",
            "_pairing_current",
            'and hmac.compare_digest(current["key"], done["key"])',
            "and True",
            0,
            "PairingRevocationSecurity.test_finish_retry_requires_the_original_link_key",
        ),
        (
            "revoked finish restores trust",
            "on_finish",
            'if self._completed.get(record["id"]) is not done:',
            "if False:",
            0,
            "PairingRevocationSecurity.test_unpair_between_finish_transition_and_pin_cannot_restore_trust",
        ),
        (
            "control operation opens unauthenticated connection",
            "control_request",
            "connection=conn,",
            "connection=None,",
            1,
            "ControlConnectionSecurity.test_valid_control_exchange_uses_one_authenticated_connection",
        ),
        (
            "control hello terminates authenticated connection",
            "do_GET",
            "self.close_connection = False",
            "self.close_connection = True",
            0,
            "ControlConnectionSecurity.test_daemon_answers_authenticated_operation_on_hello_connection",
        ),
    ]


def mutation_witnesses():
    path = Path(pl.__file__)
    original = path.read_text(encoding="utf-8")
    records = []
    for label, function, before, after, occurrence, witness in _mutations():
        source = _replace_in_function(original, function, before, after, occurrence)
        module = types.ModuleType("peer_input_mutation")
        module.__file__ = str(path)
        exec(compile(source, str(path), "exec"), module.__dict__)
        result = unittest.TestResult()
        suite = unittest.defaultTestLoader.loadTestsFromName(witness, regression)
        with mock.patch.object(regression, "pl", module):
            suite.run(result)
        if result.testsRun != 1 or result.errors or not result.failures:
            raise AssertionError(("guard mutation lacks an assertion witness", label, result.errors, result.failures))
        records.append(
            {"mutation": label, "test": witness, "outcome": "killed", "assertion_failures": len(result.failures)}
        )
    if path.read_text(encoding="utf-8") != original:
        raise AssertionError("mutation changed source bytes")
    return records


def main():
    result = {
        "source_sha256": hashlib.sha256(Path(pl.__file__).read_bytes()).hexdigest(),
        "differential_cases": {
            "valid_inventory_legacy_parity": inventory_differential(),
            "invalid_inventory_rejection": invalid_inventory_differential(),
            "canonical_uuid_policy": uuid_differential(),
            "file_byte_and_key_policy": file_differential(),
        },
        "mutations": mutation_witnesses(),
    }
    result["differential_total"] = sum(result["differential_cases"].values())
    result["mutants_killed"] = len(result["mutations"])
    sys.stdout.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
