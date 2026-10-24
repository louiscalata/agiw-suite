"""Two in-process peer links on 127.0.0.1, paired and read through the agiw CLI wrapper.

The peer links are the real, unchanged agiw-peer/2 code (SPAKE2 PIN pairing, signed status); only discovery
is injected as hello datagrams, ports are ephemeral, and nothing leaves loopback."""
import json
import re
import time
import unittest
from unittest import mock

from support import isolate_windows_registry, pl, status_doc, temp_dir

from agiw import cli


class LoopbackPairing(unittest.TestCase):
    def setUp(self):
        isolate_windows_registry(self)
        poller = mock.patch.object(pl.PeerLink, "_poll", lambda link: None)  # the test drives every poll
        poller.start()
        self.addCleanup(poller.stop)
        self.alpha = self.node("mac", "alpha")
        self.beta = self.node("linux", "beta")

    def node(self, host, name):
        state = temp_dir(self, f"agiw-{name}-") / "peer-link"
        link = pl.PeerLink(state, host, name, lambda: status_doc(host, name), status_port=0, beacon_port=0,
                           bind="127.0.0.1", broadcast=(), allow_loopback=True).start()
        self.addCleanup(link.stop)
        return link

    def hello(self, sender, receiver):
        receiver.handle_datagram(pl.make_hello(sender.node, sender.host, sender.name, sender.status_port,
                                               time.time()), "127.0.0.1")

    def agiw(self, link, *argv, pin=None):
        lines = []
        code = cli.main(["--state-dir", str(link.state_dir), "--status-port", str(link.status_port), *argv],
                        out=lines.append, read_pin=lambda prompt: pin)
        return code, lines

    def test_pair_exchange_one_signed_status_and_unpair(self):
        self.hello(self.alpha, self.beta)
        self.hello(self.beta, self.alpha)

        code, lines = self.agiw(self.alpha, "peers")
        self.assertEqual(code, 0)
        self.assertTrue(any("beta" in line and "linux" in line and " no" in line for line in lines), lines)

        code, lines = self.agiw(self.alpha, "pair", "beta")
        self.assertEqual(code, 0, lines)
        pin = re.fullmatch(r"PIN (\d{6})", lines[0]).group(1)

        with mock.patch("sys.stderr"):
            code, _ = self.agiw(self.beta, "accept", "alpha", pin=f"{(int(pin) + 1) % 1000000:06d}")
        self.assertEqual(code, 1)  # a wrong PIN pins nothing
        self.assertEqual(self.beta.trusted(), {})

        code, lines = self.agiw(self.beta, "accept", "alpha", pin=pin)
        self.assertEqual((code, lines), (0, ["paired with alpha"]))
        self.assertIn(self.beta.node, self.alpha.trusted())
        self.assertIn(self.alpha.node, self.beta.trusted())

        self.alpha.poll_once()  # one signed GET /agiw-peer/v2/status from alpha to beta
        code, lines = self.agiw(self.alpha, "status", "--json")
        self.assertEqual(code, 0)
        doc = json.loads("\n".join(lines))
        self.assertEqual((doc["self"]["name"], doc["signed"], doc["encrypted"]), ("alpha", True, False))
        (machine,) = doc["machines"]
        self.assertEqual((machine["name"], machine["host"], machine["state"], machine["health"],
                          machine["modelsLoaded"]), ("beta", "linux", "up", "ok", 1))

        code, lines = self.agiw(self.alpha, "status")
        self.assertTrue(any(re.match(r"beta\s+linux\s+up\s+ok, 1 model", line) for line in lines), lines)

        code, lines = self.agiw(self.alpha, "unpair", "beta")
        self.assertEqual(code, 0)
        self.beta.poll_once()  # beta still trusts alpha; alpha now refuses its signed request
        snapshot = pl.control_request(self.beta.state_dir, self.beta.status_port, "GET", "/control/peers")[1]
        row = next(r for r in snapshot["peers"] if r["name"] == "alpha")
        self.assertIn("403", row["error"])
        code, lines = self.agiw(self.alpha, "status", "--json")
        self.assertEqual(json.loads("\n".join(lines))["machines"], [])


if __name__ == "__main__":
    unittest.main()
