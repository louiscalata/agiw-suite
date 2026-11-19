import io
import json
import os
import socket
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib import error as urlerror

from agiw import free_pool_call as fpc

SHIPPED = Path(fpc.__file__).with_name("free_pool_routing.json")
FAKE_KEY = "fixture-only-key-value"
SMALL = "Fix the bug: def add(a, b): return a - b"


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self, _limit=None):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def api_payload(model="gpt-5.6-terra", payer="openai", text="def add(a, b): return a + b", total=120):
    return {
        "id": "resp_fixture_1",
        "model": model,
        "billing": {"payer": payer},
        "usage": {"input_tokens": total - 20, "output_tokens": 20, "total_tokens": total},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


class RoutingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.machine_path = self.root / "free_pool_machine.json"
        # Tests use the shipped config minus any dated fleet-wide day blocks, so they do not depend on today's date.
        shipped = json.loads(SHIPPED.read_text(encoding="utf-8"))
        for group in shipped["groups"].values():
            group.pop("blocked_utc_days", None)
        self.routing = self.root / "shipped-routing.json"
        self.routing.write_text(json.dumps(shipped), encoding="utf-8")
        self.write_machine("m5")
        self.cfg = fpc.load_routing(self.routing)
        self.calls = []
        env = {"OPENAI_FREE_POOL_API_KEY": FAKE_KEY}
        self.env = patch.dict(os.environ, env, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text(SMALL, encoding="utf-8")

    def write_machine(self, machine_id, **extra):
        data = {
            "machine_id": machine_id,
            "receipts_dir": str(self.root / "receipts"),
            "disabled_file": str(self.root / "free-pool-disabled.json"),
            **extra,
        }
        self.machine_path.write_text(json.dumps(data), encoding="utf-8")

    def machine(self):
        return fpc.load_machine(self.cfg, self.machine_path)

    def opener(self, *responses):
        queue = list(responses)

        def fake(req, timeout=None):
            body = json.loads(req.data)
            self.calls.append(body)
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return FakeResponse(item)

        return patch.object(fpc, "_urlopen", side_effect=fake)

    def run_cli(self, *args, routing=None):
        argv = [*args, "--machine-config", str(self.machine_path), "--routing", str(routing or self.routing)]
        out = io.StringIO()
        with redirect_stdout(out):
            code = fpc.main(argv)
        text = out.getvalue()
        self.assertNotIn(FAKE_KEY, text)
        lines = [line for line in text.splitlines() if line.strip()]
        return code, (json.loads(lines[-1]) if lines and lines[-1].startswith("{") else text)

    def real(self, *extra):
        return (str(self.prompt), "--project", "agiw-suite", "--source", "code", "--lane", "unit-test", *extra)

    def ledger_rows(self):
        path = self.root / "receipts" / fpc.LEDGER_NAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def seed(self, **row):
        path = self.root / "receipts" / fpc.LEDGER_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        row.setdefault("utc_day", fpc.utc_day())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")


class ConfigTests(RoutingBase):
    def test_every_listed_model_has_group_and_active_ones_have_caps_and_strengths(self):
        groups = self.cfg["groups"]
        self.assertEqual(groups["official_1M"]["daily_cap"], 1_000_000)
        self.assertEqual(groups["official_10M"]["daily_cap"], 10_000_000)
        self.assertFalse(groups["empirical_6.2"]["official"])
        for key, entry in self.cfg["models"].items():
            self.assertIn(entry["group"], groups, key)
            if entry.get("doc_status") == "active":
                self.assertIn("max_total_tokens", entry, key)
                self.assertIn("max_output_tokens", entry, key)
                self.assertTrue(entry["strengths"], key)
        self.assertFalse(self.cfg["models"]["gpt-6.2-sol"]["enabled"])
        self.assertTrue(self.cfg["models"]["gpt-6.2-astra"]["requires_hard"])

    def test_fleet_split_sums_to_one(self):
        split = self.cfg["fleet"]["split"]
        self.assertEqual(split, {"m5": 0.6, "pc": 0.25, "macpro": 0.15})
        self.assertAlmostEqual(sum(split.values()), 1.0)

    def test_routing_order_per_kind_and_mini_off_code_routes(self):
        routes = self.cfg["routes"]
        self.assertEqual(
            set(routes), {"small_patch", "large_change", "review", "docs_text", "extraction", "long_context"}
        )
        for kind in self.cfg["code_kinds"]:
            self.assertNotIn("gpt-5.4-mini", routes[kind])
        self.assertEqual(routes["docs_text"][0], "gpt-5.6-terra")
        code, out = self.run_cli(str(self.prompt), "--kind", "docs_text", "--dry-run")
        self.assertEqual((code, out["model"]), (0, "gpt-5.6-terra"))
        code, out = self.run_cli(str(self.prompt), "--kind", "extraction", "--dry-run")
        self.assertEqual((code, out["model"]), (0, "gpt-5.6-luna"))

    def test_code_kinds_need_a_code_trusted_model(self):
        code, out = self.run_cli(str(self.prompt), "--kind", "small_patch", "--dry-run")
        self.assertEqual((code, out["model"]), (0, "gpt-6.1-sol"))
        cfg = json.loads(self.routing.read_text(encoding="utf-8"))
        for key in cfg["models"]:
            cfg["models"][key]["code_trusted"] = key == "gpt-5.4-mini"
        routing = self.root / "routing.json"
        routing.write_text(json.dumps(cfg), encoding="utf-8")
        code, out = self.run_cli(str(self.prompt), "--kind", "small_patch", "--dry-run", routing=routing)
        self.assertEqual(code, fpc.EXIT_BUDGET)
        self.assertTrue(all(r.get("skipped") for r in out["route"]))
        cfg["models"]["gpt-6-sol"]["code_trusted"] = True
        routing.write_text(json.dumps(cfg), encoding="utf-8")
        code, out = self.run_cli(str(self.prompt), "--kind", "small_patch", "--dry-run", routing=routing)
        self.assertEqual((code, out["model"]), (0, "gpt-6-sol"))

    def test_hard_blocks_deprecated_and_snapshot_mapping(self):
        for model in ("gpt-5.5-pro", "o3-pro", "gpt-5.3-codex", "gpt-5.3-codex-2026-01-01", "gpt-4.5-preview"):
            with self.assertRaises(fpc.Refused) as caught:
                fpc.resolve_model(self.cfg, model)
            self.assertEqual(caught.exception.exit_code, fpc.EXIT_MODEL)
        self.assertEqual(fpc.resolve_model(self.cfg, "gpt-5.4-mini-2026-03-17"), "gpt-5.4-mini")
        self.assertEqual(fpc.classify_group(self.cfg, "gpt-5.5-2026-04-23"), "official_1M")
        self.assertEqual(fpc.classify_group(self.cfg, "gpt-4o-2024-05-13"), "official_1M")
        with self.assertRaises(fpc.Refused):
            fpc.resolve_model(self.cfg, "gpt-5.5-2025-01-01")
        code, out = self.run_cli(str(self.prompt), "--model", "gpt-5-nano", "--dry-run")
        self.assertEqual(code, fpc.EXIT_BUDGET)
        self.assertIn("deprecated", out["route"][0]["skipped"])


class GuardTests(RoutingBase):
    def test_near_cap_fails_over_within_the_same_kind(self):
        allowed = int(9_000_000 * 0.6 * 0.95)
        self.seed(model="gpt-5.4-mini", group="official_10M", total_tokens=allowed - 100, response_id="r1")
        code, out = self.run_cli(str(self.prompt), "--kind", "docs_text", "--dry-run")
        self.assertEqual((code, out["model"]), (0, "gpt-5.6-sol"))
        self.assertIn("near cap", out["route"][0]["skipped"])

    def test_share_split_changes_allowance(self):
        self.write_machine("macpro")
        report = fpc._budget_report(self.cfg, self.machine())
        self.assertEqual(report["groups"]["official_1M"]["allowed"], int(800_000 * 0.15 * 0.95))

    def test_request_size_caps_reroute_to_a_model_with_evidence(self):
        self.prompt.write_text("x = 1\n" * 1200, encoding="utf-8")
        code, out = self.run_cli(str(self.prompt), "--kind", "docs_text", "--dry-run")
        self.assertEqual((code, out["model"]), (0, "gpt-5.4-mini"))
        self.assertIn("free request cap", out["route"][0]["skipped"])

    def test_run_cost_cap(self):
        cfg = json.loads(self.routing.read_text(encoding="utf-8"))
        cfg["models"]["gpt-5.6-terra"]["price_usd_per_1m"] = [2000.0, 2000.0]
        routing = self.root / "routing.json"
        routing.write_text(json.dumps(cfg), encoding="utf-8")
        code, out = self.run_cli(str(self.prompt), "--kind", "docs_text", "--dry-run", routing=routing)
        self.assertEqual((code, out["model"]), (0, "gpt-5.6-sol"))
        self.assertIn("run cap", out["route"][0]["skipped"])

    def test_exhausted_credit_fails_closed(self):
        body = io.BytesIO(json.dumps({"error": {"code": "insufficient_quota"}}).encode())
        err = urlerror.HTTPError(fpc.ENDPOINT, 429, "quota", {}, body)
        with self.opener(err):
            code, out = self.run_cli(*self.real("--kind", "docs_text"))
        self.assertEqual((code, out["refused"]), (fpc.EXIT_CREDIT, "CREDIT_EXHAUSTED"))
        with self.opener():
            code, out = self.run_cli(*self.real("--kind", "docs_text"))
        self.assertEqual(code, fpc.EXIT_CREDIT)
        self.assertEqual(len(self.calls), 1)

    def test_payer_developer_stops_disables_for_day_and_next_call_reroutes_in_kind(self):
        with self.opener(api_payload(payer="developer")):
            code, out = self.run_cli(*self.real("--kind", "docs_text"))
        self.assertEqual((code, out["status"]), (fpc.EXIT_PAYER, "payer_refused"))
        disabled = json.loads((self.root / "free-pool-disabled.json").read_text(encoding="utf-8"))
        self.assertEqual(disabled["gpt-5.6-terra"]["utc_day"], fpc.utc_day())
        credit = (self.root / "receipts" / fpc.CREDIT_LEDGER_NAME).read_text(encoding="utf-8")
        self.assertIn('"payer": "developer"', credit)
        code, out = self.run_cli(str(self.prompt), "--kind", "docs_text", "--dry-run")
        self.assertEqual((code, out["model"]), (0, "gpt-5.6-sol"))
        self.assertNotEqual(out["model"], "gpt-5.4-mini")

    def test_missing_payer_is_not_treated_as_free(self):
        payload = api_payload()
        payload.pop("billing")
        with self.opener(payload):
            code, _out = self.run_cli(*self.real("--kind", "docs_text"))
        self.assertEqual(code, fpc.EXIT_PAYER)

    def test_timeout_is_not_retried_and_counts_as_possibly_billed(self):
        with self.opener(socket.timeout("slow"), api_payload()):
            code, out = self.run_cli(*self.real("--kind", "docs_text"))
        self.assertEqual((code, out["refused"]), (fpc.EXIT_TIMEOUT, "CLIENT_TIMEOUT"))
        self.assertEqual(len(self.calls), 1)
        final = [r for r in self.ledger_rows() if r["kind"] == "usage"]
        self.assertTrue(final[-1]["possibly_billed"])
        self.assertGreater(final[-1]["total_tokens"], 0)

    def test_receipts_carry_source_lane_and_listed_snapshot(self):
        with self.opener(api_payload(model="gpt-5.6-terra")):
            code, out = self.run_cli(*self.real("--kind", "docs_text"))
        self.assertEqual(code, 0)
        self.assertEqual((out["receipt"]["source"], out["receipt"]["lane"]), ("code", "unit-test"))
        self.assertEqual(out["receipt"]["task_kind"], "docs_text")
        for row in self.ledger_rows():
            self.assertEqual((row["source"], row["lane"]), ("code", "unit-test"))
        with self.opener(api_payload(model="gpt-5.4-mini-2026-03-17")):
            self.run_cli(*self.real("--model", "gpt-5.4-mini"))
        self.assertEqual(self.calls[-1]["model"], "gpt-5.4-mini")
        self.assertFalse(self.calls[-1]["store"])
        self.assertNotIn("tools", self.calls[-1])

    def test_served_snapshot_must_be_the_listed_one(self):
        with self.opener(api_payload(model="gpt-5.4-mini-2027-01-01")):
            code, out = self.run_cli(*self.real("--model", "gpt-5.4-mini"))
        self.assertEqual(code, fpc.EXIT_PAYER)
        self.assertFalse(out["receipt"]["served_model_ok"])
        self.assertIn("gpt-5.4-mini", json.loads((self.root / "free-pool-disabled.json").read_text(encoding="utf-8")))

    def test_repo_flag_selects_a_project_and_unknown_repos_are_refused(self):
        with self.opener(api_payload()):
            code, out = self.run_cli(
                str(self.prompt),
                "--repo",
                "louiscalata/nisi",
                "--source",
                "claude-code",
                "--lane",
                "claude-free-pool",
                "--kind",
                "docs_text",
            )
        self.assertEqual((code, out["receipt"]["project"], out["receipt"]["lane"]), (0, "nisi", "claude-free-pool"))
        code, out = self.run_cli(
            str(self.prompt),
            "--repo",
            "someone/other",
            "--source",
            "claude-code",
            "--lane",
            "claude-free-pool",
            "--kind",
            "docs_text",
        )
        self.assertEqual((code, out["refused"]), (fpc.EXIT_GATE, "PROJECT_REFUSED"))

    def test_real_calls_need_source_and_lane(self):
        with self.opener():
            code, out = self.run_cli(str(self.prompt), "--project", "nisi", "--kind", "docs_text", "--source", "code")
        self.assertEqual((code, out["refused"]), (fpc.EXIT_USAGE, "TAGS_REQUIRED"))
        self.assertEqual(self.calls, [])

    def test_reservation_counts_until_finished(self):
        self.seed(kind="reservation", reservation_id="abc", model="gpt-6-luna", group="official_1M", est_tokens=500)
        used, _ = fpc.usage_today(self.cfg, self.machine())
        self.assertEqual(used["official_1M"], 500)
        self.seed(kind="usage", reservation_id="abc", model="gpt-6-luna", total_tokens=120, response_id="r9")
        used, _ = fpc.usage_today(self.cfg, self.machine())
        self.assertEqual(used["official_1M"], 120)

    def test_legacy_m5_ledger_rows_count(self):
        self.seed(model="gpt-6.2-sol", group="unlisted_6.2", tokens=900, response_id="old1", payer="openai")
        self.seed(model="gpt-6.2-sol", tokens=950, response_id="old1")
        other = self.root / "legacy.jsonl"
        other.write_text(json.dumps({"bucket": "gpt-6.2-astra", "total_tokens": 40, "entry_id": "e1"}) + "\n")
        self.write_machine("m5", extra_ledgers=[str(other)])
        with patch.object(fpc, "_row_day", side_effect=lambda row: fpc.utc_day()):
            used, _ = fpc.usage_today(self.cfg, self.machine())
        self.assertEqual(used["empirical_6.2"], 990)


class PoolTests(RoutingBase):
    def test_empirical_usage_counts_toward_the_1m_pool(self):
        self.seed(model="gpt-6.2-astra", group="empirical_6.2", total_tokens=700, response_id="a1", payer="openai")
        used, _ = fpc.usage_today(self.cfg, self.machine())
        self.assertEqual((used["empirical_6.2"], used["official_1M"]), (700, 700))

    def test_billed_1m_row_reroutes_the_kind_to_the_10m_group(self):
        self.seed(model="gpt-5.5", group="official_1M", total_tokens=136, response_id="b5", payer="developer")
        code, out = self.run_cli(str(self.prompt), "--kind", "review", "--dry-run")
        self.assertEqual((code, out["model"]), (0, "gpt-5.6-terra"))
        code, out = self.run_cli(str(self.prompt), "--model", "gpt-6.2-astra", "--hard", "--no-reroute", "--dry-run")
        self.assertEqual(code, fpc.EXIT_BUDGET)

    def test_shipped_day_block_applies_fleet_wide(self):
        cfg = json.loads(self.routing.read_text(encoding="utf-8"))
        cfg["groups"]["official_1M"]["blocked_utc_days"] = [fpc.utc_day()]
        routing = self.root / "blocked.json"
        routing.write_text(json.dumps(cfg), encoding="utf-8")
        code, out = self.run_cli(str(self.prompt), "--kind", "small_patch", "--dry-run", routing=routing)
        self.assertEqual((code, out["model"]), (0, "gpt-5.6-terra"))
        self.assertIn("blocked for", out["route"][0]["skipped"])


class EmpiricalTests(RoutingBase):
    def test_sol_is_off_and_astra_needs_hard(self):
        code, out = self.run_cli(str(self.prompt), "--model", "gpt-6.2-sol", "--no-reroute", "--dry-run")
        self.assertEqual(code, fpc.EXIT_BUDGET)
        code, out = self.run_cli(str(self.prompt), "--model", "gpt-6.2-astra", "--no-reroute", "--dry-run")
        self.assertEqual(code, fpc.EXIT_BUDGET)
        self.assertEqual(out["route"][0]["skipped"], "needs --hard")
        code, out = self.run_cli(str(self.prompt), "--model", "gpt-6.2-astra", "--hard", "--dry-run")
        self.assertEqual((code, out["model"]), (0, "gpt-6.2-astra"))

    def test_hard_does_not_bypass_size_caps(self):
        code, out = self.run_cli(
            str(self.prompt), "--model", "gpt-6.2-astra", "--hard", "--max-output", "1001", "--no-reroute", "--dry-run"
        )
        self.assertEqual(code, fpc.EXIT_BUDGET)
        self.assertIn("--max-output 1001", out["route"][0]["skipped"])
        self.prompt.write_text("y" * 6000, encoding="utf-8")
        code, out = self.run_cli(str(self.prompt), "--model", "gpt-6.2-astra", "--hard", "--no-reroute", "--dry-run")
        self.assertEqual(code, fpc.EXIT_BUDGET)
        self.assertIn("free request cap 2900", out["route"][0]["skipped"])

    def test_agiw_helper_sized_astra_session_is_refused(self):
        code, out = self.run_cli(
            "--precheck-only", "--model", "gpt-6.2-astra", "--hard", "--est-tokens", "25000", "--max-output", "16000"
        )
        self.assertEqual(code, fpc.EXIT_BUDGET)
        with self.assertRaises(SystemExit) as caught, redirect_stdout(io.StringIO()):
            fpc.check_model("gpt-6.2-astra", {})
        self.assertEqual(caught.exception.code, fpc.EXIT_MODEL)

    def test_billed_row_today_blocks_the_empirical_group(self):
        self.seed(model="gpt-6.2-sol", group="unlisted_6.2", tokens=3145, response_id="b1", payer="developer")
        code, out = self.run_cli(str(self.prompt), "--model", "gpt-6.2-astra", "--hard", "--no-reroute", "--dry-run")
        self.assertEqual(code, fpc.EXIT_BUDGET)
        self.assertIn("developer-billed", out["route"][0]["skipped"])


class GateTests(RoutingBase):
    def assert_gate(self, *args, text=None):
        if text is not None:
            self.prompt.write_text(text, encoding="utf-8")
        with self.opener():
            code, out = self.run_cli(*args)
        self.assertEqual(code, fpc.EXIT_GATE, out)
        self.assertEqual(self.calls, [])

    def test_refusals_never_reach_the_api(self):
        self.assert_gate(*self.real("--kind", "review"), text="see https://www." + "notion" + ".so/page-123")
        self.assert_gate(*self.real("--kind", "review"), text="key = sk-proj-abcdefghijklmnopqrstuvwxyz")
        self.assert_gate(*self.real("--kind", "review"), text="\n".join(["2026-11-19 10:00:00 INFO ok"] * 6))
        self.assert_gate(*self.real("--kind", "review", "--security"), text=SMALL)
        self.assert_gate(
            str(self.prompt), "--project", "nisi", "--source", "linear", "--lane", "t", "--kind", "review", text=SMALL
        )
        env_file = self.root / ".env"
        env_file.write_text("A=1", encoding="utf-8")
        self.assert_gate(str(env_file), "--project", "nisi", "--source", "code", "--lane", "t", "--kind", "review")

    def test_key_loader_does_not_recurse_and_reads_env_file(self):
        env_path = self.root / "fp.env"
        env_path.write_text('export OPENAI_API_KEY="from-file-value"\n', encoding="utf-8")
        with patch.dict(os.environ, {"CODEX_API_FREE_POOL_ENV": str(env_path)}, clear=True):
            self.assertEqual(fpc.load_api_key({}), "from-file-value")


if __name__ == "__main__":
    unittest.main()
