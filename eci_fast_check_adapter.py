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
SCORER_ID = "fast-check-screening-final-v3"
PARSE_VERSION = "fast-check-parse-v3"
PROMPT_ID = "fast-check-question-choices-v2"
TEMPLATE_VERSION = "fast-check-template-v2"
SYSTEM_MESSAGE = (
    "Reason as needed, then give the final answer in the requested format. "
    "Do not put an explanation in the final answer."
)
PROMPT_TEMPLATE = {
    "system_message": SYSTEM_MESSAGE,
    "choice": "{question}\n\n{choices}\n\nEnd with a single final-answer line in this format:\n"
              "Answer: <choice label>",
    "gsm8k": "{question}\n\nEnd with a single final-answer line in this format:\n"
             "#### <number>",
    "short": "{question}\n\nEnd with a single final-answer line in this format:\n"
             "Answer: <short answer>",
    "demonstration": "Question: {question}\nAnswer:\n{answer}",
    "few_shot": "Worked demonstrations (training examples):\n\n{demonstrations}\n\nTest question:\n{question}",
}


class ResponseError(Exception):
    """Invalid HTTP/API evidence; its body is never copied into results."""


class RetryableResponseError(ResponseError):
    """A transient HTTP status eligible for one bounded retry."""


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
    local_name = socket.gethostname().rstrip(".").casefold()
    normalized_name = value.rstrip(".").casefold()
    if (normalized_name in (local_name, local_name.split(".")[0])
            or normalized_name.endswith((".local", ".lan", ".home"))
            or "::" in value):
        return None
    if (
        re.search(r"[\\/@\x00-\x1f]|\d+\.\d+\.\d+\.\d+|[0-9a-f]{12,}", value, re.I)
        or re.search(r"serial|uuid|hostname|device.?id|://", value, re.I)
        or not re.fullmatch(r"[\w .()+,:-]+", value, re.ASCII)
    ):
        return None
    return value


def positive_int(value):
    return value if type(value) is int and value > 0 else None


def quantization_name(value):
    """Normalize only descriptive quantization labels, never guess a numeric enum."""
    value = safe_text(value)
    return value.upper().replace("-", "_") if value else None


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


NUMBER = r"[+-]?(?:(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
# Conservative screening suffixes, not arbitrary prose or unit conversion.
# Word units and multi-letter abbreviations match in any case ('72 Dollars',
# '5 usd', '12 KM'). The litre symbol accepts 'l' and 'L' ('5 L'); the
# one-letter units m and g stay case-sensitive, because their capitals collide
# with scale words such as '12 M' (million) and '12 G' (giga, grand).
UNITS = (
    r"(?:(?i:dollars?|cents?|euros?|pounds?|yen|usd|eur|gbp|percent|"
    r"hours?|minutes?|seconds?|days?|weeks?|months?|years?|"
    r"miles?|kilometers?|met(?:er|re)s?|centimet(?:er|re)s?|feet|foot|inches|inch|"
    r"kilograms?|grams?|ounces?|lit(?:er|re)s?|gallons?|kg|km|cm|mm|mg|ml|"
    r"people|persons?|items?|apples?|books?|tickets?|cookies?|marbles?|coins?)|%|m|g|l|L)"
)
UNIT_SYMBOLS = frozenset("%$£€¥")


def unwrap_formatting(text):
    """Remove balanced presentation wrappers without deleting answer content."""
    text = text.strip()
    while True:
        for wrapper in ("**", "__", "`", '"', "'", "*", "_"):
            if text.startswith(wrapper) and text.endswith(wrapper) and len(text) > 2 * len(wrapper):
                text = text[len(wrapper):-len(wrapper)].strip()
                break
        else:
            return text


