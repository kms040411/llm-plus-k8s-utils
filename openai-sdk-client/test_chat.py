#!/usr/bin/env python3
"""Standard OpenAI **Chat Completions** client (SDK-based), aimed at the dev server.

  Single-turn:  uv run test_chat.py "your prompt"
  Multi-turn:   uv run test_chat.py            (interactive REPL — /exit to quit)

This is the canonical way to use `client.chat.completions.create`: the SDK owns
the HTTP, auth, and SSE parsing; we just build `messages`, stream the deltas, and
reassemble the assistant message. Chat Completions is *stateless*, so the whole
`messages` array is resent every turn and the assistant reply (text + any tool
calls) is appended to keep multi-turn context.

Env knobs: MODEL, BASE_URL, API_KEY, STREAM, AGENT_MODE, TOOLS_JSON, TOOL_CHOICE,
           OUTPUT_DIR, OUTPUT_FILE
"""

from __future__ import annotations

import argparse
import json
import sys

import openai

import common

SYSTEM_PROMPT = common.load_system_prompt(tool="code_execution")

# Live config — populated from CLI args in main() (argparse), not env. The chat
# client defaults to agent mode ON (server-side code_execution, this dev server's
# real feature); use --no-agent for a plain standard client.
client = None
TOOLS: list = []
TOOL_CHOICE = "auto"
AGENT_MODE = True
RAW = False  # --raw: dump the unparsed HTTP/SSE response instead of the parsed view


def _extra(obj, key):
    """Read a non-standard streamed field (e.g. `reasoning`) that the typed SDK
    model doesn't declare. Pydantic keeps unknown fields in `model_extra`."""
    if obj is None:
        return None
    extra = getattr(obj, "model_extra", None) or {}
    for k in (key, f"{key}_content"):  # vLLM may use `reasoning` or `reasoning_content`
        val = getattr(obj, k, None) or extra.get(k)
        if val:
            return val
    return None


def _request_kwargs(messages: list) -> dict:
    kw = {"model": common.MODEL, "messages": messages}
    if TOOLS:
        kw["tools"] = TOOLS
        kw["tool_choice"] = TOOL_CHOICE
    return kw


def _print_request(kw: dict) -> None:
    shown = dict(kw, stream=common.STREAM)
    print("=== Request (client.chat.completions.create) ===")
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    print("\n=== Response ===")


def _dump_raw(kw: dict):
    """RAW=1: print the unparsed HTTP response (status line, headers, body) via
    the SDK's raw-response accessors. For streaming this is the literal SSE
    lines; history isn't reconstructed, so it returns None."""
    if common.STREAM:
        with client.chat.completions.with_streaming_response.create(
            stream=True, stream_options={"include_usage": True}, **kw
        ) as resp:
            sys.stdout.write(f"--- HTTP {resp.status_code} ---\n")
            for k, v in resp.headers.items():
                sys.stdout.write(f"{k}: {v}\n")
            sys.stdout.write("--- raw SSE ---\n")
            for line in resp.iter_lines():
                sys.stdout.write(line + "\n")
    else:
        resp = client.chat.completions.with_raw_response.create(**kw)
        sys.stdout.write(f"--- HTTP {resp.status_code} ---\n")
        for k, v in resp.headers.items():
            sys.stdout.write(f"{k}: {v}\n")
        sys.stdout.write("--- raw body ---\n" + resp.text + "\n")
    return None


def complete(messages: list):
    """One chat-completion call. Streams the reply and returns the assistant
    message dict to append to history (or None on error / empty reply)."""
    kw = _request_kwargs(messages)
    _print_request(kw)
    try:
        if RAW:
            return _dump_raw(kw)
        if common.STREAM:
            stream = client.chat.completions.create(
                stream=True, stream_options={"include_usage": True}, **kw
            )
            return _consume_stream(stream)
        return _consume_completion(client.chat.completions.create(**kw))
    except openai.APIStatusError as e:
        sys.stdout.write(f"--- HTTP {e.status_code} ---\n")
        sys.stdout.write((getattr(e.response, "text", "") or "").strip() + "\n")
        return None
    except openai.APIConnectionError as e:
        sys.stdout.write(f"--- connection error: {e} ---\n")
        return None


