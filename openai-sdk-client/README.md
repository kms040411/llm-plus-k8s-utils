# sdk-client

Standard **OpenAI SDK** test clients for the `prunus` dev server, replacing the
hand-rolled `urllib` scripts (`test_chat.py`, `test_response.py`). Same two-mode
UX (single-turn arg / multi-turn REPL, session logging to `outputs/`), but the
request building, auth, SSE transport, retries, and event parsing are all done
by the official `openai` package — i.e. how you'd really talk to the API. Only
`base_url` differs from talking to `api.openai.com`.

- `test_chat.py` → `POST /v1/chat/completions` (`client.chat.completions.create`)
- `test_response.py` → `POST /v1/responses` (`client.responses.create`)
- `common.py` → shared config, client, logging, streaming printer, REPL

## Setup

```bash
cd openai-sdk-client
uv sync                              # installs openai (uv manages Python too)
cp config.example.json config.json   # then edit base_url / api_key
```

`config.json` holds the connection settings (`base_url`, `api_key`, `model`) plus
optional tool settings (`agent`, `tools`, `tool_choice`), and is **git-ignored** —
this is a public repo, so a real IP or key must never be committed. Values resolve
as **CLI flag > config.json > fallback** (connection settings also check env vars).

## Usage

```bash
# single-turn (prompt as positional arg)
uv run test_chat.py "Reply with one short sentence."
uv run test_response.py "Summarize what you can do in one line."

# multi-turn REPL (no prompt); /exit, /quit, or Ctrl-D to leave
uv run test_chat.py
uv run test_response.py

# options are CLI flags — list them all with -h
uv run test_response.py -h
uv run test_response.py --no-store --no-stream "..."
uv run test_chat.py --no-agent --raw "..."
```

Every session is mirrored to `outputs/<chat|response>-<timestamp>-<pid>.txt`
(ANSI stripped). The request body the SDK will send is printed before each call.

## Configuration (CLI flags)

Configured with argparse — run `-h` for the full list. No *behavior* is read from
env vars, so a stray `STORE=` / `AGENT_MODE=` in your shell can't change anything.

| Flag | Default | Meaning |
|------|---------|---------|
| `prompt` (positional) | — | single prompt; omit for the interactive REPL |
| `--model` | *(config.json, else `prunus`)* | model id |
| `--base-url` | *(config.json, else `http://localhost:8000/v1`)* | OpenAI-compatible root (SDK appends the path) |
| `--api-key` | *(config.json, else `EMPTY`)* | bearer token |
| `--stream` / `--no-stream` | stream | SSE streaming vs one-shot |
| `--agent` / `--no-agent` | *(config.json, else chat **on** / responses **off**)* | send `X-Agent-Mode` + default tools to server-side `code_execution` (see below) |
| `--tools` | *(config.json, else mode default)* | JSON array overriding the tools, e.g. `'[]'` |
| `--tool-choice` | *(config.json, else `auto`)* | tool_choice |
| `--store` / `--no-store` | store | *(test_response.py)* server-side state via `previous_response_id` vs stateless local input |
| `--raw` | off | *(test_chat.py)* dump the unparsed HTTP response (see below) |

`--model` / `--base-url` / `--api-key` resolve as **CLI flag > `MODEL` / `BASE_URL`
/ `API_KEY` env var > `config.json` > built-in fallback**. The config file is
`config.json` beside the scripts (override the path with `CONFIG_FILE`); copy it
from `config.example.json`. Logs still honor `OUTPUT_DIR` / `OUTPUT_FILE`.

The tool settings `--agent` / `--tools` / `--tool-choice` resolve as **CLI flag >
`config.json` > built-in default** (no env var). In `config.json`, `agent` is a
bool, `tools` a JSON array of tool objects (e.g. `[{"type": "web_search"}, {"type":
"web_fetch"}]`; bare strings are accepted as shorthand, so `["web_search"]` ==
`[{"type": "web_search"}]`), and `tool_choice` a string; a `null` or omitted value
means "unset — use the default". So, for
example, `"tools": []` there disables the default `code_execution` tool for every
run, and `"tool_choice": "required"` forces a tool call.

## System prompt

The system prompt is **not** secret, so — unlike `config.json` — it lives in a
committed, editable file: `system_prompt.txt`. It's a template; each client
substitutes `{tool}` with its API's code tool (`test_chat.py` → `code_execution`,
`test_response.py` → `code_interpreter`). Override the path with `SYSTEM_PROMPT_FILE`.

## Seeing the raw response (test_chat.py)

```bash
# 1) built-in --raw flag — status line + headers + the literal `data:` SSE frames
uv run test_chat.py --raw "hi"
uv run test_chat.py --raw --no-stream "hi"   # raw JSON body instead of SSE

# 2) SDK HTTP-level logging — no flag, works for any call
OPENAI_LOG=debug uv run test_chat.py "hi"
```

