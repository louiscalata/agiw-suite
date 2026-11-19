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

## Groups
| Group | Models enabled | Daily cap | Fleet ceiling used |
| --- | --- | --- | --- |
| official_1M | gpt-6.1-sol, gpt-6-astra, gpt-6-sol, gpt-6-luna, gpt-5.6-sol (gpt-5.5, gpt-5.4, gpt-5.2 listed but off, see below) | 1M | 800k |
| official_10M | gpt-5.6-terra, gpt-5.6-luna, gpt-5.4-mini | 10M | 9M |
| empirical_6.2 | gpt-6.2-astra (--hard, small only); gpt-6.2-sol off | none (not listed) | 200k |

Listed but blocked: deprecated (gpt-5.1, gpt-5, gpt-4.1, gpt-4o, o3, gpt-5.4-nano, gpt-5-mini, gpt-5-nano,
gpt-4.1-mini, gpt-4o-mini) and shut down (gpt-5.1-codex, gpt-5-codex, gpt-5-chat-latest, gpt-4.5-preview, o1,
o1-preview, gpt-5.1-codex-mini, gpt-4.1-nano, o4-mini, o1-mini, codex-mini-latest). Any `-pro` id and
gpt-5.3-codex are hard-blocked. Requests use the alias; the served snapshot must be the listed one
(for example `gpt-5.4-mini-2026-03-17`). Other dated snapshots are refused, and dated ids map to their base model's group.

## Probe results (2026-11-19, public toy snippets, <=140 tokens each)
- M5, all payer=openai and passing both code probes: gpt-6.1-sol, gpt-6-sol, gpt-6-astra, gpt-5.6-sol, gpt-6-luna,
  gpt-5.6-terra and gpt-5.6-luna. gpt-5.4-mini passed text and the trivial fix but stays off code routes (LOU-100).
- After those, gpt-5.5 was free at 65 tokens and then billed (payer=developer) at 136 tokens on the M5. On the Mac Pro,
  gpt-6.1-sol and gpt-6.2-astra were each billed at 65 tokens, while gpt-5.6-terra (10M) stayed free. Each time the
  tripwire stopped the run.
- The Free Pool project returns 403 model_not_found for dated ids, so requests use the alias (`request_id`) and the
  served snapshot must equal the listed one (`listed_id`). Otherwise the response is treated as billed.

## Request size caps
Every enabled model has `max_total_tokens` / `max_output_tokens`. The default is 2,900 / 1,000 until a payer=openai
receipt shows a larger request was free. gpt-5.4-mini is 12,000 / 4,000 (free at 7,874 in / 3,072 out). The input
estimate is UTF-8 bytes / 3 plus 64 tokens of overhead, then plus `--max-output`.

## Routing by task kind (`--kind`)
| Kind | Order | Why |
| --- | --- | --- |
| small_patch | 6.1 Sol, 6 Sol, 5.4 (off), 5.5 (off), 5.6 Terra, 6.2 Astra | Docs list 6.1 Sol and 6 Sol for complex coding and 5.4 as the affordable coding model. Mini is excluded (LOU-100). |
| large_change | 6 Astra, 6.1 Sol, 5.5 (off), 6 Sol, 6.2 Astra | 6 Astra is the listed reasoning model for complex coding. |
| review | 6.1 Sol, 6 Astra, 5.5 (off), 5.6 Sol, 5.6 Terra | Quality-sensitive reading; 10M fallback last. |
| docs_text | 5.6 Terra, 5.6 Sol, 5.6 Luna, 5.4 mini, 6 Luna | Writing is cheap to verify, so 10M first. |
| extraction | 5.6 Luna, 5.4 mini, 5.6 Terra, 6 Luna | Nano and mini tiers are documented for focused, high-volume work. |
| long_context | 5.6 Terra, 5.6 Luna, 5.4 mini, 5.4 (off), 6 Luna | 1.05M-context 10M models first, so long input doesn't drain the 1M group. |

Code kinds only use models with `code_trusted: true`, which is set after the model passes a public probe. When a model
is blocked (disabled, over its size cap, near its group's cap, over the $1 cap, or tripped today), the next model in
the same kind is tried. `--model` still works for older callers and falls back through `legacy_model_fallback`.

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
The org's 1M complimentary pool is shared with the unlisted gpt-6.2 models and was full. That is the help article's rule
("a request that would cross the cap is billed in full"): while the pool is almost full, small requests still fit and
come back payer=openai, and larger ones cross and are billed. It fits all the evidence:
- The Sol calls billed on Nov 18 3:39 PM and Nov 19 7:00 AM and 10:19 AM PST were 9,998, 18,738 and 3,145 tokens, while
  calls of at most 2,989 tokens stayed free.
- On Nov 19, gpt-5.5 (officially 1M) was free at 65 tokens and billed at 136. Then gpt-6.1-sol and gpt-6.2-astra were billed at
  65 tokens, and the 10M group stayed free.
- About 4.6M Astra and 4.5M Sol tokens went out that week, about 1.3M a day, which is more than the 1M pool.
Our ledgers can't see all org traffic (Agents sessions, OpenCode, other lanes), so the guard now counts 6.2 usage toward
official_1M, blocks a whole group for the UTC day after any payer=developer row, and carries a shipped
`blocked_utc_days` list for fleet-wide blocks. Earlier, the old M5 guard checked only a daily soft cap, `--hard`
skipped the size reroute, and payer=developer was logged without disabling anything.
