#!/usr/bin/env python3
"""Standard OpenAI **Responses API** client (SDK-based), aimed at the dev server.

  Single-turn:  uv run test_response.py "your prompt"
  Multi-turn:   uv run test_response.py            (interactive REPL — /exit to quit)

Uses `client.responses.create`; the SDK delivers typed stream events and the
fully-assembled final response, so we don't hand-parse SSE. The system prompt is
passed via the standard `instructions` param (sent every turn).

Multi-turn has the two standard modes, selected by --store / --no-store:

  --store     (default) server-side state. Each turn sends only the new user
              input plus `previous_response_id`; the server holds the history.
  --no-store  stateless. The full `input` array is resent each turn and grown
              locally from each response's output items.

Config is via CLI flags (run `-h`): --model --base-url --api-key
--stream/--no-stream --agent/--no-agent --store/--no-store --tools --tool-choice.
(OUTPUT_DIR / OUTPUT_FILE are still read from env.)
"""

from __future__ import annotations

import argparse
import json
import sys

import openai

import common

SYSTEM_PROMPT = common.load_system_prompt(tool="code_interpreter")

# Live config — populated from CLI args in main() (argparse), NOT from env, so a
# bare `uv run test_response.py` is predictable with no hidden env state. The
# helpers below read these module globals.
client = None
TOOLS: list = []
TOOL_CHOICE = "auto"
AGENT_MODE = False
STORE = True  # server-side state (store + previous_response_id); --no-store disables


def _request_kwargs(input_items: list, previous_response_id: str | None) -> dict:
    kw = {
        "model": common.MODEL,
        "input": input_items,
        "instructions": SYSTEM_PROMPT,
        "store": STORE,
    }
    if previous_response_id:
        kw["previous_response_id"] = previous_response_id
    if TOOLS:
        kw["tools"] = TOOLS
        kw["tool_choice"] = TOOL_CHOICE
    return kw


def _print_request(kw: dict) -> None:
    shown = dict(kw, stream=common.STREAM)
    print("=== Request (client.responses.create) ===")
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    print("\n=== Response ===")


def respond(input_items: list, previous_response_id: str | None):
    """One Responses call. Streams output and returns
    (response_id, output_items, ok):
      response_id  - chains the next turn in store mode
      output_items - the model's output as input-shaped dicts (no-store history)
      ok           - True if the response completed without error
    """
    kw = _request_kwargs(input_items, previous_response_id)
    _print_request(kw)
    try:
        if common.STREAM:
            return _consume_stream(client.responses.create(stream=True, **kw))
        return _consume_response(client.responses.create(**kw))
    except openai.APIStatusError as e:
        sys.stdout.write(f"--- HTTP {e.status_code} ---\n")
        sys.stdout.write((getattr(e.response, "text", "") or "").strip() + "\n")
        return None, [], False
    except openai.APIConnectionError as e:
        sys.stdout.write(f"--- connection error: {e} ---\n")
        return None, [], False


def _consume_stream(stream):
    sw = common.SectionWriter()
    items: dict[int, object] = {}  # output_index -> item (for labels)
    final = None
    errors: list[str] = []
    seen_unknown: set[str] = set()

    def label(idx: int) -> str:
        it = items.get(idx)
        if it is None:
            return f"[{idx}]"
        t = getattr(it, "type", "?")
        name = getattr(it, "name", None) or getattr(it, "server_label", None)
        return f"[{idx}] {t}" + (f" {name}" if name else "")

    for event in stream:
        et = getattr(event, "type", None)

        if et == "response.completed":
            final = event.response
            continue
        if et in ("response.created", "response.in_progress", "response.queued"):
            continue
        if et == "error" or getattr(event, "error", None):
            errors.append(str(getattr(event, "error", event)))
            continue

        if et == "response.output_item.added":
            idx = getattr(event, "output_index", 0)
            items[idx] = event.item
            sw.switch(f"{label(idx)}  ← added")
            continue
        if et == "response.output_item.done":
            continue

        # Any "...delta" event carries a streamed chunk in .delta. The event name
        # (output_text / reasoning_summary_text / mcp_call_arguments / ...) is the
        # section label, so we don't special-case each one.
        delta = getattr(event, "delta", None)
        if et and et.endswith(".delta") and delta is not None:
            idx = getattr(event, "output_index", 0)
            kind = et[len("response."):-len(".delta")]
            sw.switch(f"{label(idx)} · {kind}")
            sw.write(delta if isinstance(delta, str) else json.dumps(delta, ensure_ascii=False))
            continue

        # Quietly skip the matching *.done / *.added housekeeping events.
        if et and (et.endswith(".done") or et.endswith(".added")):
            continue

        # Surface anything we don't recognize, once, so schema drift is visible.
        if et not in seen_unknown:
            seen_unknown.add(et)
            sw.switch(f"event:{et}")
            sample = {k: v for k, v in _as_dict(event).items() if k != "type"}
            preview = json.dumps(sample, ensure_ascii=False, default=str)
            sw.write((preview[:240] + "…") if len(preview) > 240 else preview)
            sw.write("\n")

    return _finish(final, errors)


