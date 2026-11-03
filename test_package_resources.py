"""The packagers copy every module the observer imports and every page it serves; reads source text only."""
import ast
from pathlib import Path
import re
import shlex
import unittest

REPO = Path(__file__).resolve().parent


def resources(script):
    text = (REPO / script).read_text()
    return shlex.split(re.search(r'resources=\((.*?)\)', text, re.DOTALL).group(1))


def local_imports(module):
    """Repository modules reachable from ``module`` through import statements."""
    seen, pending = set(), [module]
    while pending:
        name = pending.pop()
        if name in seen or not (REPO / f'{name}.py').is_file():
            continue
        seen.add(name)
        for node in ast.walk(ast.parse((REPO / f'{name}.py').read_text())):
            if isinstance(node, ast.Import):
                pending.extend(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                pending.append(node.module.split('.')[0])
    return seen


def served_assets():
    for node in ast.parse((REPO / 'server.py').read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'ASSETS' for t in node.targets):
            return [f'web/{name}' for name, _ in ast.literal_eval(node.value).values()]
    raise AssertionError('server.py has no ASSETS table')


class PackageResourceTests(unittest.TestCase):
    def test_both_packagers_copy_the_same_resources(self):
        self.assertEqual(sorted(resources('build.sh')), sorted(resources('package-release.sh')))

    def test_every_observer_import_is_packaged(self):
        modules = local_imports('server')
        self.assertIn('inference_collect', modules)
        for script in ('build.sh', 'package-release.sh'):
            missing = sorted(f'{name}.py' for name in modules if f'{name}.py' not in resources(script))
            self.assertEqual(missing, [], script)

    def test_every_served_page_is_packaged(self):
        for script in ('build.sh', 'package-release.sh'):
            missing = sorted(set(served_assets()) - set(resources(script)))
            self.assertEqual(missing, [], script)


if __name__ == '__main__':
    unittest.main()
