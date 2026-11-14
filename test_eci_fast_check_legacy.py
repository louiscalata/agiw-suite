"""Frozen v1 compatibility fixture, generated with archived origin/main code.

Source commit: 46ebf832ae6532fc1c561dc9bf8d59d5e77c2bf5 (PR #141).
Export: git archive origin/main | tar -x -C "$TMPDIR/fc3-base"
In that export, test_eci_fast_check.run(datasets(6), baseline=run(datasets(6)))
used an answer callable raising ConnectionError for the first frozen item,
returning (None, 4, 2) for the second, and ("A", 4, 2) for the others.
No model or real fast check was run. Do not regenerate via the current writer.
"""
import copy
import json
import unittest

import eci_fast_check as f
import eci_fast_check_adapter as a
from test_eci_fast_check import datasets, run, rehash

FROZEN_V1 = json.loads(r'''
{
  "alpha": "0.05",
  "baseline": {
    "outcomes_sha256": "36e10358edf64d8b997b5aee34c7a479f016ac59d17a8eea2ff2745c052f8d0a",
    "record": {
      "alpha": "0.05",
      "baseline": null,
      "benchmarks": [
        {
          "accuracy": 1.0,
          "accuracy_over_parsed_items": 1.0,
          "answer_counts": {
            "A": 6
          },
          "answer_seconds": 12,
          "attempted": 6,
          "benchmark": "arc",
          "completed": 6,
          "correct": 6,
          "errors": 0,
          "interval95": [
            0.6096657120978346,
            1.0
          ],
          "normalized_interval95": [
            0.4795542827971128,
            1.0
          ],
          "normalized_score": 1.0,
          "observations": [
            {
              "answer_value": "A",
              "completion_tokens": 4,
              "correct": true,
              "cut_off": false,
              "error": null,
              "finish_reason": "stop",
              "item_id": "3",
              "outcome": "correct",
              "parsed": true,
              "prompt_tokens": 8,
              "seconds": 2
            },
            {
              "answer_value": "A",
              "completion_tokens": 4,
              "correct": true,
              "cut_off": false,
              "error": null,
              "finish_reason": "stop",
              "item_id": "4",
              "outcome": "correct",
              "parsed": true,
              "prompt_tokens": 8,
              "seconds": 2
            },
            {
              "answer_value": "A",
              "completion_tokens": 4,
              "correct": true,
              "cut_off": false,
              "error": null,
              "finish_reason": "stop",
              "item_id": "1",
              "outcome": "correct",
              "parsed": true,
              "prompt_tokens": 8,
              "seconds": 2
            },
            {
              "answer_value": "A",
              "completion_tokens": 4,
              "correct": true,
              "cut_off": false,
              "error": null,
              "finish_reason": "stop",
              "item_id": "2",
              "outcome": "correct",
              "parsed": true,
              "prompt_tokens": 8,
              "seconds": 2
            },
            {
              "answer_value": "A",
              "completion_tokens": 4,
              "correct": true,
              "cut_off": false,
              "error": null,
              "finish_reason": "stop",
              "item_id": "0",
              "outcome": "correct",
              "parsed": true,
              "prompt_tokens": 8,
              "seconds": 2
            },
            {
              "answer_value": "A",
              "completion_tokens": 4,
              "correct": true,
              "cut_off": false,
              "error": null,
              "finish_reason": "stop",
              "item_id": "5",
              "outcome": "correct",
              "parsed": true,
              "prompt_tokens": 8,
              "seconds": 2
            }
          ],
          "outcome_counts": {
            "correct": 6,
            "error": 0,
            "truncated_empty": 0,
            "unparsed": 0,
            "wrong": 0
          },
          "outcome_rates": {
            "correct": 1.0,
            "error": 0.0,
            "truncated_empty": 0.0,
            "unparsed": 0.0,
            "wrong": 0.0
          },
          "parsed": 6,
          "planned": 6,
          "prompt_tokens": 48,
          "stop_reason": "complete",
          "timeouts": 0,
          "tokens": 24,
          "tokens_per_s": 2.0,
          "top_answer_count": 6,
          "top_answer_share": 1.0,
          "truncations": 0,
          "unknown_finish": 0,
          "wall_time_s": 0
        }
      ],
      "comparison": {
        "arc": {
          "alpha": "0.05",
          "baseline_only": null,
          "both_correct": null,
          "both_wrong": null,
          "model_only": null,
          "n_baseline": null,
          "n_candidate": 6,
          "n_discordant": null,
          "n_expected": 6,
          "n_shared": null,
          "p_exact": null,
          "p_value": null,
          "reason": "baseline cannot be resolved and verified",
          "verdict": "INCONCLUSIVE",
          "verification": "UNVERIFIED"
        }
      },
      "created_at": "2026-11-12T12:00:00+00:00",
      "hardware": {
        "cpu": "fixture-cpu",
        "gpu": "fixture-gpu",
        "machine_label": "fixture-machine-a",
        "memory": {
          "bytes": 1000
        },
        "memory_bytes": 1000,
        "runtime_build_id": "fixture-build"
      },
      "journal": null,
      "kind": "agiw_fast_check",
      "model": {
        "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "file_size_bytes": 100,
        "name": "synthetic candidate",
        "quantization": "Q4_K_M"
      },
      "plan": {
        "benchmarks": [
          {
            "answer_options": 4,
            "benchmark": "arc",
            "chance": "0.25",
            "dataset_revision": null,
            "dataset_sha256": "bb690be68fa414ad58c52163385ee44430373c93d729b13e265b27517b8bbc66",
            "item_ids": [
              "3",
              "4",
              "1",
              "2",
              "0",
              "5"
            ],
            "population": 6,
            "sampling": "simple_random_without_replacement",
            "seed": "58828607da703953ea2bcf519f7b5b81a74edebd1798478ebcc72d282d47d192",
            "strata": [
              {
                "label": null,
                "population": 6,
                "selected": 6
              }
            ],
            "stratum_key": null,
            "whole_dataset": true
          }
        ],
        "items_per_benchmark": 200,
        "kind": "agiw_fast_check_plan",
        "plan_id": "cb218422149dc1e425d78407083b204c55ece457742ea7416713b61f52da2813",
        "plan_version": "fast-check-plan-v1",
        "schema_version": 1,
        "wall_budget_s": null
      },
      "post": {
        "advisory": null,
        "decision": "REJECT",
        "inputs": {
          "alpha": "0.05",
          "evidence": [
            {
              "answer_options": 4,
              "attempted": 6,
              "chance": "0.25",
              "correct": 6,
              "errors": 0,
              "parsed": 6,
              "planned": 6,
              "top_answer_count": 6,
              "truncations": 0,
              "unknown_finish": 0
            }
          ],
          "max_error_rate": "0.01",
          "max_interval_width": "0.2",
          "max_truncation_rate": "0.05",
          "max_unparsed_rate": "0.10",
          "min_coverage": "1"
        },
        "local_decision": "REJECT",
        "reasons": [
          "A Wilson interval meets or exceeds the declared maximum width."
        ]
      },
      "pre": {
        "advisory": null,
        "decision": "CHECK",
        "inputs": {
          "available_memory_bytes": 800,
          "file_size_bytes": 100,
          "quantization": "Q4_K_M",
          "safety_margin": "0.25"
        },
        "local_decision": "CHECK",
        "reasons": [
          "File fits the estimate; actual runtime/KV memory remains unverified."
        ]
      },
      "protocol_id": "agiw-fast-check-v1",
      "record_id": "edaf35fc487d20942e6ff9b5bfd647a718c42ba362efee1eb780972b72c6d6fe",
      "runtime_settings": {
        "backend": "fake",
        "backend_version": "1",
        "ctx": 4096,
        "max_tokens": 128,
        "n_epochs": 1,
        "prompt_template_id": "fixture-v1",
        "scorer_id": "parsed_exact_match-v1",
        "seed": 42,
        "stop": [],
        "system_message": "",
        "temperature": 0
      },
      "schema_version": 1,
      "wall_time_s": 0
    },
    "record_id": "edaf35fc487d20942e6ff9b5bfd647a718c42ba362efee1eb780972b72c6d6fe"
  },
  "benchmarks": [
    {
      "accuracy": 0.6666666666666666,
      "accuracy_over_parsed_items": 1.0,
      "answer_counts": {
        "A": 4
      },
      "answer_seconds": 10,
      "attempted": 6,
      "benchmark": "arc",
      "completed": 4,
      "correct": 4,
      "errors": 1,
      "interval95": [
        0.299993315138392,
        0.9032285888942195
      ],
      "normalized_interval95": [
        0.066657753517856,
        0.8709714518589593
      ],
      "normalized_score": 0.5555555555555555,
      "observations": [
        {
          "answer_value": null,
          "completion_tokens": null,
          "correct": false,
          "cut_off": false,
          "error": "ConnectionError",
          "finish_reason": null,
          "item_id": "3",
          "outcome": "error",
          "parsed": false,
          "prompt_tokens": null,
          "seconds": 0
        },
        {
          "answer_value": null,
          "completion_tokens": 4,
          "correct": false,
          "cut_off": false,
          "error": null,
          "finish_reason": "stop",
          "item_id": "4",
          "outcome": "unparsed",
          "parsed": false,
          "prompt_tokens": 8,
          "seconds": 2
        },
        {
          "answer_value": "A",
          "completion_tokens": 4,
          "correct": true,
          "cut_off": false,
          "error": null,
          "finish_reason": "stop",
          "item_id": "1",
          "outcome": "correct",
          "parsed": true,
          "prompt_tokens": 8,
          "seconds": 2
        },
        {
          "answer_value": "A",
          "completion_tokens": 4,
          "correct": true,
          "cut_off": false,
          "error": null,
          "finish_reason": "stop",
          "item_id": "2",
          "outcome": "correct",
          "parsed": true,
          "prompt_tokens": 8,
          "seconds": 2
        },
        {
          "answer_value": "A",
          "completion_tokens": 4,
          "correct": true,
          "cut_off": false,
          "error": null,
          "finish_reason": "stop",
          "item_id": "0",
          "outcome": "correct",
          "parsed": true,
          "prompt_tokens": 8,
          "seconds": 2
        },
        {
          "answer_value": "A",
          "completion_tokens": 4,
          "correct": true,
          "cut_off": false,
          "error": null,
          "finish_reason": "stop",
          "item_id": "5",
          "outcome": "correct",
          "parsed": true,
          "prompt_tokens": 8,
          "seconds": 2
        }
      ],
      "outcome_counts": {
        "correct": 4,
        "error": 1,
        "truncated_empty": 0,
        "unparsed": 1,
        "wrong": 0
      },
      "outcome_rates": {
        "correct": 0.6666666666666666,
        "error": 0.16666666666666666,
        "truncated_empty": 0.0,
        "unparsed": 0.16666666666666666,
        "wrong": 0.0
      },
      "parsed": 4,
      "planned": 6,
      "prompt_tokens": null,
      "stop_reason": "complete",
      "timeouts": 0,
      "tokens": null,
      "tokens_per_s": null,
      "top_answer_count": 4,
      "top_answer_share": 1.0,
      "truncations": 0,
      "unknown_finish": 1,
      "wall_time_s": 0
    }
  ],
  "comparison": {
    "arc": {
      "alpha": "0.05",
      "baseline_only": 0,
      "both_correct": 4,
      "both_wrong": 0,
      "model_only": 0,
      "n_baseline": 6,
      "n_candidate": 4,
      "n_discordant": 0,
      "n_expected": 6,
      "n_shared": 4,
      "p_exact": [
        "1",
        "1"
      ],
      "p_value": 1.0,
      "reason": "incomplete pairs",
      "verdict": "INCONCLUSIVE",
      "verification": "VERIFIED"
    }
  },
  "created_at": "2026-11-12T12:00:00+00:00",
  "hardware": {
    "cpu": "fixture-cpu",
    "gpu": "fixture-gpu",
    "machine_label": "fixture-machine-a",
    "memory": {
      "bytes": 1000
    },
    "memory_bytes": 1000,
    "runtime_build_id": "fixture-build"
  },
  "journal": null,
  "kind": "agiw_fast_check",
  "model": {
    "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "file_size_bytes": 100,
    "name": "synthetic candidate",
    "quantization": "Q4_K_M"
  },
  "plan": {
    "benchmarks": [
      {
        "answer_options": 4,
        "benchmark": "arc",
        "chance": "0.25",
        "dataset_revision": null,
        "dataset_sha256": "bb690be68fa414ad58c52163385ee44430373c93d729b13e265b27517b8bbc66",
        "item_ids": [
          "3",
          "4",
          "1",
          "2",
          "0",
          "5"
        ],
        "population": 6,
        "sampling": "simple_random_without_replacement",
        "seed": "58828607da703953ea2bcf519f7b5b81a74edebd1798478ebcc72d282d47d192",
        "strata": [
          {
            "label": null,
            "population": 6,
            "selected": 6
          }
        ],
        "stratum_key": null,
        "whole_dataset": true
      }
    ],
    "items_per_benchmark": 200,
    "kind": "agiw_fast_check_plan",
    "plan_id": "cb218422149dc1e425d78407083b204c55ece457742ea7416713b61f52da2813",
    "plan_version": "fast-check-plan-v1",
    "schema_version": 1,
    "wall_budget_s": null
  },
  "post": {
    "advisory": null,
    "decision": "REJECT",
    "inputs": {
      "alpha": "0.05",
      "evidence": [
        {
          "answer_options": 4,
          "attempted": 6,
          "chance": "0.25",
          "correct": 4,
          "errors": 1,
          "parsed": 4,
          "planned": 6,
          "top_answer_count": 4,
          "truncations": 0,
          "unknown_finish": 1
        }
      ],
      "max_error_rate": "0.01",
      "max_interval_width": "0.2",
      "max_truncation_rate": "0.05",
      "max_unparsed_rate": "0.10",
      "min_coverage": "1"
    },
    "local_decision": "REJECT",
    "reasons": [
      "A Wilson interval meets or exceeds the declared maximum width.",
      "Errors exceed the declared maximum rate.",
      "runtime/model incompatibility suspected: answers could not be parsed",
      "Cutoff/finish evidence is unknown."
    ]
  },
  "pre": {
    "advisory": null,
    "decision": "CHECK",
    "inputs": {
      "available_memory_bytes": 800,
      "file_size_bytes": 100,
      "quantization": "Q4_K_M",
      "safety_margin": "0.25"
    },
    "local_decision": "CHECK",
    "reasons": [
      "File fits the estimate; actual runtime/KV memory remains unverified."
    ]
  },
  "protocol_id": "agiw-fast-check-v1",
  "record_id": "4468afae0fa2f444bcdb82ecd20abc2d0dddfef720a2585ea319c57be35034a5",
  "runtime_settings": {
    "backend": "fake",
    "backend_version": "1",
    "ctx": 4096,
    "max_tokens": 128,
    "n_epochs": 1,
    "prompt_template_id": "fixture-v1",
    "scorer_id": "parsed_exact_match-v1",
    "seed": 42,
    "stop": [],
    "system_message": "",
    "temperature": 0
  },
  "schema_version": 1,
  "wall_time_s": 0
}
''')


