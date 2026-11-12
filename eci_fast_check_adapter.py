"""Opt-in HTTP adapter for agiw-fast-check-v1; Python 3.9, stdlib only.

Consumes an already running, explicitly named loopback server. It owns neither
the server nor its configuration. Run ``python3 -B -m eci_fast_check_adapter -h``.
"""

import argparse
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import eci_fast_check as fast
from remote_machines import PROTECTED_PORTS


DEFAULT_MAX_TOKENS = 16384
MIN_MAX_TOKENS = 8192
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SCORER_ID = "fast-check-strict-final-v1"
PROMPT_ID = "fast-check-question-choices-v1"
SYSTEM_MESSAGE = (
    "Reason as needed, then give the final answer in the requested format. "
    "Do not put an explanation in the final answer."
)


class ResponseError(Exception):
    """Invalid HTTP/API evidence; its body is never copied into results."""


def validate_base_url(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (ValueError, TypeError):
        raise fast.ValidationError("invalid base URL") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.netloc != "127.0.0.1:" + str(port)
        or parsed.path not in ("", "/", "/v1", "/v1/")
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
        or any(c.isspace() or ord(c) < 32 for c in url)
    ):
        raise fast.ValidationError(
            "base URL must be http://127.0.0.1:PORT with optional /v1"
        )
    if port in PROTECTED_PORTS:
        raise fast.ValidationError("protected server port")
    return "127.0.0.1", port, parsed.path.rstrip("/")


