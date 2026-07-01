"""Throwaway probe: dump every event/delta + final items to see where (if
anywhere) the dev server exposes tool *results*, not just tool *calls*.
"""
import json

import common

client = common.make_client(agent_mode=True)
MODEL = common.MODEL
TOOLS = [{"type": "code_execution"}]
SYS = "Always use the code_execution tool to compute, and show the result."
Q = "Use code to compute 2+2."

print("########## RESPONSES: all event types ##########")
stream = client.responses.create(
    model=MODEL, stream=True, instructions=SYS,
    input=[{"role": "user", "content": Q}], tools=TOOLS, tool_choice="auto",
)
final = None
for ev in stream:
    t = getattr(ev, "type", None)
    extra = ""
    if t and t.endswith(".delta"):
        d = getattr(ev, "delta", None)
        extra = f"  delta={str(d)[:60]!r}"
    if t == "response.output_item.done":
        it = ev.item
        keys = list(it.model_dump(exclude_none=True).keys())
        extra = f"  item.type={getattr(it,'type',None)}  keys={keys}"
    if t == "response.completed":
        final = ev.response
    print(t + extra)

print("\n########## RESPONSES: final output[] items (full) ##########")
for it in (final.output or []) if final else []:
    print(json.dumps(it.model_dump(exclude_none=True), ensure_ascii=False)[:600])

print("\n########## CHAT: every non-empty delta ##########")
stream = client.chat.completions.create(
    model=MODEL, stream=True, stream_options={"include_usage": True},
    messages=[{"role": "system", "content": SYS}, {"role": "user", "content": Q}],
    tools=TOOLS, tool_choice="auto",
)
for ch in stream:
    for c in ch.choices:
        d = c.delta
        if d is None:
            continue
        dd = d.model_dump(exclude_none=True)
        if dd:
            print(f"role={c.delta.role}  {json.dumps(dd, ensure_ascii=False)[:160]}")
