"""Shared helpers for the SDK-based dev-server test clients.

`test_chat.py` and `test_response.py` point the official `openai` SDK at an
OpenAI-compatible endpoint (default: the prunus dev server) and stream the reply
with a readable, time-ordered layout. This module holds the parts that aren't
API-shape-specific: configuration, client construction, terminal+logfile
mirroring, a streaming section printer, and the REPL skeleton.

The goal is *standard* SDK usage — the request construction, auth, SSE transport
and retries are all handled by the SDK; only the interpretation of streamed
deltas (which is necessarily app-specific) lives here.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import pathlib
import re
import sys

from openai import OpenAI

# --- configuration ------------------------------------------------------------
# Connection settings resolve with precedence: CLI flag > env var > config.json
# > built-in fallback. The server address (base_url) and secret (api_key) live
# in config.json, which is git-ignored — this is a public repo, so nothing here
# may contain a real IP or key. Copy config.example.json to config.json and fill
# it in. base_url is the OpenAI-compatible *root*; the SDK appends
# /chat/completions or /responses itself.
CONFIG_PATH = pathlib.Path(
    os.environ.get("CONFIG_FILE", pathlib.Path(__file__).resolve().parent / "config.json")
)


def load_config() -> dict:
    """Read connection settings from the JSON config file. A missing file is
    fine (env vars / CLI flags / fallbacks still apply); a malformed one warns
    and is ignored rather than crashing the client."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"[warn] ignoring config {CONFIG_PATH}: {e}", file=sys.stderr)
        return {}


_CONFIG = load_config()


def _setting(env_key: str, cfg_key: str, fallback: str) -> str:
    """Resolve one setting: env var > config.json > fallback (empty = unset)."""
    return os.environ.get(env_key) or _CONFIG.get(cfg_key) or fallback


# Fallbacks are deliberately non-sensitive placeholders — real values come from
# config.json (git-ignored) or env vars, never from committed source.
BASE_URL = _setting("BASE_URL", "base_url", "http://localhost:8000/v1")
API_KEY = _setting("API_KEY", "api_key", "EMPTY")
MODEL = _setting("MODEL", "model", "prunus")
STREAM = True  # placeholder; overwritten by configure() from --stream/--no-stream


# The system prompt is non-sensitive, so it lives in a committed text file
# (system_prompt.txt), not in the git-ignored config.json. It's a template: each
# client fills {tool} with its API's code tool name.
SYSTEM_PROMPT_PATH = pathlib.Path(
    os.environ.get(
        "SYSTEM_PROMPT_FILE", pathlib.Path(__file__).resolve().parent / "system_prompt.txt"
    )
)


def load_system_prompt(tool: str) -> str:
    """Return the shared system prompt template with {tool} substituted
    ('code_execution' for chat, 'code_interpreter' for responses)."""
    try:
        text = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SystemExit(f"could not read system prompt {SYSTEM_PROMPT_PATH}: {e}")
    return text.replace("{tool}", tool)


# Agent mode re-enables the dev server's non-standard server-side tool execution:
# make_client(agent_mode=True) sends the X-Agent-Mode header and the scripts
# default their tools to {"type": "code_execution"} — the only path on this dev
# server that actually *runs* code server-side. Without it the endpoints behave
# like stock vLLM and reject code_execution. Each script picks its own default
# (test_chat: on, test_response: off); controlled by --agent / --no-agent.


def make_client(agent_mode: bool = False) -> OpenAI:
    """An OpenAI client aimed at BASE_URL.

    In standard mode this is exactly how you'd talk to api.openai.com — only
    base_url differs. With agent_mode it adds the X-Agent-Mode header that routes
    the dev server to its server-side tool-execution handler.
    """
    headers = {"X-Agent-Mode": "true"} if agent_mode else None
    return OpenAI(base_url=BASE_URL, api_key=API_KEY, default_headers=headers)