def safe_text(value):
    """Keep short descriptive facts, excluding paths and identity-like values.

    No raw props, command lines, machine names, UUIDs, addresses or device IDs
    are retained. This allowlist deliberately loses unusual server descriptions.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    value = value.strip()
    if (
        re.search(r"[\\/@\x00-\x1f]|\d+\.\d+\.\d+\.\d+|[0-9a-f]{12,}", value, re.I)
        or re.search(r"serial|uuid|hostname|device.?id|://", value, re.I)
        or not re.fullmatch(r"[\w .()+,:-]+", value, re.ASCII)
    ):
        return None
    return value


def positive_int(value):
    return value if type(value) is int and value > 0 else None


def model_name(value):
    fast.string(value)
    if (
        len(value) > 256
        or value.startswith(("/", ".", "\\"))
        or ".." in value
        or not re.fullmatch(r"[A-Za-z0-9_./:-]+", value)
        or re.search(r"\d+\.\d+\.\d+\.\d+|://", value)
    ):
        raise fast.ValidationError("model id must be a public model identifier, not a path or address")
    return value


def choices_for(row):
    values = row.get("choices")
    if values is None:
        return []
    if not isinstance(values, list) or not 2 <= len(values) <= 26:
        raise fast.ValidationError("choices must contain 2 to 26 options")
    result = []
    for index, value in enumerate(values):
        if isinstance(value, str):
            label, text = chr(65 + index), value
        elif isinstance(value, dict) and set(value) == {"label", "text"}:
            label, text = value["label"], value["text"]
        else:
            raise fast.ValidationError("choice must be text or a label/text object")
        if not isinstance(label, str) or not re.fullmatch(r"[A-Z0-9]", label):
            raise fast.ValidationError("choice label must be one uppercase letter or digit")
        fast.string(text)
        result.append((label, text))
    if len({label for label, _ in result}) != len(result):
        raise fast.ValidationError("duplicate choice label")
    return result


NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def number_answer(text):
    if not isinstance(text, str):
        return None
    # Only a complete final answer, or the final explicitly marked line, counts.
    text = text.strip()
    marked = re.search(r"(?:^|\n)(?:####|Final answer:)\s*(" + NUMBER + r")\s*\Z", text, re.I)
    token = marked.group(1) if marked else text
    if not re.fullmatch(NUMBER, token) or len(token) > 128:
        return None
    try:
        value = Decimal(token)
        if not value.is_finite() or abs(value.adjusted()) > 100:
            return None
        # Decimal.normalize() rounds to the process precision; scoring must
        # preserve every supplied digit instead.
        result = format(value, "f")
        if "." in result:
            result = result.rstrip("0").rstrip(".")
        if value == 0:
            result = "0"
        return result if len(result) <= 128 else None
    except (InvalidOperation, ValueError):
        return None


def reference(row, benchmark):
    fast.string(row.get("question"))
    answer = row.get("answer")
    fast.string(answer)
    options = choices_for(row)
    if options:
        if answer not in {label for label, _ in options}:
            raise fast.ValidationError("reference answer must be a choice label")
    elif benchmark == "gsm8k":
        answer = number_answer(answer)
        if answer is None:
            raise fast.ValidationError("GSM8K reference requires a numeric final answer")
    if len(answer) > 128 or answer != answer.strip() or any(ord(c) < 32 for c in answer):
        raise fast.ValidationError("reference must be a short canonical answer")
    return answer


def prompt_for(row, benchmark):
    options = choices_for(row)
    text = row["question"]
    if options:
        text += "\n\n" + "\n".join(label + ". " + value for label, value in options)
        text += "\n\nGive only the choice label as your final answer."
    elif benchmark == "gsm8k":
        text += "\n\nEnd with 'Final answer: <number>'."
    else:
        text += "\n\nGive only the short answer as your final answer."
    return text


def extract_answer(content, row, benchmark):
    options = choices_for(row)
    text = content.strip()
    if options:
        labels = "".join(label for label, _ in options)
        matched = re.fullmatch(
            r"(?:(?:Final answer|Answer):\s*)?(?:([" + labels + r"])|\(([" + labels + r"])\))[.]?",
            text, re.I
        )
        return (matched.group(1) or matched.group(2)).upper() if matched else None
    if benchmark == "gsm8k":
        return number_answer(text)
    return text if text and len(text) <= 128 and not any(ord(c) < 32 for c in text) else None


class OpenAIAdapter:
    def __init__(self, base_url, model_id, *, max_tokens=DEFAULT_MAX_TOKENS, timeout=180):
        self.host, self.port, self.prefix = validate_base_url(base_url)
        self.model_id = model_name(model_id)
        fast.numeric(max_tokens, MIN_MAX_TOKENS, integer=True)
        fast.numeric(timeout, low=0.001)
        self.max_tokens, self.timeout = max_tokens, timeout
        self.facts = {}
        self.available_memory_bytes = None
        self.ctx = None
        self.backend_version = None

    def request(self, method, path, payload=None, *, optional=False):
        # A direct numeric connection ignores proxy environment variables and
        # http.client never follows redirects. No other host is ever resolved.
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        deadline = time.monotonic() + self.timeout
        expired = threading.Event()
        timer = None
        try:
            connection.connect()
            transport = connection.sock

            def expire():
                expired.set()
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            # A socket inactivity timeout alone permits slow-drip headers/body
            # to run forever. Shutdown this request's socket at the total
            # deadline, even after HTTPConnection relinquishes it to a response.
            timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            body = None if payload is None else fast.canonical(payload).encode("utf-8")
            connection.request(method, path, body=body, headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            if expired.is_set():
                raise TimeoutError("HTTP deadline")
            if optional and response.status in (404, 405):
                return None
            if response.status != 200:
                raise ResponseError("unexpected HTTP status")
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTP deadline")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if expired.is_set():
                    raise TimeoutError("HTTP deadline")
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ResponseError("HTTP response too large")
                if not chunk:
                    break
                chunks.append(chunk)
            try:
                value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=fast.unique_object)
                fast.json_value(value)
                if not isinstance(value, dict):
                    raise ValueError("response must be an object")
                return value
            except (ValueError, UnicodeError, RecursionError):
                raise ResponseError("invalid JSON response") from None
        except socket.timeout:
            # socket.timeout was not a TimeoutError alias until Python 3.10.
            raise TimeoutError("HTTP deadline") from None
        except (OSError, http.client.HTTPException):
            if expired.is_set():
                raise TimeoutError("HTTP deadline") from None
            raise
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

    def discover(self):
        props = self.request("GET", "/props", optional=True)
        models = self.request("GET", "/v1/models", optional=True)
        sources = []
        reported = []
        selected_meta = {}
        if models is not None:
            entries = models.get("data")
            if not isinstance(entries, list):
                raise ResponseError("invalid model listing")
            matches = [m for m in entries if isinstance(m, dict) and m.get("id") == self.model_id]
            if len(matches) != 1:
                raise ResponseError("requested model is not uniquely advertised")
            selected = matches[0]
            selected_meta = selected.get("meta", {})
            reported.append(selected_meta)
            sources.append("/v1/models")
        if props is not None:
            declared = props.get("model")
            if isinstance(declared, dict):
                if declared.get("id") not in (None, self.model_id):
                    raise ResponseError("props model disagrees with requested model")
                reported.append(declared)
            sources.append("/props")
        if not sources or (models is None and not (
            isinstance(props.get("model"), dict) and props["model"].get("id") == self.model_id
        )):
            raise ResponseError("server did not advertise the requested model")
        model = dict(name=self.model_id, file_sha256=None, quantization=None, file_size_bytes=None)
        for item in reported:
            if not isinstance(item, dict):
                raise ResponseError("invalid model metadata")
            values = {
                "file_sha256": item.get("sha256", item.get("file_sha256")),
                "quantization": safe_text(item.get("quantization")),
                "file_size_bytes": positive_int(item.get("file_size_bytes")),
            }
            if values["file_sha256"] is not None:
                try:
                    fast.sha(values["file_sha256"])
                except fast.ValidationError:
                    raise ResponseError("invalid advertised artifact hash") from None
            for key, value in values.items():
                if value is not None:
                    if model[key] not in (None, value):
                        raise ResponseError("conflicting model facts")
                    model[key] = value
        # Proxies may expose the same allowlisted facts in model metadata.
        # Never retain the raw object.
        facts = dict(selected_meta)
        facts.update(props or {})
        hardware = facts.get("hardware", {})
        if not isinstance(hardware, dict):
            raise ResponseError("invalid hardware metadata")
        build = facts.get("build_info", {})
        if isinstance(build, str):
            self.backend_version = safe_text(build)
        elif isinstance(build, dict):
            self.backend_version = safe_text(build.get("version"))
        generation = facts.get("default_generation_settings", {})
        if isinstance(generation, dict):
            self.ctx = positive_int(generation.get("n_ctx"))
        memory = positive_int(hardware.get("memory_bytes"))
        result = dict(
            machine_label=None, cpu=safe_text(hardware.get("cpu")),
            gpu=safe_text(hardware.get("gpu")),
            memory={"bytes": memory} if memory else None,
            memory_bytes=memory, runtime_build_id=self.backend_version,
            facts_source="server-reported; not independently attested",
        )
        self.available_memory_bytes = positive_int(hardware.get("available_memory_bytes"))
        self.facts = dict(metadata_sources=sources, model_id=self.model_id,
                          interface="openai-compatible", endpoint_port=self.port)
        return model, result

    def answer(self, row, *, benchmark, spec):
        expected = reference(row, benchmark)
        start = time.monotonic()
        reply = self.request("POST", (self.prefix or "/v1") + "/chat/completions", {
            "model": self.model_id, "messages": [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": prompt_for(row, benchmark)},
            ], "temperature": 0, "seed": 42, "max_tokens": self.max_tokens,
            "stop": [], "stream": False,
        })
        if reply.get("model") not in (None, self.model_id):
            raise ResponseError("completion model disagrees with requested model")
        choices = reply.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ResponseError("expected one completion")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ResponseError("missing completion message")
        content, reasoning = message.get("content"), message.get("reasoning_content")
        if content is None:
            content = ""
        if not isinstance(content, str) or (reasoning is not None and not isinstance(reasoning, str)):
            raise ResponseError("invalid completion content")
        finish = choice.get("finish_reason")
        if finish is not None and (not isinstance(finish, str) or not finish.strip()):
            raise ResponseError("invalid finish reason")
        sample = reply.get("sample", {})
        choice_sample = choice.get("sample", {})
        if not isinstance(sample, dict) or not isinstance(choice_sample, dict):
            raise ResponseError("invalid sample limit evidence")
        cut_off = (
            finish in fast.TOKEN_LIMIT_REASONS
            or sample.get("limit") is not None
            or choice_sample.get("limit") is not None
            or (not content.strip() and bool(reasoning and reasoning.strip()))
        )
        value = extract_answer(content, row, benchmark)
        outcome = ("truncated_empty" if cut_off else "unparsed") if value is None else (
            "correct" if value == expected else "wrong"
        )
        usage = reply.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise ResponseError("invalid usage")
        counts = {}
        for key in ("prompt_tokens", "completion_tokens"):
            count = usage.get(key)
            if count is not None and (type(count) is not int or count < 0):
                raise ResponseError("invalid token measurement")
            counts[key] = count
        return dict(
            outcome=outcome, parsed=value is not None, finish_reason=finish,
            cut_off=bool(cut_off), answer_value=value, error=None,
            seconds=max(0, time.monotonic() - start), **counts,
        )


def atomic_json(path, value):
    """Atomic final/plan publication; the item ledger is separately fsynced."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + "-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@contextmanager