class RecordVersionTests(unittest.TestCase):
    def test_frozen_base_record_with_error_and_unparsed_validates_unchanged(self):
        record = copy.deepcopy(FROZEN_V1)
        bench = record['benchmarks'][0]
        self.assertEqual(record['schema_version'], 1)
        self.assertEqual(bench['errors'], 1)
        self.assertEqual(bench['outcome_counts']['unparsed'], 1)
        self.assertEqual(bench['unknown_finish'], 1)
        self.assertEqual(record['comparison']['arc']['n_candidate'], 4)
        self.assertEqual(f.validate_record(record), FROZEN_V1)
        self.assertEqual(record, FROZEN_V1)

    def test_new_records_use_v2_and_validate_with_v1_bound_baseline(self):
        record = run(datasets(6), baseline=FROZEN_V1)
        self.assertEqual(record['schema_version'], 2)
        self.assertEqual(f.validate_record(record), record)
        self.assertEqual(record['comparison']['arc']['n_baseline'], 5)
        self.assertEqual(record['baseline']['record'], FROZEN_V1)

    def test_tuning_ingest_accepts_frozen_v1_record_next_to_clean_v2_record(self):
        import tempfile
        from pathlib import Path
        import eci_tuning as e
        clean = run(datasets(6))
        self.assertEqual(clean['schema_version'], 2)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'mixed-versions.json'
            source.write_text(json.dumps(dict(schema_version=1, runs=[], items=[],
                                              fast_checks=[copy.deepcopy(FROZEN_V1), clean])))
            ingested = e.ingest([source])
        self.assertEqual(sorted(r['record_id'] for r in ingested['fast_checks']),
                         sorted([FROZEN_V1['record_id'], clean['record_id']]))
        self.assertEqual([r['schema_version'] for r in ingested['fast_checks']], [1, 2])

    def test_published_schema_accepts_both_record_versions(self):
        from pathlib import Path
        schema = f.load(Path(__file__).resolve().parent / 'eci_fast_check.schema.json')
        record_schema = schema['$defs']['record']
        self.assertEqual(record_schema['properties']['schema_version']['enum'], [1, 2])
        self.assertEqual(f.SCHEMA_VERSIONS, (1, 2))
        self.assertEqual(f.SCHEMA_VERSION, 2)
        self.assertLessEqual(set(record_schema['required']), set(FROZEN_V1))
        self.assertLessEqual(set(FROZEN_V1), set(record_schema['properties']))
        bench_schema = schema['$defs']['benchmark']
        self.assertLessEqual(set(bench_schema['required']), set(FROZEN_V1['benchmarks'][0]))
        self.assertLessEqual(set(FROZEN_V1['benchmarks'][0]), set(bench_schema['properties']))

    def test_v1_legacy_rules_are_not_the_v2_rules(self):
        bench = FROZEN_V1['benchmarks'][0]
        spec = FROZEN_V1['plan']['benchmarks'][0]
        args = (spec, bench['observations'], bench['wall_time_s'], bench['stop_reason'])
        self.assertEqual(f._summarize_v1(*args)['unknown_finish'], 1)
        self.assertEqual(f.summarize(*args)['unknown_finish'], 0)
        self.assertEqual(sum(v is not None for v in f._outcomes_v1(bench).values()), 4)
        self.assertEqual(sum(v is not None for v in f.outcomes(bench).values()), 5)

    def test_v1_records_cannot_carry_schema_2_evidence(self):
        for change, reason in (
            (lambda r: r.update(provenance={}), 'predate provenance'),
            (lambda r: r['benchmarks'][0]['observations'][2].update(attempts=1), 'predate unit and retry'),
            (lambda r: r['benchmarks'][0].update(stop_reason='errors'), 'invalid stop reason'),
        ):
            with self.subTest(reason=reason):
                record = copy.deepcopy(FROZEN_V1)
                change(record)
                with self.assertRaisesRegex(f.ValidationError, reason):
                    f.validate_record(rehash(record))

    def test_legacy_derived_claims_and_version_are_still_verified(self):
        for change, reason in (
            (lambda r: r['benchmarks'][0].update(unknown_finish=0), 'summary disagrees'),
            (lambda r: r['comparison']['arc'].update(n_candidate=5), 'paired comparison disagrees'),
            (lambda r: r['post'].update(decision='ADMIT'), 'post decision disagrees'),
            (lambda r: r.update(schema_version=3), 'unsupported fast-check record'),
        ):
            with self.subTest(reason=reason):
                record = copy.deepcopy(FROZEN_V1)
                change(record)
                with self.assertRaisesRegex(f.ValidationError, reason):
                    f.validate_record(rehash(record))


