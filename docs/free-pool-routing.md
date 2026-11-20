# Free-pool routing (LOU-101)

`agiw/free_pool_call.py` and `agiw/free_pool_routing.json` are the single script and shared config that every machine
(M5, PC, Mac Pro) runs for OpenAI complimentary-token calls. A small `free_pool_machine.json` next to the script names
the machine (`m5`, `pc` or `macpro`), which picks its share of each group's fleet ceiling: 60 / 25 / 15 percent.
See `agiw/free_pool_machine.example.json`. Machine files hold paths only; the key stays in its existing private file.

## Sources
- Eligible models, groups, caps and the "a crossing request is billed in full" rule:
  https://help.openai.com/en/articles/10306912 (checked 2026-11-19).
- Model strengths, context windows and deprecation status: https://developers.openai.com/api/docs/models and each
  model's page under https://developers.openai.com/api/docs/models/.
- List prices (used only for the $1/run worst-case cap): https://developers.openai.com/api/docs/pricing.
- Our evidence: LOU-100 (gpt-5.4-mini 0/3 with corrupt patches; gpt-6.2-sol passed but took shortcuts) and the
  M5 receipts of Nov 17-19 (Grok's Bot 1): on the unlisted 6.2 models, billing depends on request size.

## Groups (as shipped 2026-11-19)
| Group | State | Daily cap | Fleet ceiling used |
| --- | --- | --- | --- |
| official_10M | **enabled**: gpt-5.6-terra, gpt-5.6-luna, gpt-5.4-mini (each probed payer=openai on M5, PC and Mac Pro) | 10M | 9M |
| official_1M | **disabled**: billing unexplained (gpt-5.5 136 tokens payer=developer 2026-11-19) | 1M | 800k |
| empirical_6.2 | **disabled**: same note; counts toward official_1M when re-enabled | none (not listed) | 200k |

The other listed 10M models are blocked as deprecated (gpt-5.4-nano, gpt-5-mini, gpt-5-nano, gpt-4.1-mini, gpt-4o-mini)
or shut down (gpt-5.1-codex-mini, gpt-4.1-nano, o4-mini, o1-mini, codex-mini-latest) per the model pages.
Deprecated 1M models (gpt-5.1, gpt-5, gpt-4.1, gpt-4o, o3) and shut-down ones (gpt-5.1-codex, gpt-5-codex, gpt-5-chat-latest,
gpt-4.5-preview, o1, o1-preview) stay blocked. Any `-pro` id and gpt-5.3-codex are hard-blocked. A model is used on a
machine only if that machine is in its `verified_machines`, meaning a tiny public probe there returned payer=openai.
Requests use the alias, and the served snapshot must be the listed one (for example `gpt-5.4-mini-2026-03-17`).
Otherwise the response is treated as billed. Dated ids map to their base model's group.

## Routing by task kind (`--kind`), as shipped
| Kind | Order | Why |
| --- | --- | --- |
| small_patch | 5.6 Terra, 5.6 Luna | Only 10M models that passed the code probes. Mini is excluded (LOU-100). |
| large_change | local lanes only | No free-pool model is trusted for larger changes. |
| review | 5.6 Terra, 5.4 mini, 5.6 Luna | Terra is the mini tier and balances quality and cost. Mini is fine for reading. |
| docs_text | 5.6 Terra, 5.6 Luna, 5.4 mini | Writing is cheap to verify. |
| extraction | 5.6 Luna, 5.4 mini, 5.6 Terra | Nano and mini tiers are documented for focused, high-volume work. |
| long_context | 5.6 Terra, 5.6 Luna, 5.4 mini | 1.05M-context models first. |

`routes_1m_when_reenabled` keeps the strength order for the 1M group (6.1 Sol and 6 Sol for patches, 6 Astra for larger
changes, and so on) for when its billing is explained.

## Probe results (2026-11-19, public toy snippets, <=140 tokens each)
- 10M group, payer=openai on every machine: gpt-5.6-terra (M5, PC, Mac Pro; code probes passed on all three),
  gpt-5.6-luna (M5 code probes passed, PC and Mac Pro), gpt-5.4-mini (M5, PC, Mac Pro; text only, kept off code routes).
  Response ids are in `probes_2026_11_19` in the config.
- 1M and empirical groups: on the M5, gpt-6.1-sol, gpt-6-sol, gpt-6-astra, gpt-5.6-sol and gpt-6-luna were free, and then
  gpt-5.5 was billed at 136 tokens. On the Mac Pro, gpt-6.1-sol and gpt-6.2-astra were each billed at 65 tokens. The
  tripwire stopped each run, and no more 1M or 6.2 calls were sent.
- The Free Pool project returns 403 model_not_found for dated ids, so requests use the alias (`request_id`) and the
  served snapshot must equal the listed one (`listed_id`). Otherwise the response is treated as billed.

## Request size caps
Every enabled model has `max_total_tokens` / `max_output_tokens`. The default is 2,900 / 1,000 until a payer=openai
receipt shows a larger request was free. gpt-5.4-mini is 12,000 / 4,000 (free at 7,874 in / 3,072 out). The input
estimate is UTF-8 bytes / 3 plus 64 tokens of overhead, then plus `--max-output`.

## Guards
- Pre-call: the content gate (no Linear/Notion/Granola, secrets, .env, raw logs or security work), the per-request size
  caps, a projected-total check against this machine's share of the group ceiling (5% margin), and the $1 worst-case
  cost cap. The plan and a token reservation are written under a lock, so parallel calls can't both use the
  same headroom.
- Empirical group: refused for the rest of the UTC day once any developer-billed row exists. --hard doesn't
  bypass the size caps.
- Post-call: billing.payer other than `openai` (including a missing payer) writes the credit ledger, disables that
  model until 00:00 UTC and exits 7.
- Credit exhausted (402, insufficient_quota, billing errors): exits 9 and sets `free-pool-credit-state.json`, and every
  later call exits 9 until a person checks the dashboard and removes the file.
- Timeout: exits 11, is recorded as possibly billed at the worst-case size, and is never retried.
- Real calls need `--source` and `--lane`. Both are written to every ledger row and to the stdout receipt.

## Root cause of the developer-billed calls
The billed gpt-5.5 probe was response `resp_0fe0b3c17f46e2ed016aff4d202b7087d09d20c6959e867b68` at 19:25:21 UTC
(11:25:21 AM PST) on lane `lou101-probe-m5`: 66 in, 70 out, and the served snapshot was the listed `gpt-5.5-2026-04-23`. It
used the free-pool key and project. The M5 shell had no OPENAI_API_KEY, OPENAI_FREE_POOL_API_KEY or CODEX_API_FREE_POOL_ENV
set, so the loader read `~/.config/agiw/codex-api-free-pool.env`. The request sent the Free Pool project header
`proj_qng3FHr6sJqG9BhXZkNBXb0Z` and the org header. The same key and project returned payer=openai for every 10M call
before and after it. So it was not a wrong key or project. The billing is on OpenAI's side of the 1M group.

The most likely explanation is that the org's 1M complimentary pool, which is shared with the unlisted gpt-6.2 models, was
already full. The help article's rule is that "a request that would cross the cap is billed in full". About 4.6M Astra and
4.5M Sol tokens went out that week (about 1.3M a day), and the Sol calls billed on Nov 18 and 19 were the larger ones.
Our ledgers can't see all org traffic (Agents sessions, OpenCode, other lanes), so this can't be proven from here. It is
recorded as "billing unexplained". The 1M and empirical groups ship disabled until someone checks the dashboard and
explains it. The guard also counts 6.2 usage toward official_1M and blocks a whole group for the UTC day after any
payer=developer row. Earlier, the old M5 guard checked only a daily soft cap, `--hard` skipped the size reroute, and
payer=developer was logged without disabling anything.
