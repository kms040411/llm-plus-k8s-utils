"""Shared plumbing for the S3-compatible object-store explorer.

`explore.py` is a small, **read-only** interactive inspector for S3-compatible
object stores (MinIO / Ceph / self-hosted, and real AWS S3). This module holds
the parts that aren't command-specific: JSON config loading, boto3 client
construction (with the botocore knobs that matter for non-AWS servers), a few
formatters, terminal+logfile mirroring, and the REPL skeleton.

The client-construction defaults here (path-style addressing, `when_required`
checksums, bounded retries/timeouts) are tuned so a plain run works against
self-hosted servers without surprises; every one is overridable from config.json.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import sys
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
import botocore.exceptions as bexc
from botocore.config import Config as BotoConfig


# --- configuration ------------------------------------------------------------
def resolve_config_path(explicit: str | None) -> pathlib.Path:
    """Config path resolution: --config flag → $S3_EXPLORER_CONFIG → ./config.json
    next to this script. Mirrors the sibling's env-fallback ethos (path only)."""
    if explicit:
        return pathlib.Path(explicit).expanduser()
    env = os.environ.get("S3_EXPLORER_CONFIG")
    if env:
        return pathlib.Path(env).expanduser()
    return pathlib.Path(__file__).resolve().parent / "config.json"


def _die(msg: str) -> None:
    sys.stderr.write(color(msg + "\n", "31"))
    raise SystemExit(2)


def load_config(explicit_path: str | None = None) -> dict:
    """Read the JSON config, with friendly errors that point at the example."""
    path = resolve_config_path(explicit_path)
    if not path.exists():
        _die(
            f"config file not found: {path}\n"
            "  copy config.example.json to config.json and fill in your endpoint/keys,\n"
            "  or pass --config <path> / set $S3_EXPLORER_CONFIG."
        )
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(f"invalid JSON in {path}: {e}\n  see config.example.json for the expected shape.")
    if not isinstance(cfg, dict):
        _die(f"config in {path} must be a JSON object, got {type(cfg).__name__}.")
    return cfg


def make_client(cfg: dict):
    """Build a boto3 S3 client from config. This is the correctness hot-spot for
    S3-compatible servers — the botocore Config defaults below (checksums,
    addressing style, timeouts, retries) are what make non-AWS endpoints behave."""
    # verify: True/False, or a CA-bundle path when both verify_ssl and ca_bundle set.
    verify = cfg.get("verify_ssl", True)
    ca = cfg.get("ca_bundle")
    if verify and ca:
        verify = ca
    if verify is False:
        # Quiet the per-request "InsecureRequestWarning" spam when TLS verify is off.
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    boto_config = BotoConfig(
        signature_version=cfg.get("signature_version", "s3v4"),
        s3={"addressing_style": cfg.get("addressing_style", "auto")},
        retries={"max_attempts": int(cfg.get("max_attempts", 3)), "mode": "standard"},
        connect_timeout=cfg.get("connect_timeout", 10),
        read_timeout=cfg.get("read_timeout", 60),
        # The big one: botocore's default (when_supported) adds a CRC32 that many
        # S3-compatible servers reject/mishandle, causing opaque 400s and failed
        # downloads. when_required only sends/validates checksums when the API
        # actually needs them.
        request_checksum_calculation=cfg.get("request_checksum_calculation", "when_required"),
        response_checksum_validation=cfg.get("response_checksum_validation", "when_required"),
    )

    session = boto3.session.Session(profile_name=cfg.get("profile") or None)

    kwargs: dict = {
        "config": boto_config,
        "region_name": cfg.get("region", "us-east-1"),
        "verify": verify,
    }
    # Only pass endpoint_url when set — absent means real AWS S3.
    if cfg.get("endpoint_url"):
        kwargs["endpoint_url"] = cfg["endpoint_url"]
    # Only pass inline creds when present — otherwise botocore's credential chain
    # (env vars, ~/.aws/credentials, the named `profile`, or an IAM role) resolves them.
    if cfg.get("access_key_id") and cfg.get("secret_access_key"):
        kwargs["aws_access_key_id"] = cfg["access_key_id"]
        kwargs["aws_secret_access_key"] = cfg["secret_access_key"]
        if cfg.get("session_token"):
            kwargs["aws_session_token"] = cfg["session_token"]

    return session.client("s3", **kwargs)


