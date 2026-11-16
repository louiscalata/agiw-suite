"""Every workflow job uses the same checkout and setup-python pins."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))


def pins(action):
    found = {}
    for workflow in WORKFLOWS:
        for number, line in enumerate(workflow.read_text().splitlines(), 1):
            match = re.search(rf"uses:\s*(actions/{action}@\S+)", line)
            if match:
                found[f"{workflow.name}:{number}"] = match.group(1)
    return found


class WorkflowPinTests(unittest.TestCase):
    def test_workflows_exist(self):
        self.assertTrue(WORKFLOWS)

    def test_checkout_pin_is_the_same_everywhere(self):
        found = pins("checkout")
        self.assertTrue(found)
        self.assertEqual(set(found.values()), {"actions/checkout@v7.0.1"}, found)

    def test_setup_python_pin_is_the_same_everywhere(self):
        found = pins("setup-python")
        self.assertTrue(found)
        self.assertEqual(set(found.values()), {"actions/setup-python@v7.0.0"}, found)


if __name__ == "__main__":
    unittest.main()