class UnitCaseTests(unittest.TestCase):
    def test_word_units_and_multi_letter_abbreviations_ignore_case(self):
        for value, expected in (('72 Dollars', '72'), ('5 usd', '5'), ('3 Hours', '3'),
                                ('12 KM', '12'), ('Answer: 7 Apples', '7')):
            with self.subTest(value=value):
                self.assertEqual(a.number_answer(value), expected)
        self.assertEqual(a.number_parts('12 Cents'), ('12', 'cents'))

    def test_cents_cannot_match_unitless_reference_and_scale_stays_unparsed(self):
        row = dict(id='fixture', question='Example?', answer='#### 12')
        for value in ('12 Cents', '12 CENTS', '12 M', '12 G'):
            with self.subTest(value=value):
                self.assertEqual(a.parse_answer(value, row, 'gsm8k'), (None, None))
        for unit in ('m', 'g', 'l', 'L'):
            self.assertEqual(a.number_answer('12 ' + unit), '12')

    def test_capital_litre_parses_and_capital_scale_letters_do_not(self):
        row = dict(id='fixture', question='Example?', answer='#### 5')
        self.assertEqual(a.parse_answer('Answer: 5 L', row, 'gsm8k'), ('5', 'numeric_marked_final'))
        self.assertEqual(a.number_parts('Answer: 5 L'), ('5', 'l'))
        self.assertEqual(a.number_parts('12 M'), (None, None))
        self.assertEqual(a.number_parts('12 G'), (None, None))
        self.assertEqual(a.number_parts('12 m'), ('12', 'm'))

    def test_marked_capitalized_dollars_preserves_parse_rule(self):
        row = dict(id='fixture', question='Example?', answer='#### 72')
        self.assertEqual(a.parse_answer('Answer: 72 Dollars', row, 'gsm8k'),
                         ('72', 'numeric_marked_final'))