def _consume_stream(stream):
    sw = common.SectionWriter()
    content = ""
    tool_calls: dict[int, dict] = {}  # index -> {id, name, arguments}
    finish = None
    usage = None

    for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        for choice in chunk.choices:
            delta = choice.delta
            if delta is not None:
                if r := _extra(delta, "reasoning"):
                    sw.switch("reasoning")
                    sw.write(r)
                if delta.content:
                    sw.switch("content")
                    sw.write(delta.content)
                    content += delta.content
                for tc in delta.tool_calls or []:
                    idx = tc.index if tc.index is not None else 0
                    slot = tool_calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                            sw.switch(f"tool_call[{idx}] {tc.function.name}")
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
                            sw.switch(f"tool_call[{idx}] {slot['name'] or '?'}")
                            sw.write(tc.function.arguments)
            if choice.finish_reason:
                finish = choice.finish_reason

    if finish:
        sw.write(f"\n--- finish_reason: {finish} ---\n")
    if usage:
        sw.write(f"--- usage: {usage.model_dump_json()} ---\n")
    common.chatbot_view(content)
    return _assemble(content, tool_calls)


def _consume_completion(completion):
    msg = completion.choices[0].message
    if r := _extra(msg, "reasoning"):
        sys.stdout.write("--- reasoning ---\n" + r + "\n")
    if msg.content:
        sys.stdout.write("--- content ---\n" + msg.content + "\n")
    tool_calls: dict[int, dict] = {}
    for i, tc in enumerate(msg.tool_calls or []):
        sys.stdout.write(f"--- tool_call[{i}] {tc.function.name} ---\n{tc.function.arguments}\n")
        tool_calls[i] = {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
    if getattr(completion.choices[0], "finish_reason", None):
        sys.stdout.write(f"--- finish_reason: {completion.choices[0].finish_reason} ---\n")
    if completion.usage:
        sys.stdout.write(f"--- usage: {completion.usage.model_dump_json()} ---\n")
    common.chatbot_view(msg.content or "")
    return _assemble(msg.content or "", tool_calls)


def _assemble(content: str, tool_calls: dict):
    """Rebuild the assistant message in the standard Chat Completions shape so it
    can be appended to `messages` for the next turn."""
    msg = {"role": "assistant", "content": content}
    tcs = []
    for idx in sorted(tool_calls):
        s = tool_calls[idx]
        tcs.append(
            {
                "id": s["id"],
                "type": "function",
                "function": {"name": s["name"], "arguments": s["arguments"]},
            }
        )
    if tcs:
        msg["tool_calls"] = tcs
    if not content and not tcs:
        return None
    return msg


def main() -> int:
    global client, TOOLS, TOOL_CHOICE, AGENT_MODE, RAW

    p = argparse.ArgumentParser(
        description="Standard OpenAI Chat Completions test client (prunus dev server).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    common.add_common_args(p, default_agent=True)
    p.add_argument(
        "--raw", action="store_true",
        help="dump the unparsed HTTP response (status, headers, raw SSE/JSON body)",
    )
    args = p.parse_args()

    common.configure(args)
    AGENT_MODE = args.agent
    RAW = args.raw
    TOOL_CHOICE = args.tool_choice
    TOOLS = (
        json.loads(args.tools) if args.tools is not None
        else ([{"type": "code_execution"}] if AGENT_MODE else [])
    )
    client = common.make_client(AGENT_MODE)

    common.install_logfile("chat")
    print(f"[base_url: {common.BASE_URL}]  [model: {common.MODEL}]  [stream: {common.STREAM}]")
    print(f"[agent_mode: {AGENT_MODE}; tools: {len(TOOLS)}; tool_choice: {TOOL_CHOICE!r}; raw: {RAW}]\n")

    if args.prompt is not None:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": args.prompt},
        ]
        result = complete(messages)
        return 0 if (RAW or result is not None) else 1

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def on_turn(user_input: str) -> None:
        messages.append({"role": "user", "content": user_input})
        reply = complete(messages)
        if RAW:
            messages.pop()  # raw inspection mode: don't track history
            return
        if reply is not None:
            messages.append(reply)
            sys.stdout.write(f"\n[history: {len(messages)} messages]\n")
        else:
            messages.pop()  # don't drift into a stuck user,user,... sequence
            sys.stdout.write("\n[no reply — turn discarded]\n")

    banner = (
        "[interactive chat mode — /exit or Ctrl-D to quit]\n"
        f"[system prompt: {len(SYSTEM_PROMPT)} chars; agent_mode: {AGENT_MODE}; tools: {len(TOOLS)}]"
    )
    return common.repl(on_turn, banner)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
