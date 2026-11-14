"""Review regressions for the evaluation comparison and dataset loader.

Moved from test_eci_fast_check_review.py so the fast-check runner lane can land
on its own; these exercise eci_tuning only.
"""
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import eci_tuning as e
from test_eci_tuning_correctness import comparable_run, observations, compare_with_items


class TuningReviewFixTests(unittest.TestCase):
    def test_05_comparisons_require_same_benchmark_item_sets(self):
        left, right = comparable_run(), comparable_run('right')
        items = observations('left') + observations('right')
        self.assertEqual(list(inspect.signature(e.compare_runs).parameters), ['a','b'])
        self.assertTrue(compare_with_items(left, right, items)['comparable'])
        items[-1]['item_id'] = 'different-example-item'
        result = compare_with_items(left, right, items)
        self.assertFalse(result['comparable'])
        self.assertIn('item set differs', result['reasons'])
        self.assertEqual(result['uncertainty'], 'Not comparable; no winner established.')
        ranked = e.rank(dict(runs=[left,right], items=items, fast_checks=[]))
        self.assertIn('item set differs', ranked['baseline_protocol']['comparisons'][0]['reasons'])

    def test_09_transient_read_error_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'dataset.json'
            source.write_text(json.dumps(dict(schema_version=1,runs=[],items=[])))
            with patch.object(e, '_read_dataset', side_effect=PermissionError('fixture')) as read:
                first, _ = e._configured_view(directory)
                self.assertEqual(first['dataset']['status'], 'unknown')
                self.assertEqual(read.call_count, 1)
            second, _ = e._configured_view(directory)
            self.assertEqual(second['dataset']['status'], 'configured')

    def test_legacy_comparison_projection_is_unknown_without_item_keys(self):
        left, right = comparable_run(), comparable_run('right')
        for row in (left,right):
            row['item_evidence'] = dict(observed=2,truncated=0,truncation_unknown=0,missing_parsed_answer=0)
        result = e.compare_runs(left,right)
        self.assertFalse(result['comparable'])
        self.assertIn('item set unknown',result['reasons'])

    def test_compare_runs_preserves_null_condition_handling(self):
        unknown = dict(run_id='example-unknown',settings=None,components=None,diagnostics=None)
        self.assertFalse(e.compare_runs(unknown,dict(unknown,run_id='example-other'))['comparable'])


if __name__ == '__main__':
    unittest.main()
