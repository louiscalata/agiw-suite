"""Source-only distribution parity: never run build, install, or release scripts."""
import ast
from http.client import HTTPConnection
import threading
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent
LICENSE = 'fonts/OFL-IBM-Plex.txt'


def assets(path):
    tree = ast.parse((ROOT / path).read_text())
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'ASSETS' for target in node.targets))
    return ast.literal_eval(assignment.value)


def manifest(path):
    source = (ROOT / path).read_text()
    if path.endswith('.ps1'):
        block = next(block for block in re.findall(r"foreach \(\$file in (.*?)\) \{", source, re.S) if "'index.html'" in block)
        return re.findall(r"'([^']+)'", block)
    block = re.search(r'resources=\((.*?)\n\)', source, re.S)[1]
    return [item.removeprefix('web/') for item in block.split()]


class FontResourceParity(unittest.TestCase):
    def test_each_distribution_ships_its_served_fonts_and_license(self):
        css_fonts = set(re.findall(r'url\("/(fonts/[^"\n]+\.woff2)"\)', (ROOT / 'web/jaryo.css').read_text()))
        self.assertEqual(len(css_fonts), 4)
        for script, server in [('build.sh', 'server.py'), ('package-release.sh', 'server.py'),
                               ('windows/install.ps1', 'windows/agiw_win/server.py')]:
            with self.subTest(distribution=script):
                shipped = manifest(script)
                served = assets(server)
                served_fonts = {name for route, (name, mime) in served.items() if route.startswith('/fonts/')}
                self.assertEqual(served_fonts, css_fonts)
                self.assertEqual({name for name in shipped if name.endswith('.woff2')}, served_fonts)
                for name in served_fonts:
                    self.assertEqual(shipped.count(name), 1, name)
                    self.assertEqual(served['/' + name], (name, 'font/woff2'))
                    self.assertEqual((ROOT / 'web' / name).read_bytes()[:4], b'wOF2')
                self.assertEqual(shipped.count(LICENSE), 1)
                self.assertIn('SIL OPEN FONT LICENSE Version 1.1', (ROOT / 'web' / LICENSE).read_text())
                # Adding typography must not drop the current ECI page resources.
                for name in ('eci-tuning.html', 'eci-tuning.mjs', 'eci-tuning.css'):
                    self.assertIn(name, shipped)
                    self.assertIn(name, {value[0] for value in served.values()})


    def test_mac_serves_font_bytes_with_same_origin_csp(self):
        # Empty snapshot and explicit fixture capability; no collector or model runs.
        from server import MonitorServer, SnapshotStore
        server = MonitorServer(('127.0.0.1', 0), SnapshotStore(), control_capability='font-fixture')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for route, (name, mime) in assets('server.py').items():
                if not route.startswith('/fonts/'):
                    continue
                with self.subTest(route=route):
                    connection = HTTPConnection('127.0.0.1', server.server_address[1], timeout=2)
                    try:
                        connection.request('GET', route)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), (ROOT / 'web' / name).read_bytes())
                        self.assertEqual(response.getheader('Content-Type'), mime)
                        self.assertIn("font-src 'self'", response.getheader('Content-Security-Policy'))
                    finally:
                        connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)



if __name__ == '__main__':
    unittest.main()
