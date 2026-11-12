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



def windows_root_resources(text):
    """Read the explicit root copy inventory, not filenames quoted elsewhere.

    The installer uses a literal foreach list feeding the root Join-Path pair.
    This is a source contract check, not a claim to execute PowerShell.
    """
    code = re.sub(r'(?ms)<#.*?#>', '', text)
    code = re.sub(r'(?m)^\s*#.*$', '', code)
    pattern = (r"foreach\s*\(\$file\s+in\s+((?:'(?:[^']|'')*'\s*,?\s*)+)\)\s*\{"
               r"\s*\$files\.Add\(@\(\(Join-Path\s+\$repo\s+\$file\),\s*\$file\)\)\s*\}")
    matches = re.findall(pattern, code)
    if len(matches) != 1:
        raise AssertionError('expected one literal root resource copy loop')
    return [name.replace("''", "'") for name in re.findall(r"'((?:[^']|'')*)'", matches[0])]


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
    def test_eci_runtime_data_is_packaged(self):
        for script in ('build.sh', 'package-release.sh'):
            for name in ('eci_tuning_catalog.json', 'eci_tuning_routes.json', 'eci_tuning.schema.json',
                         'eci_fast_check.py', 'eci_fast_check.schema.json'):
                self.assertIn(name, resources(script))

    def test_windows_installer_retains_fast_check_dependency_and_schema(self):
        text = (REPO / 'windows/install.ps1').read_text()
        for name in ('eci_fast_check.py', 'eci_fast_check.schema.json'):
            self.assertIn(name, windows_root_resources(text))

    def test_windows_resource_contract_ignores_names_only_in_comments(self):
        text = (REPO / 'windows/install.ps1').read_text()
        for name in ('eci_fast_check.py', 'eci_fast_check.schema.json'):
            text = text.replace("'" + name + "', ", '')
            text += "\n# '" + name + "' is only a comment\n"
        names = windows_root_resources(text)
        self.assertNotIn('eci_fast_check.py', names)
        self.assertNotIn('eci_fast_check.schema.json', names)
        self.assertIn('eci_tuning.py', names)

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