`--raw` uses the SDK's raw-response accessors: `with_streaming_response.create(...)`
→ `resp.iter_lines()` for the SSE frames, and `with_raw_response.create(...)`
→ `resp.text` / `resp.headers` for the one-shot body.

## Two modes, and what the dev server actually does

This dev server (vLLM under the hood) exposes **two** behaviors, and the choice
decides whether code execution works at all. Verified against the live server:

### Standard mode (`--no-agent` — default for `test_response.py`)

Plain OpenAI usage, no custom headers. The endpoints validate `tools` against
the standard OpenAI tool union.

- ✅ Plain chat / responses stream fine (reasoning + text + usage).
- ❌ `{"type":"code_execution"}` is **rejected** (`400`) — it isn't a standard tool.
- ⚠️ `{"type":"code_interpreter","container":{"type":"auto"}}` **validates** and the
  model emits the code call, but this dev server does **not** execute it
  server-side — the response just completes with the un-run call. Try it with:
  ```bash
  uv run test_response.py --tools '[{"type":"code_interpreter","container":{"type":"auto"}}]' "compute 2+2 with code"
  ```
- Default tools in this mode: **none** (inject standard function tools via `--tools` to exercise tool-calling).

### Agent mode (`--agent` — default for `test_chat.py`)

Sends `X-Agent-Mode: true`, which routes to the dev server's custom handler that
**runs code server-side** — the path the old scripts relied on. Tools default to
`[{"type":"code_execution"}]`. `test_chat.py` runs here by default (use
`--no-agent` for a plain standard chat client).

```bash
uv run test_chat.py "Use code to compute 2+2, then answer in one sentence."          # agent by default
uv run test_response.py --agent "Use code to compute 2+2, then answer in one sentence."
```

Here the server executes the code and the model returns a real answer. In chat
this shows as a `code_execution` tool call; in responses as a
`code_execution_call` output item (the generic event handler labels whatever the
server sends, so no per-tool code is needed).

> Note: `AGENT_MODE`/`code_execution` is **not** part of the OpenAI standard — it's
> this server's extension.

## Responses multi-turn: `--store` vs `--no-store`

Both modes work (the dev server now persists responses):

- `--store` (default): **server-side state** — sends `store: true` and chains
  `previous_response_id`, sending only the new user input each turn; the server
  reconstructs the history. ✅ Verified: turn 2 recalls earlier context
  ("remember 42" → "42").
- `--no-store`: **stateless** — the full `input` array is resent each turn and
  grown locally from each response's `output` items. Best for inspecting the exact
  request sent each turn.

## Known dev-server quirks (verified live, not client bugs)

The client is standard; these are things the dev server does that you may hit:

1. **`code_execution` needs agent mode** — in standard mode both endpoints reject
   it (`400`); only `X-Agent-Mode` (`--agent`) makes it work.
2. **`code_interpreter` isn't executed** — it validates and the model emits the
   code call, but the server completes the response without running it.
3. **Non-stream chat completions `400`** — the server injects
   `stream_options:{include_usage:true}` server-side, then rejects it for not
   having `stream=true`. Reproduces with raw `curl` sending only `{model, messages}`,
   so it's a server bug. Workaround: keep streaming (the default; only
   `--no-stream` triggers it). Non-stream **responses** works fine.

(Previously `previous_response_id` wasn't persisted — `STORE=1` 404'd on turn 2.
That's now fixed: the server persists responses and chaining works.)

## Tool *calls* and *results*

Tool **calls** (the code/arguments) stream live. Tool **results** (stdout) are
delivered differently per API:

- **Responses**: the result is on the finalized `code_execution_call` item's
  `output` field (e.g. `{"stdout":"4\n",...}`) — present only in the final
  `response.completed`, **not** in the stream deltas. So `test_response.py` prints
  an ordered `=== result (ordered) ===` recap at the end where each tool call is
  followed by its result and then the answer (handles multiple calls in order):
  ```
  === result (ordered) ===
  tool call [code_execution]: {"code": "print(2+2)", "lang": "python"}
  tool result [code_execution]: {"stdout":"4\n","stderr":"","exit_code":0}
  assistant: 2+2 equals 4.
  ```
- **Chat Completions**: the server emits no structured tool result; the outcome
  is only narrated inside the model's `reasoning_content` (chat-completions has no
  slot for server-side tool results).

See `_probe.py` for a raw dump of every event/delta.

## Scope

Deliberately small — enough to test the server, not a full client. Notably
omitted: client-side function-call execution loops, image/file/audio inputs,
attachments, and structured-output helpers. Add as needed.