def output_lock(output):
    """Own plan, checkpoint and final publication as one nonblocking operation."""
    with (output / "adapter.lock").open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise fast.ValidationError("output directory already locked by another writer") from None
        yield


def run(base_url, model_id, sample_files, output_dir, *, count=200,
        max_tokens=DEFAULT_MAX_TOKENS, timeout=180, model_sha256=None,
        file_size_bytes=None, quantization=None, available_memory_bytes=None,
        wall_budget_s=None):
    adapter = OpenAIAdapter(base_url, model_id, max_tokens=max_tokens, timeout=timeout)
    datasets = {}
    for path in sample_files:
        data = fast.load(path)
        if not isinstance(data, dict) or datasets.keys() & data.keys():
            raise fast.ValidationError("sample files must have distinct benchmark descriptors")
        datasets.update(data)
    plan = fast.make_plan(datasets, count=count, wall_budget_s=wall_budget_s)
    # Validate all scorer inputs before contacting the server, including items
    # outside the selected sample (they participate in the dataset identity).
    for benchmark, source in datasets.items():
        for row in source["items"]:
            reference(row, benchmark)
    model, hardware = adapter.discover()
    supplied = dict(file_sha256=model_sha256, file_size_bytes=file_size_bytes, quantization=quantization)
    for key, value in supplied.items():
        if value is not None:
            if model[key] not in (None, value):
                raise fast.ValidationError("explicit model fact disagrees with server")
            model[key] = value
    if model["file_sha256"] is None:
        raise fast.ValidationError("resumable checks require a server artifact SHA-256 or --model-sha256")
    fast.sha(model["file_sha256"])
    if model["quantization"] is not None and safe_text(model["quantization"]) != model["quantization"]:
        raise fast.ValidationError("quantization must be a short public descriptor")
    available = adapter.available_memory_bytes if available_memory_bytes is None else available_memory_bytes
    pre = fast.pre_check(model["file_size_bytes"], model["quantization"], available)
    if pre["decision"] != "CHECK":
        raise fast.ValidationError("fit gate requires known model size, quantization and sufficient available memory")
    settings = dict(
        scorer_id=SCORER_ID, prompt_template_id=PROMPT_ID, backend="openai-compatible",
        backend_version=adapter.backend_version, temperature=0, max_tokens=max_tokens,
        ctx=adapter.ctx, seed=42, stop=[], system_message=SYSTEM_MESSAGE, n_epochs=1,
        server_facts=adapter.facts,
        explicit_model_facts=sorted(k for k, v in supplied.items() if v is not None),
        available_memory_source="explicit" if available_memory_bytes is not None else "server-reported",
        request_timeout_s=timeout,
        pre_inputs=pre["inputs"],
    )
    fast.identity(model, hardware, settings)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    with output_lock(output):
        plan_path = output / "plan.json"
        if plan_path.exists():
            if fast.load(plan_path) != plan:
                raise fast.ValidationError("output directory contains a different plan")
        else:
            atomic_json(plan_path, plan)
        record = fast.run_check(
            plan, datasets, adapter.answer, model=model, hardware=hardware,
            runtime_settings=settings, pre=pre, checkpoint=output / "checkpoint.jsonl",
        )
        atomic_json(output / "result.json", record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--samples", nargs="+", type=Path, required=True, help="local JSON benchmark descriptor files")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--timeout", type=float, default=180, help="per-request deadline in seconds")
    parser.add_argument("--wall-budget-s", help="exact positive decimal; checked between items")
    parser.add_argument("--model-sha256", help="actual artifact hash when server omits it")
    parser.add_argument("--file-size-bytes", type=int)
    parser.add_argument("--quantization")
    parser.add_argument("--available-memory-bytes", type=int)
    args = parser.parse_args(argv)
    try:
        record = run(
            args.base_url, args.model, args.samples, args.output_dir, count=args.count,
            max_tokens=args.max_tokens, timeout=args.timeout, wall_budget_s=args.wall_budget_s,
            model_sha256=args.model_sha256, file_size_bytes=args.file_size_bytes,
            quantization=args.quantization, available_memory_bytes=args.available_memory_bytes,
        )
        print("Fast-check result written; full ECI NOT_RUN. Post: " + record["post"]["decision"])
        return 0
    except KeyboardInterrupt:
        print("Interrupted; durable checkpoint retained. Repeat the same command to resume.", file=sys.stderr)
        return 130
    except (ValueError, TypeError, KeyError, OSError, ResponseError, http.client.HTTPException) as exc:
        # Never echo untrusted server responses, sample content or local paths.
        message = str(exc) if isinstance(exc, fast.ValidationError) else type(exc).__name__
        print("Fast-check adapter failed: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