def add_common_args(parser: argparse.ArgumentParser, *, default_agent: bool) -> None:
    """Add the CLI arguments shared by both clients. Connection settings fall
    back to env vars as their defaults; behavior toggles use explicit hardcoded
    defaults so a bare run is predictable with no hidden env state."""
    parser.add_argument("prompt", nargs="?",
                        help="single prompt (one-shot); omit for the interactive REPL")
    parser.add_argument("--model", default=MODEL, help="model id")
    parser.add_argument("--base-url", default=BASE_URL, help="OpenAI-compatible root URL")
    parser.add_argument("--api-key", default=API_KEY, help="bearer token")
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=True,
                        help="stream the response over SSE")
    parser.add_argument("--agent", action=argparse.BooleanOptionalAction, default=default_agent,
                        help="send X-Agent-Mode and default tools to server-side code_execution")
    parser.add_argument("--tools", default=None,
                        help="JSON array of tools (overrides the mode default), e.g. '[]'")
    parser.add_argument("--tool-choice", default="auto", help="tool_choice")


def configure(args: argparse.Namespace) -> None:
    """Populate the live connection config (MODEL/STREAM/BASE_URL/API_KEY) from
    parsed CLI args so the rest of the module reads consistent values."""
    global MODEL, STREAM, BASE_URL, API_KEY
    MODEL = args.model
    STREAM = args.stream
    BASE_URL = args.base_url
    API_KEY = args.api_key


# --- color + terminal/logfile mirroring --------------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_USE_COLOR = (
    (
        (sys.__stdout__ is not None and sys.__stdout__.isatty())
        or os.environ.get("FORCE_COLOR") == "1"
    )
    and not os.environ.get("NO_COLOR")
)


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


class _Tee(io.TextIOBase):
    """Write to the terminal and a log file at once; strip ANSI for the file."""

    def __init__(self, term, file):
        self.term = term
        self.file = file

    def write(self, s):
        self.term.write(s)
        self.term.flush()
        self.file.write(_ANSI_RE.sub("", s))
        self.file.flush()
        return len(s)

    def flush(self):
        self.term.flush()
        self.file.flush()


def install_logfile(prefix: str) -> pathlib.Path:
    """Redirect stdout/stderr through a Tee into outputs/<prefix>-<ts>-<pid>.txt
    and return the path. Mirrors the original scripts' session logging."""
    here = pathlib.Path(__file__).resolve().parent
    out_dir = pathlib.Path(os.environ.get("OUTPUT_DIR", here / "outputs"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    default = out_dir / f"{prefix}-{ts}-{os.getpid()}.txt"
    path = pathlib.Path(os.environ.get("OUTPUT_FILE", str(default)))
    f = open(path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, f)
    sys.stderr = sys.stdout
    return path


# --- streaming display --------------------------------------------------------
class SectionWriter:
    """Print labelled, time-ordered sections. Call switch(label) when the kind
    of output changes (reasoning -> content -> tool call); write(text) the
    streamed chunk in between."""

    def __init__(self):
        self.section: str | None = None

    def switch(self, label: str) -> None:
        if label != self.section:
            if self.section is not None:
                sys.stdout.write("\n")
            sys.stdout.write(f"--- {label} ---\n")
            self.section = label
            sys.stdout.flush()

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()


def chatbot_view(text: str) -> None:
    """Print just the assistant text a chat UI would show."""
    text = (text or "").strip()
    if not text:
        return
    sys.stdout.write("\n")
    sys.stdout.write(color("=== assistant (chatbot view) ===\n", "1;36"))
    sys.stdout.write(color(text + "\n", "36"))


# --- REPL skeleton ------------------------------------------------------------
def repl(on_turn, banner: str) -> int:
    """Drive an interactive loop. `on_turn(user_text)` handles one turn and owns
    the conversation state (via closure). /exit, /quit, or EOF leaves."""
    sys.stdout.write(banner + "\n")
    turn = 0
    while True:
        try:
            # Prompt on the real terminal; Tee handles the mirrored output.
            sys.__stdout__.write("\n>>> user: ")
            sys.__stdout__.flush()
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            sys.stdout.write("\n[interrupted]\n")
            return 130
        if line == "":
            sys.stdout.write("\n[eof]\n")
            return 0
        user_input = line.rstrip("\n")
        if not user_input.strip():
            continue
        if user_input.strip().lower() in ("/exit", "/quit"):
            sys.stdout.write("[exit]\n")
            return 0
        turn += 1
        sys.stdout.write(f"\n=== turn {turn} — user ===\n{user_input}\n\n")
        on_turn(user_input)