def final_marker(line):
    # Markers and answers are often emphasized separately.
    line = re.sub(r"^(?:\*\*|__)((?:Final answer|Answer):)(?:\*\*|__)", r"\1", line.strip(), flags=re.I)
    line = unwrap_formatting(line)
    match = re.fullmatch(r"(####|(?:Final answer|Answer):)\s*(.+)", line, re.I)
    return unwrap_formatting(match.group(2)) if match else None


def numeric_final(text):
    """Select only a whole answer or a terminal explicit/boxed answer line."""
    if not isinstance(text, str):
        return None, None
    text = text.strip()
    lines = text.splitlines()
    if not lines:
        return None, None
    last = unwrap_formatting(lines[-1])
    marked = final_marker(lines[-1])
    if marked is not None:
        return marked, "numeric_marked_final"
    if re.fullmatch(r"(?:\$\$?|\\\(|\\\[)?\s*\\boxed\{[^{}]+\}\s*(?:\$\$?|\\\)|\\\])?\.?", last):
        return last, "numeric_boxed_final"
    return (unwrap_formatting(text), "numeric_complete") if len(lines) == 1 else (None, None)


def number_parts(text):
    token, _ = numeric_final(text)
    if token is None:
        return None, None
    token = token.strip()
    if token.endswith("."):
        token = token[:-1].rstrip()
    # Strip only balanced LaTeX math delimiters and one complete box.
    for left, right in (("$$", "$$"), ("$", "$"), (r"\(", r"\)"), (r"\[", r"\]")):
        if (
            token.startswith(left) and token.endswith(right)
            and len(token) > len(left) + len(right)
        ):
            token = token[len(left):-len(right)].strip()
            break
    boxed = re.fullmatch(r"\\boxed\{([^{}]+)\}", token)
    if boxed:
        token = boxed.group(1).strip()
        if token.endswith("."):
            token = token[:-1].rstrip()
    if token.startswith(r"\$"):
        token = token[1:]
    matched = re.fullmatch(
        r"(?P<prefix>[+-]?[$£€¥]\s*|[$£€¥]\s*[+-]?)?(?P<number>" + NUMBER
        + r")(?:\s+(?P<unit>" + UNITS + r")|\s*(?P<symbol>[%$£€¥]))?", token
    )
    if not matched:
        return None, None
    prefix = matched.group("prefix") or ""
    unit = matched.group("unit") or matched.group("symbol")
    if unit is None:
        unit = next((symbol for symbol in "$£€¥" if symbol in prefix), None)
    elif unit not in UNIT_SYMBOLS:
        # Casefold word units so the cents check also catches '12 Cents'.
        unit = unit.casefold()
    token = matched.group("number")
    if "+" in prefix or "-" in prefix:
        if token.startswith(("+", "-")):
            return None, unit
        token = ("-" if "-" in prefix else "+") + token
    token = token.replace(",", "")
    if len(token) > 128:
        return None, unit
    try:
        value = Decimal(token)
        if not value.is_finite() or abs(value.adjusted()) > 100:
            return None, unit
        # Decimal.normalize() rounds to the process precision; scoring must
        # preserve every supplied digit instead.
        result = format(value, "f")
        if "." in result:
            result = result.rstrip("0").rstrip(".")
        if value == 0:
            result = "0"
        return (result if len(result) <= 128 else None), unit
    except (InvalidOperation, ValueError):
        return None, unit


def number_answer(text):
    return number_parts(text)[0]


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


def demonstrations_for(datasets):
    result = {}
    for benchmark, source in datasets.items():
        demos = fast.demonstrations_for(benchmark, source)
        rendered = []
        for demo in demos:
            answer = number_answer(demo["answer"])
            if answer is None:
                raise fast.ValidationError("GSM8K demonstration requires a numeric final answer")
            # Keep worked reasoning supplied by the descriptor; never derive it
            # from test references or fabricate demonstrations from test rows.
            lines = demo["answer"].strip().splitlines()
            reasoning = "\n".join(lines[:-1]) if len(lines) > 1 else ""
            reasoning = "\n".join(v for v in (demo.get("rationale", ""), reasoning) if v)
            rendered.append(dict(
                question=demo["question"],
                answer=(reasoning + "\n" if reasoning else "") + "#### " + answer,
            ))
        if rendered:
            result[benchmark] = rendered
    return result


