# Nisi

[![CI](https://github.com/louiscalata/nisi/actions/workflows/ci.yml/badge.svg)](https://github.com/louiscalata/nisi/actions/workflows/ci.yml)
[![Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/louiscalata/nisi/blob/main/LICENSE)
[![Node.js 22+](https://img.shields.io/badge/node-%3E%3D22-339933.svg)](https://github.com/louiscalata/nisi/blob/main/package.json)

Nisi runs one coding task through draft, checks, tests, separate review and bounded repair, and returns a report. It does not apply the files.

**Status:** [v0.3.2](https://github.com/louiscalata/nisi/releases/tag/v0.3.2) was released on 18 November 2026 as a GitHub Release with `nisi-0.3.2.tgz` and `SHA256SUMS`. Requires Node.js 22 or newer on macOS, Linux or Windows. Apache-2.0. Nisi is not on npm.

## Quick start

Install Node.js 22 or newer. Then:

```bash
git clone --branch v0.3.2 --depth 1 https://github.com/louiscalata/nisi.git
cd nisi
node bin/nisi.mjs demo
```

You should see this JSON:

```json
{
  "outcome": "COMPLETED",
  "code": null,
  "repairAttempts": 1,
  "reportStored": true,
  "modelCalls": 0
}
```

The demo uses deterministic callbacks to simulate model drafting and review; it calls no model. It repairs an intentionally incorrect module once, writes a report in a temporary directory, checks it by read-back, and deletes it. `node examples/workflow.mjs` keeps the report.

From a source checkout, `npm ci` then `npm run check` runs the structural check and the tests. Installing the release archive is described in [Install and use](https://github.com/louiscalata/nisi/blob/main/docs/install.md).

## Jev pre- and post-analysis

Jev is TypeSafe's hosted System One model. In v0.3.1, calling `planWithJev` without a `judge` uses the hosted client when `TYPESAFE_API_KEY` is set; a reviewer created without a judge uses that client when its review runs. Creating the reviewer does not send a request. Unreleased source on main requires a `judge` function or `judge: 'typesafe'`. With the key set and no judge, `planWithJev` rejects and `createJevReviewer` throws `JEV_JUDGE_REQUIRED`; each prints one line to stderr and sends nothing. The unreleased harder-task runner requires `--judge typesafe` or a judge module for Jev planning or review.

**Pre-analysis** (`planWithJev`) asks whether the task needs the model to reason (`reason` or `direct`) and whether the task is missing information. The default map gives `direct` tasks `reasoningEffort: 'none'` and 1,024 output tokens, and `reason` tasks 4,096 tokens. That choice is a setting, not evidence that the setting is better.

**Post-analysis** (`createJevReviewer`) records Jev's probability that the candidate meets every acceptance criterion. By default the review passes, and it still uses one of the task's required-reviewer slots. `gate: true` fails the review when the probability is below `threshold` (default 0.5) and sends the candidate to repair. If the judge throws or returns an invalid answer, the run ends `BLOCKED` with `REVIEW_UNAVAILABLE`.

On 16 harder tasks with one local model, each figure from a single run, the Jev plan matched the adapter defaults (14 of 16) and showed no demonstrated gain in accuracy or speed. On 31 labeled answers, Jev's ranking was too weak to gate a run (AUC 0.78). Numbers and the exact data that leaves the machine are in [Local models and Jev](https://github.com/louiscalata/nisi/blob/main/docs/local-models.md#jev-pre--and-post-analysis-optional).

## What is in 0.3.2

- A host-held journal anchor (`nisi/history/run-journal-anchor`) that can detect rollback and rewrite, plus harder journal reads (a 1 GiB request cap, `FILE_CHANGED` on size changes, and UTF-8 conversion refusals).
- The remote TypeSafe judge is opt-in: pass `judge: 'typesafe'` or `--judge typesafe`. `TYPESAFE_API_KEY` alone prints one line and sends nothing.
- The built-in Jev client uses Node's `https` module, so timeouts may be longer than five minutes. An injected `fetch` is unchanged.
- `--tasks v2` is a separate 42-task set that replaces the default 16-task v1 set. The two share no tasks.

The [changelog](https://github.com/louiscalata/nisi/blob/main/CHANGELOG.md) is the full list. The published notes are on the [v0.3.2 release](https://github.com/louiscalata/nisi/releases/tag/v0.3.2).

## How it works

An **adapter** is a callback object you supply that connects one role to your tool or model. A **candidate** is a snapshot of the changed file names and their complete contents, not a patch. Nisi validates everything an adapter returns.

1. Nisi binds the task and asks your code whether the permitted files may be used.
2. The author adapter drafts a candidate.
3. Static checks run, then tests. A failed check skips the tests, and a failed test skips review.
4. Each reviewer adapter reviews the candidate separately.
5. A failed check, test or review sends the candidate back to the author for repair, within the repair budget. The repaired candidate returns to step 3 and must pass fresh checks, tests and review.
6. The run ends with an outcome (`COMPLETED`, `FAILED`, `BLOCKED`, `CANCELLED`, `TIMED_OUT`, `REPAIR_LIMIT` or `NO_PROGRESS`) and a report. Cancellation, a deadline, missing evidence or a repeated candidate also end it.

Nisi never writes the files; your application decides whether to apply the candidate. In review mode you supply the candidate and Nisi skips drafting and repair. The full flow diagram is in [Architecture](https://github.com/louiscalata/nisi/blob/main/docs/architecture.md#execution-flow).

## Check a journal on your machine

Real run journals are not in this repository. Point the checker at a directory or one file. It reads locally and prints counts, sha256 and reason codes.

```bash
scripts/validate-real-data.sh --data-dir /absolute/path/to/your/nisi-data
scripts/validate-real-data.sh --journal /absolute/path/to/journal.jsonl
```

`scripts/validate-real-data.sh --smoke` checks only `fixtures/sample-data/`, which is synthetic. `verified: true` on a journal means the hash chain matches the file, not that the events happened or that a signature was checked.

## Limits

- **Scope.** The CLI runs two fixed demonstrations and `validate`. It does not run your repository. `demo` calls no model. `local-model` needs a loopback chat server that you run, an endpoint and two different model names. Neither command runs model-generated code or changes a repository.
- **Distribution.** Nisi is not on the npm registry. The archive installs no models and starts no model server.
- **Trust.** Nisi checks what your callbacks report. It cannot see what they did. `COMPLETED` does not authorize applying files or shipping.
- **Generated code.** If you run generated code, isolate it.
- **Run journal.** The optional run journal is a hash-chained record of a run. Its `verified: true` means internal consistency, not authenticity.
- **Data sent to Jev.** In v0.3.2, the built-in client is used only when you select it explicitly (`judge: 'typesafe'`, or `--judge typesafe` in the harder-task runner). The key alone prints one line and sends nothing. If you do opt in, planning sends the task's id, mode, language, allowed file names and acceptance criteria. Review also sends the candidate's file contents and whether the tests passed. A supplied `judge` function controls its own transmission.
- **Jev as reviewer.** By default it passes, yet counts as a required reviewer. A failing judge blocks the run. The measured ranking was too weak to use as a gate. See the section above.
- **Not measured.** The benchmarks are integration checks, not accuracy estimates. The local pilot used one loaded model and 12 tasks, and Nisi tied a handwritten checked loop.

## Verification

v0.3.2 is covered by the test suite and Linux/Windows CI on Node.js 22 and 24. On the development Mac, `npm run check` passed (552 passed, 0 failed, 1 skipped) after a test-only fixture fix (PR #70). Hosted CI still has no macOS job. A local-model end-to-end run and the harder-task re-measure were **NOT_RUN**. The [Windows hardware check](https://github.com/louiscalata/nisi/blob/main/docs/verification-v0.3-windows.md) covers one PC on v0.3.0 and was not repeated. The [published release notes](https://github.com/louiscalata/nisi/releases/tag/v0.3.2) record checks performed at publication.

## Learn more

- [Documentation index](https://github.com/louiscalata/nisi/blob/main/docs/README.md)
- [Install and use](https://github.com/louiscalata/nisi/blob/main/docs/install.md)
- [Workflow API](https://github.com/louiscalata/nisi/blob/main/docs/workflow-api.md)
- [Local models and Jev](https://github.com/louiscalata/nisi/blob/main/docs/local-models.md)
- [Run journal and store](https://github.com/louiscalata/nisi/blob/main/docs/run-journal.md)
- [Benchmarks](https://github.com/louiscalata/nisi/blob/main/docs/benchmarks.md)
- [Changelog](https://github.com/louiscalata/nisi/blob/main/CHANGELOG.md)

## License and support

Apache-2.0, maintained by Louis Calata.

Questions and reproducible reports are welcome in [GitHub Issues](https://github.com/louiscalata/nisi/issues). Pull requests are not accepted. See [CONTRIBUTING.md](https://github.com/louiscalata/nisi/blob/main/CONTRIBUTING.md), [SECURITY.md](https://github.com/louiscalata/nisi/blob/main/SECURITY.md) and [LICENSE](https://github.com/louiscalata/nisi/blob/main/LICENSE).
