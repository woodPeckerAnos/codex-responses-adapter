# Codex Responses Adapter

A small local HTTP service that exposes the **OpenAI Responses API** (`POST /v1/responses`)
on top of the **Codex CLI**, called through the [`acodex`](https://docs.acodex.dev/)
Python SDK.

It is intended for agent frameworks that can use a Responses-compatible model provider.
The adapter is text-first and deliberately narrow: it converts text input into a Codex turn
and text output back into Responses objects and SSE events. Codex's own tool, shell, and
reasoning events are filtered out.

```text
OpenAI SDK / agent framework
        │  POST /v1/responses (JSON or SSE)
        ▼
FastAPI adapter (this project)
        │  acodex AsyncCodex / AsyncThread.run_streamed()
        ▼
codex exec --experimental-json
```

## Important: pin acodex 0.117.0

The PyPI project named `acodex` changed direction at version 1.0.0. The current
`acodex 1.x` package is a different project for automating the Codex desktop app and
does **not** expose the `Codex.start_thread()` / `Thread.run_streamed()` API used here.

This project therefore pins:

```text
acodex==0.117.0
```

Do not replace it with an unpinned `pip install acodex`, and do not upgrade to 1.x without
rewriting the backend. The 0.117.0 release still exposes the typed SDK surface this adapter
uses.

## Requirements

- Python 3.11 or newer. The repository uses [`uv`](https://docs.astral.sh/uv/) for
  reproducible environments.
- The Codex CLI installed and available as `codex` on `PATH` (or configured through
  `CRA_CODEX_PATH`).
- A Codex login (`codex login`). The adapter starts `codex exec` per turn; you do **not**
  need to keep an interactive Codex session running.
- No OpenAI API key is needed by the adapter itself. Codex uses its own configured
  credentials.

Example prerequisite install (macOS/Linux):

```bash
# Install uv, then the Codex CLI
curl -LsSf https://astral.sh/uv/install.sh | sh
npm install -g @openai/codex
```

Check the prerequisites:

```bash
codex --version
codex login status
```

If `codex login status` is not authenticated, run:

```bash
codex login
```

## Install and start

```bash
cd codex-responses-adapter
uv sync --dev
uv run codex-responses-adapter
```

The default bind address is `127.0.0.1:8000`. Equivalent Uvicorn command:

```bash
uv run uvicorn codex_responses_adapter.app:app --host 127.0.0.1 --port 8000
```

Copy `.env.example` to `.env` if you want persistent local configuration:

```bash
cp .env.example .env
```

Health check:

```bash
curl -sS http://127.0.0.1:8000/health | jq
```

Example response:

```json
{
  "status": "ok",
  "service": "codex-responses-adapter",
  "version": "0.1.0",
  "codex": {
    "available": true,
    "path": "/usr/local/bin/codex"
  },
  "stored_responses": 0,
  "concurrency_limit": 2,
  "auth_required": false
}
```

`status` is `degraded` if the executable cannot be found, but the HTTP server still starts.
The health endpoint does not verify the Codex login.

## Configuration

All settings use the `CRA_` environment prefix and may be placed in `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `CRA_HOST` | `127.0.0.1` | Bind host |
| `CRA_PORT` | `8000` | Bind port |
| `CRA_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `CRA_CODEX_PATH` | unset | Explicit path to the `codex` executable |
| `CRA_CODEX_MODEL` | unset | Model passed to Codex; otherwise Codex config decides |
| `CRA_CODEX_WORKDIR` | process cwd | Working directory passed to Codex |
| `CRA_CODEX_SANDBOX` | `read-only` | `read-only`, `workspace-write`, or `danger-full-access` |
| `CRA_CODEX_APPROVAL_POLICY` | `never` | Codex approval policy; `never` avoids interactive hangs |
| `CRA_WEB_SEARCH` | `false` | Set `true` for Codex `web_search=live`; false is `disabled` |
| `CRA_SKIP_GIT_CHECK` | `true` | Allow Codex to run outside a Git repository |
| `CRA_REQUEST_TIMEOUT` | `300` | Per-turn timeout in seconds |
| `CRA_QUEUE_TIMEOUT` | `30` | Maximum wait for a concurrency slot |
| `CRA_MAX_CONCURRENCY` | `2` | Maximum simultaneously running Codex turns |
| `CRA_RESPONSE_TTL` | `86400` | Lifetime of in-memory `previous_response_id` mappings |
| `CRA_MAX_RESPONSE_RECORDS` | `1000` | LRU cap for response mappings |
| `CRA_STORE_BY_DEFAULT` | `true` | Default value of request `store` |
| `CRA_API_KEY` | unset | Optional static Bearer token |
| `CRA_LOG_REQUESTS` | `true` | Log method, path, status, duration, request ID |
| `CRA_LOG_PROMPTS` | `false` | Reserved; prompt content is not logged by this adapter |
| `CRA_DEFAULT_MODEL` | `codex-cli` | Response `model` when the request omits it |

## Non-streaming request

```bash
curl -sS http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "codex-cli",
    "input": "Explain the difference between a process and a thread in one sentence."
  }' | jq
```

The response follows the Responses object shape:

```json
{
  "id": "resp_...",
  "object": "response",
  "created_at": 1760000000,
  "completed_at": 1760000003,
  "status": "completed",
  "model": "codex-cli",
  "output": [
    {
      "id": "msg_...",
      "type": "message",
      "status": "completed",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "A process has its own address space, while threads share ...",
          "annotations": [],
          "logprobs": []
        }
      ]
    }
  ],
  "usage": {
    "input_tokens": 123,
    "input_tokens_details": {
      "cached_tokens": 0,
      "cache_write_tokens": 0
    },
    "output_tokens": 45,
    "output_tokens_details": {
      "reasoning_tokens": 0
    },
    "total_tokens": 168
  },
  "metadata": {},
  "store": true,
  "previous_response_id": null
}
```

Extract just the text:

```bash
curl -sS http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"codex-cli","input":"Say hello."}' \
  | jq -r '.output[0].content[0].text'
```

## Streaming SSE request

```bash
curl -N -sS http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "codex-cli",
    "input": "Write three short bullet points about HTTP SSE.",
    "stream": true
  }'
```

Expected event sequence:

```text
event: response.created
data: {"type":"response.created","sequence_number":1,...}

event: response.in_progress
data: {"type":"response.in_progress","sequence_number":2,...}

event: response.output_item.added
data: {"type":"response.output_item.added","sequence_number":3,...}

event: response.content_part.added
data: {"type":"response.content_part.added","sequence_number":4,...}

event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":5,...}

event: response.output_text.done
data: {"type":"response.output_text.done","sequence_number":6,...}

event: response.content_part.done
data: {"type":"response.content_part.done","sequence_number":7,...}

event: response.output_item.done
data: {"type":"response.output_item.done","sequence_number":8,...}

event: response.completed
data: {"type":"response.completed","sequence_number":9,...}
```

`sequence_number` is global and strictly increasing. The stream ends with
`response.completed`; it does **not** send `data: [DONE]`.

The `response.output_text.delta` payload contains the complete text of one Codex
`agent_message`. acodex's Codex event stream reports completed messages rather than
token-level pieces, so this adapter cannot honestly emit token-by-token deltas.

On a failure after streaming has begun, the stream emits:

```text
event: error
data: {"type":"error","code":"codex_timeout","message":"...","param":null,"sequence_number":10}
```

and closes without `response.completed`. HTTP status remains 200 after SSE headers are sent.

## Multi-turn with `previous_response_id`

The adapter maintains a process-local mapping from its `resp_...` ID to the Codex
`thread_id` captured from `thread.started`. The next request resumes that Codex thread.

```bash
FIRST=$(curl -sS http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "codex-cli",
    "input": "Remember the codeword: ORANGE-42."
  }' | jq -r '.id')

echo "first response: $FIRST"

curl -sS http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d "{
    \"model\": \"codex-cli\",
    \"input\": \"What was the codeword?\",
    \"previous_response_id\": \"$FIRST\"
  }" | jq -r '.output[0].content[0].text'
```

This also works with `stream: true`. The mapping is written before `response.completed`
is sent, so a client can safely issue the next request after receiving that event.

If `store` is `false` the response is still returned, but its ID is not retained and cannot
be used with `previous_response_id`:

```json
{"model":"codex-cli","input":"One-off question","store":false}
```

The mapping lives in memory only. Restarting the adapter, running multiple Uvicorn workers,
or waiting past `CRA_RESPONSE_TTL` makes old response IDs unavailable. Codex may still have
the underlying session on disk, but the adapter no longer knows which thread it maps to.

## OpenAI Python SDK example

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="unused-unless-CRA_API_KEY-is-set",
)

response = client.responses.create(
    model="codex-cli",
    input="Summarize the purpose of this adapter.",
)
print(response.output_text)
```

For streaming:

```python
with client.responses.stream(
    model="codex-cli",
    input="Count from one to three.",
) as stream:
    for event in stream:
        if event.type == "response.output_text.delta":
            print(event.delta, end="", flush=True)
```

If `CRA_API_KEY` is set, pass that value as the SDK's `api_key`.

## Bearer authentication

Set `CRA_API_KEY` to require a static token on `/v1/responses`:

```bash
export CRA_API_KEY=local-secret
uv run codex-responses-adapter
```

```bash
curl -sS http://127.0.0.1:8000/v1/responses \
  -H 'Authorization: Bearer local-secret' \
  -H 'Content-Type: application/json' \
  -d '{"model":"codex-cli","input":"Hello"}'
```

Missing or invalid credentials produce HTTP 401 with an OpenAI-style error envelope and
`WWW-Authenticate: Bearer`. `/health` remains public. This is a single static token, not a
user database, quota system, or OAuth server.

## Compatibility boundaries

### Supported

- `POST /v1/responses`
- `stream: false` and `stream: true`
- Text `input` as a string
- Text message arrays with `user`, `assistant`, `system`, and `developer` roles
- `input_text` / `output_text` content parts
- `instructions` as a string (flattened into the Codex prompt; arrays are rejected)
- `previous_response_id` continuation
- Optional `store: false`
- `metadata`, `user`, `temperature`, `top_p`, and `max_output_tokens` accepted for wire
  compatibility
- Optional Bearer auth and `/health`
- Responses `error` events and OpenAI-style JSON errors
- Usage from Codex `turn.completed`

### Not supported

- Function/custom tools and tool-call output. Requests with a non-empty `tools` array are
  rejected with HTTP 400 instead of silently pretending tools work.
- `instructions` arrays. The adapter accepts only a string form.
- Images and files in input. `input_image` / `input_file` parts are rejected.
- Structured output / JSON schema response formats. Only `text.format.type = "text"` is
  accepted.
- Background responses and conversations.
- Response retrieval (`GET /v1/responses/{id}`), deletion, cancellation endpoints, and
  `/v1/models`.
- Token-level streaming. One Codex `agent_message` becomes one `output_text.delta`.
- Multiple adapter workers sharing `previous_response_id` state.
- Passing through Codex reasoning, command execution, file changes, MCP calls, web search
  items, todo lists, or error items as Responses output items.

### Semantics and assumptions

- **`model` is informational.** The request `model` is echoed in the response so SDK
  clients see the model they asked for, but it does not select the Codex model. Set
  `CRA_CODEX_MODEL` (or Codex's own config) to change the actual model. This avoids sending
  arbitrary OpenAI model IDs to the local Codex CLI.
- **`instructions` is prompt text.** acodex 0.117 has no separate Responses `instructions`
  parameter, so instructions are prepended inside `<instructions>` tags. Codex therefore
  sees them as part of the prompt, not as a native system/developer message. A resumed
  Codex thread may retain earlier prompt context; prior instructions are not automatically
  removed. When using `previous_response_id`, send only the new turn's input rather than
  resending the whole transcript, otherwise the model may see duplicate context.
- **Sampling controls are not enforced.** `temperature`, `top_p`, and `max_output_tokens`
  are accepted and echoed but are not forwarded to Codex or used to limit output.
- **Usage details are partial.** `input_tokens`, `cached_input_tokens`, and `output_tokens`
  come from acodex. acodex 0.117 does not expose cache-write or reasoning-output token
  counts, so those detail fields are reported as `0`. They are not invented measurements.
- **`store=false` is adapter-local.** It disables the in-memory `previous_response_id`
  mapping. Codex CLI may still persist its own session files under `~/.codex/sessions`.
- **Non-text items are filtered.** Codex may still run read-only commands as part of a turn
  depending on its configuration; filtering affects the Responses output, not Codex's
  internal behavior. The default sandbox is `read-only`, approval policy is `never`, and
  web search is disabled. Changing `CRA_CODEX_SANDBOX` to `workspace-write` or
  `danger-full-access` gives Codex real side effects; do that deliberately.
- **One worker only.** The concurrency limiter and response mapping are process-local.
  Running multiple Uvicorn workers would create independent maps and independent limits.
- **No retries.** A failed Codex turn is reported; the adapter does not silently retry and
  risk duplicating side effects.

## Error behavior

Pre-stream failures return normal JSON:

```json
{
  "error": {
    "message": "Codex CLI is not authenticated. Run `codex login` and retry. ...",
    "type": "server_error",
    "param": null,
    "code": "codex_not_authenticated"
  }
}
```

Representative status codes:

| Condition | Status | Code |
|---|---|---|
| Invalid request / unsupported input | 400 | `invalid_request` / `unsupported_parameter` |
| Missing `previous_response_id` | 400 | `previous_response_not_found` |
| Bad/missing Bearer token | 401 | `invalid_api_key` |
| Same Codex thread already running | 409 | `thread_busy` |
| Concurrency queue timeout | 429 | `concurrency_limit_exceeded` |
| Codex executable missing | 503 | `codex_unavailable` |
| Codex not logged in | 503 | `codex_not_authenticated` |
| Codex nonzero exit | 502 | `codex_exec_failed` |
| Codex `turn.failed` / `error` event | 502 | `codex_turn_failed` |
| Turn timeout | 504 | `codex_timeout` |
| Malformed event / missing `turn.completed` | 502 | `codex_protocol_error` |

After an SSE stream has started, the equivalent failure is an `error` event and HTTP 200.

Codex stderr is truncated before being placed in an error message; the adapter never returns
the full process environment or command line.

## Testing

```bash
uv sync --dev
uv run pytest -q
```

Most tests use an in-memory fake backend. One additional test writes a temporary fake
`codex` executable and drives it through the real acodex subprocess layer, validating
`thread.started`, `resume`, and JSONL parsing without needing a Codex login. The suite
covers request parsing, event mapping, SSE sequence numbers, usage, multi-turn mapping,
`store=false`, auth, timeout, unsupported input, and error responses.

Optional lint:

```bash
uv run ruff check .
```

## Troubleshooting

### `codex` not found

```text
codex_unavailable
```

Install the Codex CLI, ensure `codex --version` works in the same shell, or set an absolute
`CRA_CODEX_PATH`. `uv run` inherits the shell environment, but a launchd/systemd service may
have a different `PATH`.

### Not logged in

```bash
codex login status
codex login
```

Restart the adapter after changing credentials if your Codex setup requires it. The adapter
does not manage Codex tokens.

### Every request times out

- Increase `CRA_REQUEST_TIMEOUT`.
- Confirm `codex exec` itself works with a short prompt.
- Check whether a sandbox/approval policy is blocking Codex's app-server or file access.
- Your Codex account/model may be rate-limited or unavailable.

### `Operation not permitted` from Codex

Some restricted hosts/containers block Codex's in-process app-server or its access to
`~/.codex`. Run the adapter in a normal terminal or adjust the environment. This is an
environment/permissions issue, not an HTTP protocol issue.

### `previous_response_id` no longer works

The mapping is in-memory and TTL-limited. It is lost on restart and not shared between
workers. It may also have expired, or the response may have been created with `store=false`.
Start a fresh turn without `previous_response_id` to recover.

### Streaming appears buffered

The adapter sets `Cache-Control: no-cache` and `X-Accel-Buffering: no`. If a reverse proxy is
in front of it, disable proxy buffering for `/v1/responses` as well. Do not add gzip
middleware to the adapter.

## Security

- The default bind address is loopback (`127.0.0.1`). Do not expose the service publicly
  without authentication and network controls.
- `CRA_API_KEY` is a single static token. Store it in the environment or `.env`, not in
  source control.
- Prompts and model output are sent to the Codex/OpenAI account configured for the local
  Codex CLI. Review that data flow before using sensitive material.
- Codex sessions may be persisted by the CLI under `~/.codex/sessions`.
- Keep `CRA_CODEX_SANDBOX=read-only` unless you explicitly want Codex to modify files.

## Design notes and references

The adapter was built against:

- [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
- [Responses streaming events](https://platform.openai.com/docs/api-reference/responses-streaming)
- [OpenAI OpenAPI schema](https://github.com/openai/openai-openapi)
- [acodex core API](https://docs.acodex.dev/reference/core.html)
- [acodex streaming guide](https://docs.acodex.dev/guides/streaming.html)
- [acodex threads and sessions](https://docs.acodex.dev/guides/threads-and-sessions.html)
- [acodex 0.117.0 on PyPI](https://pypi.org/project/acodex/0.117.0/)
- [Codex CLI](https://github.com/openai/codex)

Notable implementation facts:

- `acodex==0.117.0` is pinned because `acodex 1.x` is a different project.
- `AsyncThread.run_streamed()` is used; the adapter never starts an interactive Codex client.
- The Codex `thread_id` is captured from `thread.started`.
- The adapter response ID (`resp_...`) is independent from the Codex thread ID.
- `turn.completed` produces `response.completed`; no `[DONE]` marker is sent.
- If the Codex CLI is missing or fails, the server remains up and returns a structured error.
