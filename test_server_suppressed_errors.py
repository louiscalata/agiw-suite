import contextlib
import io
import unittest
from unittest import mock

import server


class TestServerSuppressedErrors(unittest.TestCase):
    def setUp(self):
        server._SUPPRESSED_SEEN.clear()

    def test_memory_guard_logs_once(self):
        stderr = io.StringIO()
        with mock.patch.object(server, "Guard", side_effect=RuntimeError("boom")):
            with contextlib.redirect_stderr(stderr):
                self.assertIsNone(server.memory_guard())
                first = stderr.getvalue().strip().splitlines()
                self.assertEqual(first, ['{"suppressed": "memory_guard", "reason": "RuntimeError"}'])
                self.assertIsNone(server.memory_guard())
                second = stderr.getvalue().strip().splitlines()
                self.assertEqual(second, ['{"suppressed": "memory_guard", "reason": "RuntimeError"}'])

    def test_stop_peer_link_logs(self):
        class Link:
            def stop(self):
                raise OSError("nope")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertIsNone(server.stop_peer_link(Link()))
        self.assertEqual(
            stderr.getvalue().strip().splitlines(), ['{"suppressed": "peer_link.stop", "reason": "OSError"}']
        )


if __name__ == "__main__":
    unittest.main()