# --- formatters ---------------------------------------------------------------
def human_size(n) -> str:
    """Bytes → human-readable (KiB/MiB/…)."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024 or unit == "PiB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


# Display timezone for rendered S3 timestamps. Defaults to KST; override with the
# `display_timezone` config key (any IANA name). Centralized here so every display
# site (listing rows, `stat`, `buckets`) — and the date filters, which compute
# "today" in this same zone — stays consistent.
_KST_FALLBACK = timezone(timedelta(hours=9), "KST")  # only used if the system has no tz database at all
try:
    _DISPLAY_TZ = ZoneInfo("Asia/Seoul")
except Exception:
    _DISPLAY_TZ = _KST_FALLBACK


def display_tz():
    """The tzinfo currently used to render S3 timestamps (also used for date-filter math)."""
    return _DISPLAY_TZ


def set_display_timezone(name: str | None) -> None:
    """Apply the configured display timezone (an IANA name like 'Asia/Seoul' or
    'UTC'). Empty/None keeps the default (KST); an unknown name warns and keeps the
    current zone rather than crashing."""
    global _DISPLAY_TZ
    if not name:
        return
    try:
        _DISPLAY_TZ = ZoneInfo(name)
    except (KeyError, ValueError):
        sys.stderr.write(color(f"unknown display_timezone {name!r} — keeping current zone\n", "33"))


def fmt_time(dt) -> str:
    """Render an S3 timestamp (tz-aware UTC) in the configured display timezone.
    The %Z suffix labels the zone explicitly (e.g. 'KST'), so a time is never
    misread as some other zone."""
    if dt is None:
        return "-"
    try:
        return dt.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return str(dt)


def strip_etag(s: str | None) -> str:
    """ETag comes wrapped in literal quotes (and for multipart is <md5>-<n>, not a
    content MD5). Just unwrap the quotes for display."""
    return (s or "").strip('"')


def is_probably_text(data: bytes) -> bool:
    """Decide text-vs-binary by content, not by an (often wrong) ContentType.
    NUL byte → binary; else attempt a strict utf-8 decode of a sample."""
    if not data:
        return True
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        # May just be a multi-byte char split at the sample boundary; fall back to
        # a printable-ratio heuristic.
        printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b <= 126 or b >= 128)
        return printable / len(sample) > 0.85


def hexdump(data: bytes, limit: int = 256) -> str:
    """Compact hex+ascii preview of the first `limit` bytes (for binary objects)."""
    lines = []
    for i in range(0, min(len(data), limit), 16):
        chunk = data[i : i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:08x}  {hexs:<47}  {ascii_}")
    if len(data) > limit:
        lines.append(f"… (+{len(data) - limit} more bytes)")
    return "\n".join(lines)


def friendly_error(exc: Exception) -> str:
    """Map botocore exceptions to a short, actionable one-line message."""
    if isinstance(exc, (bexc.EndpointConnectionError, bexc.ConnectTimeoutError, bexc.ReadTimeoutError)):
        return color(f"cannot reach endpoint — is the server up and endpoint_url correct? ({exc})", "31")
    if isinstance(exc, bexc.SSLError):
        return color(
            'TLS verification failed — for self-signed certs set "verify_ssl": false '
            f'(or a "ca_bundle" path) in config.json. ({exc})',
            "31",
        )
    if isinstance(exc, bexc.ClientError):
        err = exc.response.get("Error", {}) if getattr(exc, "response", None) else {}
        code = str(err.get("Code", "?"))
        msgs = {
            "NoSuchKey": "no such key.",
            "NoSuchBucket": "no such bucket.",
            "AccessDenied": "access denied — check credentials/permissions.",
            "InvalidAccessKeyId": "invalid access key id — check access_key_id in config.json.",
            "SignatureDoesNotMatch": (
                'signature mismatch — on self-hosted servers this is often addressing_style '
                '(try "path") or the checksum settings; also verify secret_access_key.'
            ),
            "NotImplemented": "the endpoint doesn't implement this operation.",
            "404": "not found.",
            "403": "forbidden — access denied.",
        }
        hint = msgs.get(code, err.get("Message", "request failed."))
        return color(f"[{code}] {hint}", "31")
    if isinstance(exc, bexc.BotoCoreError):
        return color(f"error: {exc}", "31")
    return color(f"error: {exc}", "31")


# --- color + terminal/logfile mirroring (adapted from openai-sdk-client) ------
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
    """Mirror stdout/stderr into outputs/<prefix>-<ts>-<pid>.txt and return the
    path. Uses a fixed-ish name pattern like the sibling; outputs/ is gitignored."""
    import datetime

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


# --- REPL skeleton ------------------------------------------------------------
def repl(dispatch, banner: str, prompt: str = "s3> ", completer=None) -> int:
    """Drive the interactive loop. `dispatch(line)` handles one command line.
    /exit, /quit, or EOF (Ctrl-D) leaves; Ctrl-C at the prompt cancels the line.

    When `completer` is given and readline is available, Tab-completion and
    in-session command history are enabled. readline only drives line editing
    while stdout is a real terminal, so we temporarily restore sys.__stdout__
    around input() (the Tee logger otherwise sits in stdout's place)."""
    try:
        import readline
    except ImportError:
        readline = None
    if readline is not None and completer is not None:
        readline.set_completer(completer)
        # Only whitespace splits words, so a whole key/prefix argument (which may
        # contain '/', ':', spaces-are-rare) is completed as a single unit.
        readline.set_completer_delims(" \t\n")
        # macOS ships libedit under the "readline" name; its bind syntax differs.
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")

    sys.stdout.write(banner + "\n")
    while True:
        tee = sys.stdout
        # Hand the real terminal to readline for line editing / completion.
        sys.stdout = sys.__stdout__
        try:
            sys.stdout.write("\n")
            line = input(prompt)
        except EOFError:
            sys.stdout = tee
            sys.stdout.write("\n[eof]\n")
            return 0
        except KeyboardInterrupt:
            sys.stdout = tee
            sys.stdout.write("\n(use /exit or Ctrl-D to quit)\n")
            continue
        sys.stdout = tee
        cmd = line
        if not cmd.strip():
            continue
        if cmd.strip().lower() in ("/exit", "/quit", "exit", "quit"):
            sys.stdout.write("[exit]\n")
            return 0
        # Record the command in the logfile only — the terminal already echoed it
        # as it was typed, so writing through the Tee would show it twice.
        if isinstance(sys.stdout, _Tee):
            sys.stdout.file.write(prompt + cmd + "\n")
            sys.stdout.file.flush()
        dispatch(cmd)