def template_manifest(demonstrations):
    return dict(template=PROMPT_TEMPLATE, demonstrations=demonstrations)


def prompt_for(row, benchmark, demonstrations=()):
    options = choices_for(row)
    if options:
        text = PROMPT_TEMPLATE["choice"].format(
            question=row["question"],
            choices="\n".join(label + ". " + value for label, value in options),
        )
    elif benchmark == "gsm8k":
        text = PROMPT_TEMPLATE["gsm8k"].format(question=row["question"])
        if demonstrations:
            block = "\n\n".join(
                PROMPT_TEMPLATE["demonstration"].format(**demo) for demo in demonstrations
            )
            text = PROMPT_TEMPLATE["few_shot"].format(demonstrations=block, question=text)
    else:
        text = PROMPT_TEMPLATE["short"].format(question=row["question"])
    return text


def parse_answer(content, row, benchmark):
    """Return (normalized answer, screening rule); ambiguity is never scored."""
    options = choices_for(row)
    text = content.strip()
    if options:
        lines = text.splitlines()
        if not lines:
            return None, None

        def resolve(candidate):
            candidate = unwrap_formatting(candidate)
            label_match = re.fullmatch(r"(?:([A-Z0-9])|\(([A-Z0-9])\))[.]?", candidate, re.I)
            label = (
                (label_match.group(1) or label_match.group(2)).upper() if label_match else None
            )
            # The requested label format has priority over text fallback. ARC
            # can contain e.g. choice A's text "C" while label C is correct.
            if label in {key for key, _ in options}:
                return label, "label"
            labeled_text = re.fullmatch(r"([A-Z0-9])[.)]\s+(.+)", candidate, re.I)
            if labeled_text:
                label = labeled_text.group(1).upper()
                value = unwrap_formatting(labeled_text.group(2))
                if any(key == label and text.casefold() in (value.casefold(), value.removesuffix(".").casefold())
                       for key, text in options):
                    return label, "label_choice_text"
                return None, None
            matches = {
                key for key, value in options if value.casefold() == candidate.casefold()
            }
            if not matches:
                matches = {key for key, value in options
                           if value.casefold() == candidate.removesuffix(".").casefold()}
            if len(matches) != 1:
                return None, None
            value = next(iter(matches))
            return value, "choice_text"

        explicit = []
        for line in lines:
            marked = final_marker(line)
            if marked is not None:
                explicit.append(resolve(marked))
        last = lines[-1].strip()
        marked = final_marker(last)
        if explicit:
            if marked is None or any(
                value is None or value != explicit[-1][0] for value, _ in explicit
            ):
                return None, None
            value, rule = explicit[-1]
            return value, "mc_explicit_" + rule
        value, rule = resolve(last)
        if value is None:
            return None, None
        return value, {"label": "mc_last_standalone_label", "label_choice_text": "mc_last_label_choice_text",
                       "choice_text": "mc_exact_choice_text"}[rule]
    if benchmark == "gsm8k":
        value, unit = number_parts(text)
        _, reference_unit = number_parts(row.get("answer"))
        sensitive = {"cent", "cents"}
        if (unit in sensitive or reference_unit in sensitive) and unit != reference_unit:
            return None, None
        return (value, numeric_final(text)[1]) if value is not None else (None, None)
    marked = re.fullmatch(r"Answer:\s*(.+)", text.splitlines()[-1], re.I) if text else None
    value = marked.group(1).strip() if marked else text
    if not value or len(value) > 128 or any(ord(c) < 32 for c in value):
        return None, None
    return value, "short_explicit" if marked else "short_exact"


def extract_answer(content, row, benchmark):
    return parse_answer(content, row, benchmark)[0]


