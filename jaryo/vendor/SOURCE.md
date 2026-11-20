# Vendored Jaryo kit tokens

`jaryo.tokens.json` is a byte-for-byte copy of the Jaryo kit token file. Do not edit it here; change it in the Jaryo repository and copy it again.

| Field | Value |
| --- | --- |
| Repository | Jaryo kit repository (`jaryo`) |
| Path | `tokens/jaryo.tokens.json` |
| Commit | `f2ecf0fd98a27c9de7340ef5bdf3c8da1fe21eb9` (Jaryo `main`, the 2026-11-19 merge of Jaryo PR #91, which made this the canonical tokens file) |
| Git blob | `469a23f50581dc27d2d98815280a75c4b7656cea` |
| SHA-256 | `7d23cf27ee6eac5d322263cb929f6fe72c5b9a4a3bd8e10e10ffe64919fead22` |

`node jaryo/generate.mjs` turns it into the `--jaryo-*` custom properties at the top of `jaryo/primitives.css` (LOU-59). Suite scale steps whose value equals a kit token (`--j-space-1`…`6`, `--j-type-meta`/`ui`/`body`/`name`/`title`, `--j-control-min`) now read that kit token. Steps without an equal kit token keep their own value, so the rendered result does not change. `node jaryo/generate.mjs --check` reports drift.

To refresh: download the file at a new commit, replace `jaryo.tokens.json`, update the table above, run `node jaryo/generate.mjs`, and run the tests.

Not converged yet: the Suite colour palette (`--j-*` colours, Copper accent, graphite dark surfaces) differs from the kit's black-and-white palette, and `--j-type-display` (28px) has no equal kit step. Moving those is a visible change and is left for a separate, reviewed decision.
