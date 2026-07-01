"""Throwaway: dump the server's *metadata* — HTTP headers, the response object's
non-content fields, per-item metadata, and the full usage breakdown."""
import json

import common

client = common.make_client()
MODEL = common.MODEL
J = lambda o: json.dumps(o, ensure_ascii=False, indent=2, default=str)

print("########## RESPONSES API ##########")
raw = client.responses.with_raw_response.create(
    model=MODEL, input=[{"role": "user", "content": "Say hi in one short word."}], store=True,
)
print("=== HTTP response headers ===")
for k, v in raw.headers.items():
    print(f"  {k}: {v}")
d = raw.parse().model_dump()
print("\n=== Response object metadata (every top-level field except output[]) ===")
print(J({k: v for k, v in d.items() if k != "output"}))
print("\n=== output[] items — metadata only (no content) ===")
for it in d.get("output") or []:
    print("  " + J({k: it.get(k) for k in ("id", "type", "role", "status", "name") if k in it}).replace("\n", " "))

print("\n########## CHAT COMPLETIONS (streaming) ##########")
stream = client.chat.completions.create(
    model=MODEL, messages=[{"role": "user", "content": "hi"}],
    stream=True, stream_options={"include_usage": True},
)
first = usage = None
for ch in stream:
    cd = ch.model_dump()
    if first is None:
        first = {k: v for k, v in cd.items() if k != "choices"}
    if cd.get("usage"):
        usage = cd["usage"]
print("=== first chunk metadata (minus choices) ===")
print(J(first))
print("=== usage (final chunk) ===")
print(J(usage))
