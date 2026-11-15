"""Keep Windows workflow triggers aligned with shipped shared dependencies."""
import ast
import fnmatch
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent


class WindowsWorkflowCoverageTests(unittest.TestCase):
    def test_windows_workflow_covers_shipped_shared_runtime_files(self):
        installer = (ROOT / "windows/install.ps1").read_text()
        shared = re.search(r"foreach \(\$file in ('usage-format\.mjs'.*?)\) \{", installer).group(1)
        paths = re.findall(r"'([^']+)'", shared)
        runtime = [p for p in paths if p.endswith((".py", ".json", ".mjs"))]
        runtime.append("online_code_repair.py")
        workflow = (ROOT / ".github/workflows/windows-edition.yml").read_text()
        filters = re.findall(r"paths: \[(.*?)\]", workflow, re.S)
        self.assertEqual(len(filters), 2)
        for event, block in zip(("push", "pull_request"), filters):
            patterns = re.findall(r"'([^']+)'", block)
            for path in runtime:
                with self.subTest(event=event, path=path):
                    self.assertTrue(any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns),
                                    f"{event} misses shipped dependency {path}")

    def test_windows_workflow_covers_root_files_the_windows_tests_read(self):
        # Root test modules (and the root modules they import) and test_support fixtures that windows/tests use.
        def root_module(name):
            top = name.split(".")[0]
            shadows = (ROOT / "windows" / f"{top}.py", ROOT / "windows" / top,
                       ROOT / "windows/tests" / f"{top}.py", ROOT / "windows/agiw_win" / f"{top}.py")
            path = ROOT / f"{top}.py"
            return path if path.is_file() and not any(shadow.exists() for shadow in shadows) else None

        def parts(node):
            # 'a/b' and ROOT / 'a' / 'b' both name a root-relative path.
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return [node.value]
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                left, right = parts(node.left), parts(node.right)
                return left + right if right else []
            return []

        needed, pending = set(), sorted((ROOT / "windows/tests").glob("*.py"))
        while pending:
            source = pending.pop()
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                for name in names:
                    module = root_module(name)
                    if module is not None and module.name not in needed:
                        needed.add(module.name)
                        pending.append(module)
                if source.parent.name == "tests" and isinstance(node, ast.BinOp):
                    path = "/".join(parts(node))
                    if path.startswith("test_support/") and (ROOT / path).is_file():
                        needed.add(path)
                elif source.parent.name == "tests" and isinstance(node, ast.Constant) and \
                        isinstance(node.value, str) and node.value.startswith("test_support/") and \
                        (ROOT / node.value).is_file():
                    needed.add(node.value)
        self.assertIn("test_eci_fast_check.py", needed)
        self.assertIn("test_support/eci_tuning/results.json", needed)
        workflow = (ROOT / ".github/workflows/windows-edition.yml").read_text()
        filters = re.findall(r"paths: \[(.*?)\]", workflow, re.S)
        self.assertEqual(len(filters), 2)
        for event, block in zip(("push", "pull_request"), filters):
            patterns = re.findall(r"'([^']+)'", block)
            for path in sorted(needed):
                with self.subTest(event=event, path=path):
                    self.assertTrue(any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns),
                                    f"{event} misses {path}, which a Windows test reads")


if __name__ == "__main__":
    unittest.main()