def prompt_size_estimate(messages):
    """Conservative byte-based estimate, including fixed chat framing headroom."""
    return 16 + sum(len(message["content"].encode("utf-8")) + 8 for message in messages)


class OpenAIAdapter:
    def __init__(self, base_url, model_id, *, max_tokens=DEFAULT_MAX_TOKENS, timeout=180,
                 model_label=None, disable_thinking=False):
        self.host, self.port, self.prefix = validate_base_url(base_url)
        fast.string(model_id)
        self.model_label = model_name(model_id if model_label is None else model_label)
        self.model_id = model_id
        if type(disable_thinking) is not bool:
            raise fast.ValidationError("disable_thinking must be a boolean lane setting")
        self.disable_thinking = disable_thinking
        fast.numeric(max_tokens, MIN_MAX_TOKENS, integer=True)
        fast.numeric(timeout, low=0.001)
        self.max_tokens, self.timeout = max_tokens, timeout
        self.facts = {}
        self.server_reported_size_bytes = None
        self.available_memory_bytes = None
        self.ctx = None
        self.backend_version = None
        self.demonstrations = {}

    def request(self, method, path, payload=None, *, optional=False):
        # A direct numeric connection ignores proxy environment variables and
        # http.client never follows redirects. No other host is ever resolved.
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        deadline = time.monotonic() + self.timeout
        expired = threading.Event()
        expiry_lock = threading.Lock()
        transport = None
        timer = None
        timer_started = False

        def expire():
            # Both the timer and the request thread can notice the deadline.
            # Serialize them so observing expiry also waits for shutdown.
            with expiry_lock:
                if expired.is_set():
                    return
                expired.set()
                sock = transport if transport is not None else connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        try:
            connection.connect()
            transport = connection.sock
            if time.monotonic() >= deadline:
                raise TimeoutError("HTTP deadline")

            # A socket inactivity timeout alone permits slow-drip headers/body
            # to run forever. Shutdown this request's socket at the total
            # deadline, even after HTTPConnection relinquishes it to a response.
            timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            timer_started = True
            body = None if payload is None else fast.canonical(payload).encode("utf-8")
            connection.request(method, path, body=body, headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("HTTP deadline")
            if optional and response.status in (404, 405):
                return None
            if response.status != 200:
                error = RetryableResponseError if response.status in (408, 429, 500, 502, 503, 504) else ResponseError
                raise error("unexpected HTTP status")
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTP deadline")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if expired.is_set() or time.monotonic() >= deadline:
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
        except (socket.timeout, TimeoutError):
            # socket.timeout was not a TimeoutError alias until Python 3.10.
            # A synchronous timeout can beat the timer, including during connect.
            expire()
            raise TimeoutError("HTTP deadline") from None
        except (OSError, http.client.HTTPException):
            if expired.is_set():
                expire()
                raise TimeoutError("HTTP deadline") from None
            raise
        finally:
            if timer is not None:
                timer.cancel()
                if timer_started:
                    timer.join()
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
        model = dict(name=self.model_label, file_sha256=None, quantization=None, file_size_bytes=None)
        for item in reported:
            if not isinstance(item, dict):
                raise ResponseError("invalid model metadata")
            values = {
                "file_sha256": item.get("sha256", item.get("file_sha256")),
                "quantization": quantization_name(item.get("quantization", item.get("ftype"))),
            }
            reported_size = positive_int(item.get("file_size_bytes", item.get("size")))
            if reported_size is not None:
                if self.server_reported_size_bytes not in (None, reported_size):
                    raise ResponseError("conflicting server-reported size facts")
                self.server_reported_size_bytes = reported_size
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
        if self.ctx is None:
            self.ctx = positive_int(facts.get("n_ctx"))
        if self.ctx is not None:
            self.max_tokens = min(self.max_tokens, self.ctx)
        memory = positive_int(hardware.get("memory_bytes"))
        result = dict(
            machine_label=None, cpu=safe_text(hardware.get("cpu")),
            gpu=safe_text(hardware.get("gpu")),
            memory={"bytes": memory} if memory else None,
            memory_bytes=memory, runtime_build_id=self.backend_version,
        )
        self.available_memory_bytes = positive_int(hardware.get("available_memory_bytes"))
        self.facts = dict(metadata_sources=sources, model_id=self.model_label,
                         interface="openai-compatible",
                         server_reported_size_bytes=self.server_reported_size_bytes)
        return model, result

    def answer(self, row, *, benchmark, spec):
        expected = reference(row, benchmark)
        payload = {
            "model": self.model_id, "messages": [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": prompt_for(row, benchmark, self.demonstrations.get(benchmark, ()))},
            ], "temperature": 0, "seed": 42, "max_tokens": self.max_tokens,
            "stop": [], "stream": False,
        }
        if self.ctx is not None:
            # Conservative UTF-8 byte estimate plus chat framing; it is not a
            # tokenizer measurement. Always reserve one further context token.
            headroom = self.ctx - prompt_size_estimate(payload["messages"]) - 1
            if headroom < 1:
                raise fast.ValidationError("prompt estimate leaves no generation context")
            payload["max_tokens"] = min(self.max_tokens, headroom)
        if self.disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        attempts = 0
        try:
            for attempt in range(2):
                attempts += 1
                start = time.monotonic()
                try:
                    reply = self.request("POST", (self.prefix or "/v1") + "/chat/completions", payload)
                    seconds = max(0, time.monotonic() - start)
                    break
                except (OSError, TimeoutError, http.client.HTTPException, RetryableResponseError):
                    if attempt:
                        raise
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
            value, parse_rule = parse_answer(content, row, benchmark)
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
                cut_off=bool(cut_off), answer_value=value, parse_rule=parse_rule, error=None,
                parsed_unit=number_parts(content)[1] if benchmark == "gsm8k" else None,
                seconds=seconds, attempts=attempts, **counts,
            )
        except Exception as error:
            error.attempts = attempts
            raise


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
        wall_budget_s=None, model_label=None, machine_label=None, cpu=None,
        gpu=None, memory_bytes=None, runtime_build_id=None, disable_thinking=False):
    adapter = OpenAIAdapter(base_url, model_id, max_tokens=max_tokens, timeout=timeout,
                            model_label=model_label, disable_thinking=disable_thinking)
    datasets = {}
    for path in sample_files:
        data = fast.load(path)
        if not isinstance(data, dict) or datasets.keys() & data.keys():
            raise fast.ValidationError("sample files must have distinct benchmark descriptors")
        datasets.update(data)
    plan = fast.make_plan(datasets, count=count, wall_budget_s=wall_budget_s)
    adapter.demonstrations = demonstrations_for(datasets)
    # Validate all scorer inputs before contacting the server, including items
    # outside the selected sample (they participate in the dataset identity).
    for benchmark, source in datasets.items():
        for row in source["items"]:
            reference(row, benchmark)
    model, hardware = adapter.discover()
    server_hash = model["file_sha256"]
    if quantization is not None:
        normalized_quantization = quantization_name(quantization)
        if normalized_quantization is None:
            raise fast.ValidationError("quantization must be a short public descriptor")
        quantization = normalized_quantization
    supplied = dict(file_sha256=model_sha256, file_size_bytes=file_size_bytes, quantization=quantization)
    for key, value in supplied.items():
        if value is not None:
            if model[key] not in (None, value):
                raise fast.ValidationError("explicit model fact disagrees with server")
            model[key] = value
    supplied_hardware = dict(machine_label=machine_label, cpu=cpu, gpu=gpu,
                             memory={"bytes": memory_bytes} if memory_bytes is not None else None,
                             runtime_build_id=runtime_build_id)
    for key, value in supplied_hardware.items():
        if value is not None:
            if key == "memory":
                fast.numeric(memory_bytes, low=1, integer=True)
            elif safe_text(value) != value:
                raise fast.ValidationError("hardware facts must be short public descriptors")
            if hardware[key] not in (None, value):
                raise fast.ValidationError("explicit hardware fact disagrees with server")
            hardware[key] = value
    if memory_bytes is not None:
        hardware["memory_bytes"] = memory_bytes
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
        scorer_id=SCORER_ID, parse_version=PARSE_VERSION,
        prompt_template_id=PROMPT_ID, prompt_template_version=TEMPLATE_VERSION,
        prompt_template=template_manifest(adapter.demonstrations),
        prompt_template_sha256=fast.digest(template_manifest(adapter.demonstrations)),
        evaluation_scope="screening; not an ECI score", backend="openai-compatible",
        backend_version=adapter.backend_version or runtime_build_id, temperature=0, max_tokens=adapter.max_tokens,
        ctx=adapter.ctx, seed=42, stop=[], system_message=SYSTEM_MESSAGE, n_epochs=1,
        request_timeout_s=timeout, request_retry_limit=1,
        context_cap_policy="utf8-bytes-plus-chat-framing-v1",
        chat_template_kwargs={"enable_thinking": False} if disable_thinking else None,
    )
    provenance = dict(
        declared_file_size_bytes=file_size_bytes,
        server_reported_size_bytes=adapter.server_reported_size_bytes,
        size_match="equal" if file_size_bytes is not None and file_size_bytes == adapter.server_reported_size_bytes else "unverified",
        server_facts=adapter.facts,
        endpoint_sha256=fast.digest(dict(port=adapter.port, prefix=adapter.prefix or "/v1")),
        explicit_model_facts=sorted(k for k, v in supplied.items() if v is not None),
        explicit_hardware_facts=sorted(k for k, v in supplied_hardware.items() if v is not None),
        available_memory_source="explicit" if available_memory_bytes is not None else "server-reported",
        hardware_attestation="server-reported or caller-supplied; not independently attested",
        hash_attestation="server-reported; unverified" if server_hash is not None else "caller-supplied; unverified",
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
            runtime_settings=settings, pre=pre, checkpoint=output / "checkpoint.jsonl", provenance=provenance,
        )
        atomic_json(output / "result.json", record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-label", help="public record label when the advertised model id is a private path")
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
    parser.add_argument("--machine-label", help="public hardware profile label, never a device name")
    parser.add_argument("--cpu")
    parser.add_argument("--gpu")
    parser.add_argument("--memory-bytes", type=int)
    parser.add_argument("--runtime-build-id")
    parser.add_argument("--disable-thinking", action="store_true",
                        help="per-lane setting: send chat_template_kwargs enable_thinking=false")
    args = parser.parse_args(argv)
    try:
        fast.numeric(args.max_tokens, MIN_MAX_TOKENS, integer=True)
        record = run(
            args.base_url, args.model, args.samples, args.output_dir, count=args.count,
            max_tokens=args.max_tokens, timeout=args.timeout, wall_budget_s=args.wall_budget_s,
            model_sha256=args.model_sha256, file_size_bytes=args.file_size_bytes,
            quantization=args.quantization, available_memory_bytes=args.available_memory_bytes,
            model_label=args.model_label, machine_label=args.machine_label, cpu=args.cpu, gpu=args.gpu,
            memory_bytes=args.memory_bytes, runtime_build_id=args.runtime_build_id,
            disable_thinking=args.disable_thinking,
        )
        print("Fast-check result written; full ECI NOT_RUN. Post: " + record["post"]["decision"])
        incomplete = [b for b in record["benchmarks"] if b["attempted"] < b["planned"]]
        if incomplete:
            print("Incomplete: " + "; ".join(b["benchmark"] + ": " + b["stop_reason"] for b in incomplete))
            print("Durable checkpoint retained. Repeat the same command to resume; trailing errors will be retried.")
            return 3
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