def _consume_response(response):
    """Non-streaming: the SDK returns the whole Response object. Show reasoning
    first (the ordered recap below skips it), then let _finish print the ordered
    call -> result -> answer block."""
    for item in response.output or []:
        if getattr(item, "type", None) == "reasoning":
            for s in getattr(item, "summary", None) or []:
                sys.stdout.write("--- reasoning ---\n" + (getattr(s, "text", "") or "") + "\n")
    return _finish(response, [])


def _as_dict(obj) -> dict:
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    return getattr(obj, "__dict__", {}) or {}


def _finish(final, errors: list):
    items_out: list[dict] = []  # next-turn input: fully-assembled output items
    rid = getattr(final, "id", None) if final is not None else None

    # Ordered recap built from the final response's output[]. Walking it in
    # sequence prints each tool call, then its result, then the assistant answer
    # in the right order. The raw result lives only on the finalized item
    # (`output`) — never in the stream deltas — so this end block is the only
    # place where call -> result -> answer can be shown in order.
    output = (getattr(final, "output", None) or []) if final is not None else []
    if output:
        sys.stdout.write("\n" + common.color("=== result (ordered) ===\n", "1;36"))
    for item in output:
        t = getattr(item, "type", None)
        if t == "reasoning":
            continue  # shown live while streaming (or above, non-streaming)
        d = item.model_dump(exclude_none=True)
        if t == "message":
            text = "".join(
                p.get("text", "")
                for p in d.get("content", [])
                if p.get("type") in ("output_text", "text")
            ).strip()
            if not text:
                continue
            sys.stdout.write(common.color(f"assistant: {text}\n", "36"))
        else:
            name = d.get("name") or t
            if (args := d.get("arguments")) is not None:
                sys.stdout.write(f"tool call [{name}]: {args}\n")
            if (out := d.get("output")) is not None:
                sys.stdout.write(f"tool result [{name}]: {out}\n")
        items_out.append(d)

    if final is not None:
        if status := getattr(final, "status", None):
            sys.stdout.write(f"--- status: {status} ---\n")
        if (usage := getattr(final, "usage", None)) is not None:
            sys.stdout.write(f"--- usage: {usage.model_dump_json()} ---\n")
    if errors:
        sys.stdout.write("\n--- errors ---\n" + "\n".join(errors) + "\n")

    ok = final is not None and not errors
    return rid, items_out, ok


def main() -> int:
    global client, TOOLS, TOOL_CHOICE, AGENT_MODE, STORE

    p = argparse.ArgumentParser(
        description="Standard OpenAI Responses API test client (prunus dev server).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    common.add_common_args(p, default_agent=False)
    p.add_argument(
        "--store", action=argparse.BooleanOptionalAction, default=True,
        help="server-side state (store=true + previous_response_id chaining); "
             "--no-store resends the full input each turn",
    )
    args = p.parse_args()

    common.configure(args)
    AGENT_MODE = args.agent
    STORE = args.store
    TOOL_CHOICE = args.tool_choice
    TOOLS = (
        json.loads(args.tools) if args.tools is not None
        else ([{"type": "code_execution"}] if AGENT_MODE else [])
    )
    client = common.make_client(AGENT_MODE)

    common.install_logfile("response")
    mode = "store (previous_response_id)" if STORE else "no-store (local input array)"
    print(f"[base_url: {common.BASE_URL}]  [model: {common.MODEL}]  [stream: {common.STREAM}]")
    print(f"[agent_mode: {AGENT_MODE}; tools: {len(TOOLS)}; tool_choice: {TOOL_CHOICE!r}]  [multi-turn: {mode}]\n")

    if args.prompt is not None:
        _, _, ok = respond([{"role": "user", "content": args.prompt}], None)
        return 0 if ok else 1

    # multi-turn state (one of the two is used depending on STORE)
    state = {"prev": None, "input": []}

    def on_turn(user_input: str) -> None:
        user_item = {"role": "user", "content": user_input}
        if STORE:
            # Send only the new input; the server reconstructs the rest from
            # previous_response_id.
            rid, _, ok = respond([user_item], state["prev"])
            if ok:
                state["prev"] = rid
                sys.stdout.write(f"\n[store: previous_response_id -> {rid}]\n")
            else:
                sys.stdout.write("\n[no reply — previous_response_id unchanged]\n")
        else:
            # Stateless: resend the whole conversation, grown locally.
            state["input"].append(user_item)
            _, items_out, ok = respond(state["input"], None)
            if ok and items_out:
                state["input"].extend(items_out)
                sys.stdout.write(f"\n[no-store: input grown to {len(state['input'])} items]\n")
            else:
                state["input"].pop()
                sys.stdout.write("\n[no reply — turn discarded]\n")

    banner = (
        "[interactive responses mode — /exit or Ctrl-D to quit]\n"
        f"[instructions: {len(SYSTEM_PROMPT)} chars; agent_mode: {AGENT_MODE}; "
        f"tools: {len(TOOLS)}; multi-turn: {mode}]"
    )
    return common.repl(on_turn, banner)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
