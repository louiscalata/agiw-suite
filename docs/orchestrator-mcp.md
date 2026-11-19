# agiw-orchestrator (MCP)

`agiw-orchestrator` is a small front-desk MCP server for the AGIW fleet. You talk to it from
ChatGPT. It reports status, drafts task briefs with a cheap Codex Free Pool model, and queues
briefs as files. It never runs code, pushes, merges or changes a machine.

## Tools

| Tool | What it does |
| --- | --- |
| `status` | Lane state, fleet workers (`/v1/ready` through the fleet router configs), the last 10 lane receipts, today's (UTC) call and token totals including any non-`openai` payer, and open PRs with a CI summary for Nisi, Jaryo, OPOS, AGIW Suite and AGIW MCP (read-only `gh pr list`). |
| `plan` | Turns a request (up to 4,000 characters) into a JSON task brief. |
| `queue_task` | Writes a brief to `~/pending-review/dot-orchestrator/inbox/<id>.json` (mode 0600). Queue only. |
| `task_result` | Lists recent tasks, or reads `results/<id>.json` or `results/<id>.md` for one id. |

## Model calls

- `plan` is the only tool that calls a model, and only through `free_pool_call.py`, so the budget
  ceiling, credit fail-closed and content gate always apply. Point the server at the script with
  `--free-pool-call` or `AGIW_FREE_POOL_CALL`.
- Allowed models: `terra` (gpt-5.6-terra, the default), `luna` (gpt-5.6-luna) and `mini`
  (gpt-5.4-mini). The output cap is 600 tokens. Any other model is refused before a process starts.
- Every attempt appends one line to `receipts/orchestrator-lane.jsonl` with `lane: dot-orchestrator`,
  the model, `response_id`, payer, usage and an outcome code. Prompt and output text are never stored.
- If a call reports a payer other than `openai`, the server writes `receipts/PAUSED` and `plan`
  refuses until you remove that file.
- Requests that look like workspace links, secrets, `.env` files or raw logs are refused before any
  model call.

## Run

The HTTP server listens on loopback only and requires a bearer token. The token is read from a
0600 file (default `~/.config/agiw/orchestrator-mcp.token`); its value is never printed or logged.

```sh
agiw-orchestrator --port 18795 --free-pool-call /path/to/free_pool_call.py \
  --fleet-config ~/.config/agiw/fleet-router-pc.json
# MCP endpoint: http://127.0.0.1:18795/mcp  (POST, Authorization: Bearer <token file contents>)
```

The free-pool script reads `OPENAI_API_KEY` from the server's environment. Start the server from a
launcher that sources the private env file; the key is passed only to that child process.

## ChatGPT

ChatGPT cannot reach a loopback server directly. Use OpenAI Secure MCP Tunnel:

1. In the OpenAI Platform tunnel settings, create a tunnel for the target ChatGPT workspace and
   download `tunnel-client`.
2. Run the server over stdio through the tunnel (stdio has no network surface, so no token is needed):

   ```sh
   tunnel-client init --sample sample_mcp_stdio_local --profile agiw-orchestrator \
     --tunnel-id tunnel_... \
     --mcp-command "python3 -m agiw.orchestrator_mcp --stdio --no-auth --free-pool-call /path/to/free_pool_call.py"
   tunnel-client run --profile agiw-orchestrator
   ```

   Or point `tunnel-client` at the HTTP server with `--mcp-server-url http://127.0.0.1:18795/mcp`.
3. In ChatGPT, open Plugins, select **+**, then **Add custom MCP server**, choose **Tunnel** under
   Connection, and select the tunnel.

Do not expose the HTTP port publicly. The server refuses to bind to a non-loopback address.
