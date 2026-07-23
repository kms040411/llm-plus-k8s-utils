#!/usr/bin/env python3
"""Interactive, **read-only** inspector for S3-compatible object stores.

  uv run explore.py        # start the interactive session
  uv run explore.py -h     # options

Connection settings come from a JSON config (see config.example.json). On start
it prints a cheap overview (top-level "folders" + a first-page key sample) and
drops into a command REPL. Commands: ls, list, summary, stat, cat, save, use,
buckets, help. It never mutates the bucket (no put/delete/copy).
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

try:
    import readline  # enables Tab-completion + command history in the REPL
except ImportError:  # pragma: no cover
    readline = None

import botocore.exceptions as bexc

import common

# Objects larger than this are not dumped whole by `cat`; it previews the first
# chunk (via a Range request) and points you at `save`.
PREVIEW_LIMIT = 1 * 1024 * 1024  # 1 MiB

STATE: "State" = None  # set in main()


class State:
    def __init__(self, client, bucket: str, cfg: dict):
        self.client = client
        self.bucket = bucket
        self.cfg = cfg
        self.max_keys = int(cfg.get("max_keys_default", 1000))


# --- sorting + flag parsing ---------------------------------------------------
_SORT_FIELDS = {
    "name": "name", "key": "name",
    "size": "size", "bytes": "size",
    "date": "date", "time": "date", "mtime": "date",
    "modified": "date", "lastmodified": "date",
}
# Unix-ls-like default direction: names ascending, size/date descending
# (largest / newest first), overridable with --asc / --desc.
_SORT_DEFAULT_DESC = {"name": False, "size": True, "date": True}
_SORT_KEYFN = {
    "name": lambda o: o["Key"],
    "size": lambda o: o["Size"],
    "date": lambda o: o["LastModified"],
}


def _parse_list_flags(arg: str):
    """Pull leading --flags off the front; the remainder is the literal prefix.
    Flags must precede the prefix. Returns (opts, prefix)."""
    opts = {"all": False, "sort": None, "desc": None,
            "today": False, "since": None, "until": None, "grep": None}
    while True:
        head = arg.lstrip()
        if not head.startswith("-"):
            break
        parts = head.split(None, 1)
        tok = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if tok == "--all":
            opts["all"] = True
            arg = rest
        elif tok == "--desc":
            opts["desc"] = True
            arg = rest
        elif tok == "--asc":
            opts["desc"] = False
            arg = rest
        elif tok.startswith("--sort="):
            opts["sort"] = tok.split("=", 1)[1]
            arg = rest
        elif tok in ("--sort", "-s"):
            field_parts = rest.split(None, 1)
            opts["sort"] = field_parts[0].lower() if field_parts else ""
            arg = field_parts[1] if len(field_parts) > 1 else ""
        elif tok == "--today":
            opts["today"] = True
            arg = rest
        elif tok.startswith("--since=") or tok.startswith("--until="):
            # Value from parts[0] (original case), not tok (lowercased) — harmless
            # for dates but preserves case for the sibling --grep= form below.
            key = "since" if tok.startswith("--since=") else "until"
            opts[key] = parts[0].split("=", 1)[1]
            arg = rest
        elif tok in ("--since", "--until"):
            vp = rest.split(None, 1)
            opts[tok[2:]] = vp[0] if vp else None  # None (not "") when the value is missing
            arg = vp[1] if len(vp) > 1 else ""
        elif tok.startswith("--grep="):
            opts["grep"] = parts[0].split("=", 1)[1]  # parts[0], not tok — keep the needle's case
            arg = rest
        elif tok == "--grep":
            vp = rest.split(None, 1)
            opts["grep"] = vp[0] if vp else None
            arg = vp[1] if len(vp) > 1 else ""
        else:
            break  # unknown flag → treat it as the start of the (literal) prefix
    return opts, arg


def _resolve_sort(opts):
    """Map the requested sort to (field|None, desc:bool), warning on a bad field."""
    raw = opts.get("sort")
    if not raw:
        return None, False
    field = _SORT_FIELDS.get(raw)
    if field is None:
        print(common.color(f"unknown sort field {raw!r} — use name|size|date; listing unsorted", "33"))
        return None, False
    desc = opts["desc"] if opts["desc"] is not None else _SORT_DEFAULT_DESC[field]
    return field, desc


def _fmt_row(o) -> str:
    return f"{common.human_size(o['Size']):>10}  {common.fmt_time(o['LastModified'])}  {o['Key']}"


def _sort_note(field, desc) -> str:
    return f", sorted by {field} {'↓desc' if desc else '↑asc'}" if field else ""


# --- filtering ----------------------------------------------------------------
_REL_RE = re.compile(r"^(\d+)([dhm])$")  # relative spec: d=days, h=hours, m=minutes


def _parse_when(spec: str, *, is_until: bool):
    """Parse a date-filter bound into a tz-aware instant. 'Nd'/'Nh'/'Nm' means N
    days/hours/minutes before now (UTC-aware); 'YYYY-MM-DD' is midnight in the
    display timezone (for --until it's the *next* midnight, so the named day is
    included). Raises ValueError on anything unparseable."""
    s = spec.strip()
    m = _REL_RE.match(s.lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
        return datetime.now(timezone.utc) - delta
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=common.display_tz())
    return d + timedelta(days=1) if is_until else d


def _build_filter(opts):
    """Build a predicate over listed objects from the filter flags. Returns
    (predicate, active): a half-open [lo, hi) window on LastModified — dates are
    resolved in the display timezone, so --today matches the KST calendar day the
    user sees — AND an optional case-sensitive substring match on the key. An
    explicit --since/--until overrides the matching bound set by --today."""
    tz = common.display_tz()
    lo = hi = None
    if opts.get("today"):
        t0 = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        lo, hi = t0, t0 + timedelta(days=1)
    for key, is_until in (("since", False), ("until", True)):
        if opts.get(key):
            try:
                bound = _parse_when(opts[key], is_until=is_until)
            except ValueError:
                print(common.color(
                    f"bad --{key} {opts[key]!r} (use YYYY-MM-DD or Nd/Nh/Nm); ignoring", "33"))
                continue
            if is_until:
                hi = bound
            else:
                lo = bound
    sub = opts.get("grep") or None
    active = lo is not None or hi is not None or sub is not None

    def pred(o):
        if sub is not None and sub not in o["Key"]:
            return False
        if lo is None and hi is None:
            return True
        lm = o.get("LastModified")
        if lm is None:
            return False
        if lm.tzinfo is None:  # defensive: a nonconforming server could return a naive time
            lm = lm.replace(tzinfo=timezone.utc)
        return (lo is None or lm >= lo) and (hi is None or lm < hi)

    return pred, active


def _filter_note(opts) -> str:
    """Human-readable ', filtered (…)' tail for summary lines (mirrors _sort_note)."""
    bits = []
    if opts.get("today"):
        bits.append("today")
    if opts.get("since"):
        bits.append(f"since {opts['since']}")
    if opts.get("until"):
        bits.append(f"until {opts['until']}")
    if opts.get("grep"):
        bits.append(f"~{opts['grep']!r}")
    return f", filtered ({', '.join(bits)})" if bits else ""


# --- listing ------------------------------------------------------------------
def cmd_ls(st: State, arg: str) -> None:
    """Folder-style view of one level: sub-prefixes + keys directly under a prefix.
      ls [--sort name|size|date] [--asc|--desc]
         [--today | --since S | --until S] [--grep SUB] [prefix]
    Prefixes are always listed first (alphabetically); --sort orders the keys.
    Date filters apply to keys only (prefixes have no timestamp); --grep also
    narrows prefixes."""
    opts, prefix = _parse_list_flags(arg)
    field, desc = _resolve_sort(opts)
    pred, filt = _build_filter(opts)
    resp = st.client.list_objects_v2(
        Bucket=st.bucket, Prefix=prefix, Delimiter="/", MaxKeys=st.max_keys
    )
    dirs = sorted(p["Prefix"] for p in resp.get("CommonPrefixes", []))
    objs = resp.get("Contents", [])
    if filt:
        objs = [o for o in objs if pred(o)]
        if opts["grep"]:  # a name filter can narrow folders too; date bounds can't
            dirs = [d for d in dirs if opts["grep"] in d]
    if field:
        objs = sorted(objs, key=_SORT_KEYFN[field], reverse=desc)
    for d in dirs:
        print(common.color(d, "1;34"))
    for o in objs:
        print(_fmt_row(o))
    if not dirs and not objs:
        print("(empty)" + (" — nothing matches the filter" if filt else ""))
        return
    print(common.color(f"— {len(dirs)} prefixes, {len(objs)} keys at this level{_sort_note(field, desc)}{_filter_note(opts)}", "2"))
    if resp.get("IsTruncated"):
        print(common.color(f"  (truncated at {st.max_keys}; use 'list {prefix}' for the full flat listing)", "2"))


def cmd_list(st: State, arg: str) -> None:
    """Flat recursive listing under a prefix.
      list [--all] [--sort name|size|date] [--asc|--desc]
           [--today | --since S | --until S] [--grep SUB] [prefix]
    Without --sort it streams (Ctrl-C shows the partial result); with --sort it
    buffers the (capped) result, sorts, then prints. Flags precede the prefix,
    which is taken literally. Filters run client-side over the scanned keys (S3
    has no server-side date filter), so add --all to search the whole bucket."""
    opts, prefix = _parse_list_flags(arg)
    field, desc = _resolve_sort(opts)
    pred, filt = _build_filter(opts)
    paginator = st.client.get_paginator("list_objects_v2")
    pg_cfg = {} if opts["all"] else {"MaxItems": st.max_keys}
    cap_note = "" if opts["all"] else f"  (capped at {st.max_keys}; add --all for everything)"

    def _scan_note(scanned):
        # If the filter only saw a capped window, say so rather than imply the
        # whole bucket was searched.
        if filt and not opts["all"] and scanned >= st.max_keys:
            return f"  (filter applied to the first {scanned:,} scanned keys; add --all to search all)"
        return cap_note

    if field is None:
        # Stream as we paginate — best for huge buckets; Ctrl-C leaves a partial.
        matched = scanned = total = 0
        # A filtered stream can go quiet for a while (few matches over many keys);
        # show scan progress like `summary`, on the real terminal only (keeps the
        # logfile clean), and clear it before printing a matched row.
        show = filt and sys.__stdout__ is not None and sys.__stdout__.isatty()
        progress = False
        pages = 0

        def _clear():
            nonlocal progress
            if progress:
                sys.__stdout__.write("\r" + " " * 60 + "\r")
                progress = False

        try:
            for page in paginator.paginate(Bucket=st.bucket, Prefix=prefix, PaginationConfig=pg_cfg):
                pages += 1
                for o in page.get("Contents", []):
                    scanned += 1
                    if filt and not pred(o):
                        continue
                    _clear()
                    print(_fmt_row(o))
                    matched += 1
                    total += o["Size"]
                if show and pages % 5 == 0:
                    sys.__stdout__.write(f"\r  scanning… {scanned:,} scanned, {matched:,} matched    ")
                    sys.__stdout__.flush()
                    progress = True
        except KeyboardInterrupt:
            _clear()
            print(common.color(f"\n[interrupted] shown {matched:,} keys ({common.human_size(total)}) so far", "33"))
            return
        _clear()
        if matched == 0:
            print("(no keys)" + (f" under {prefix!r}" if prefix else "") + (" match the filter" if filt else ""))
            return
        print(common.color(f"— {matched:,} keys{_filter_note(opts)}, {common.human_size(total)}{_scan_note(scanned)}", "1;32"))
        return

    # Sorted: buffer the (capped) result before printing — sorting needs it all.
    raw = []
    try:
        for page in paginator.paginate(Bucket=st.bucket, Prefix=prefix, PaginationConfig=pg_cfg):
            raw.extend(page.get("Contents", []))
    except KeyboardInterrupt:
        print(common.color(f"\n[interrupted] collected {len(raw):,} keys; sorting those", "33"))
    objs = [o for o in raw if pred(o)] if filt else raw
    if not objs:
        print("(no keys)" + (f" under {prefix!r}" if prefix else "") + (" match the filter" if filt else ""))
        return
    objs.sort(key=_SORT_KEYFN[field], reverse=desc)
    total = 0
    for o in objs:
        print(_fmt_row(o))
        total += o["Size"]
    print(common.color(f"— {len(objs):,} keys{_sort_note(field, desc)}{_filter_note(opts)}, {common.human_size(total)}{_scan_note(len(raw))}", "1;32"))


def _print_top(top: dict, prefix: str = "") -> None:
    if not top:
        return
    print(common.color("  top-level breakdown:", "1"))
    for name, (cnt, size) in sorted(top.items(), key=lambda kv: kv[1][1], reverse=True):
        print(f"    {common.human_size(size):>10}  {cnt:>9,}  {prefix}{name}")


def cmd_summary(st: State, arg: str) -> None:
    """Full stats under `arg`: object count + total size + top-level breakdown.
    Ctrl-C stops the scan and prints the partial total (stays in the REPL)."""
    prefix = arg
    paginator = st.client.get_paginator("list_objects_v2")
    count = 0
    total = 0
    pages = 0
    top: dict = {}
    show_progress = sys.__stdout__ is not None and sys.__stdout__.isatty()
    try:
        for page in paginator.paginate(Bucket=st.bucket, Prefix=prefix):
            pages += 1
            for o in page.get("Contents", []):
                count += 1
                total += o["Size"]
                rel = o["Key"][len(prefix):] if prefix else o["Key"]
                head = rel.split("/", 1)[0] + ("/" if "/" in rel else "")
                slot = top.setdefault(head or "(root)", [0, 0])
                slot[0] += 1
                slot[1] += o["Size"]
            if show_progress and pages % 5 == 0:
                sys.__stdout__.write(f"\r  scanning… {count:,} keys, {common.human_size(total)}    ")
                sys.__stdout__.flush()
    except KeyboardInterrupt:
        if show_progress:
            sys.__stdout__.write("\r" + " " * 60 + "\r")
        print(common.color(f"[interrupted] partial: {count:,} keys, {common.human_size(total)}", "33"))
        _print_top(top, prefix)
        return
    if show_progress:
        sys.__stdout__.write("\r" + " " * 60 + "\r")
    scope = f" under {prefix!r}" if prefix else ""
    print(common.color(f"bucket '{st.bucket}'{scope}: {count:,} objects, {common.human_size(total)} total", "1;32"))
    _print_top(top, prefix)


# --- metadata + values --------------------------------------------------------
def _try_owner(st: State, key: str) -> str:
    """Owner/user is best-effort: many S3-compatible servers don't implement ACLs.
    (There is no reliable "who last modified" concept in the S3 API.)"""
    try:
        acl = st.client.get_object_acl(Bucket=st.bucket, Key=key)
        o = acl.get("Owner") or {}
        return o.get("DisplayName") or o.get("ID") or "unavailable"
    except bexc.ClientError:
        return "unavailable (endpoint has no ACL support)"
    except bexc.BotoCoreError:
        return "unavailable"


def cmd_stat(st: State, arg: str) -> None:
    """Metadata for one key: size, last-modified (display timezone), etag,
    content-type, storage class, custom x-amz-meta-*, and best-effort owner."""
    key = arg
    if not key:
        print("usage: stat <key>")
        return
    h = st.client.head_object(Bucket=st.bucket, Key=key)
    print(common.color(key, "1;37"))
    size = h["ContentLength"]
    print(f"  size:          {size:,} bytes ({common.human_size(size)})")
    print(f"  last-modified: {common.fmt_time(h.get('LastModified'))}")
    print(f"  etag:          {common.strip_etag(h.get('ETag', ''))}")
    print(f"  content-type:  {h.get('ContentType', '-')}")
    print(f"  storage-class: {h.get('StorageClass', 'STANDARD')}")
    meta = h.get("Metadata") or {}
    if meta:
        print("  user-metadata:")
        for k, v in meta.items():
            print(f"    x-amz-meta-{k}: {v}")
    print(f"  owner:         {_try_owner(st, key)}")


def cmd_cat(st: State, arg: str) -> None:
    """Print an object's value. Guards on size (previews large objects via a Range
    request) and prints binary as a hex preview rather than dumping raw bytes."""
    key = arg
    if not key:
        print("usage: cat <key>")
        return
    h = st.client.head_object(Bucket=st.bucket, Key=key)
    size = h["ContentLength"]
    truncated = size > PREVIEW_LIMIT
    if truncated:
        print(common.color(
            f"{key} is {common.human_size(size)} — showing first {common.human_size(PREVIEW_LIMIT)} "
            f"(use 'save {key}' for the whole object)", "33"))
        obj = st.client.get_object(Bucket=st.bucket, Key=key, Range=f"bytes=0-{PREVIEW_LIMIT - 1}")
    else:
        obj = st.client.get_object(Bucket=st.bucket, Key=key)
    data = obj["Body"].read()

    if common.is_probably_text(data):
        sys.stdout.write(data.decode("utf-8", errors="replace"))
        if not data.endswith(b"\n"):
            sys.stdout.write("\n")
        if truncated:
            print(common.color("… [truncated]", "2"))
    else:
        print(common.color(
            f"[binary — {common.human_size(size)}, content-type {h.get('ContentType', '?')}]", "35"))
        print(common.hexdump(data, 256))
        print(common.color(f"use 'save {key}' to download the full object.", "2"))


def cmd_save(st: State, arg: str) -> None:
    """Download an object to ./<basename> (streamed to disk — safe for large files)."""
    key = arg
    if not key:
        print("usage: save <key>   (saves to ./<basename>)")
        return
    basename = key.rstrip("/").split("/")[-1] or "object"
    dest = pathlib.Path.cwd() / basename
    st.client.download_file(st.bucket, key, str(dest))
    print(common.color(f"saved → {dest} ({common.human_size(dest.stat().st_size)})", "1;32"))


# --- buckets ------------------------------------------------------------------
def cmd_use(st: State, arg: str) -> None:
    """Switch the active bucket (verifies access)."""
    bucket = arg.strip()
    if not bucket:
        print(f"current bucket: {st.bucket or '(none)'}")
        return
    try:
        st.client.head_bucket(Bucket=bucket)
    except (bexc.ClientError, bexc.BotoCoreError) as e:
        print(common.friendly_error(e))
        return
    st.bucket = bucket
    print(common.color(f"switched to bucket '{bucket}'", "1;32"))


def cmd_buckets(st: State, arg: str) -> None:
    """List buckets visible to these credentials (best-effort)."""
    resp = st.client.list_buckets()
    owner = resp.get("Owner") or {}
    print(f"owner: {owner.get('DisplayName') or owner.get('ID') or '?'}")
    buckets = resp.get("Buckets", [])
    if not buckets:
        print("(no buckets visible)")
        return
    for b in buckets:
        marker = common.color("* ", "1;32") if b["Name"] == st.bucket else "  "
        print(f"{marker}{b['Name']}  (created {common.fmt_time(b.get('CreationDate'))})")


def cmd_help(st: State, arg: str) -> None:
    print(common.color("commands (read-only):", "1;36"))
    print("""  ls   [--sort F] [--asc|--desc] [FILTER] [prefix]          folder view (prefixes + keys)
  list [--all] [--sort F] [--asc|--desc] [FILTER] [prefix]  flat recursive listing
  summary [prefix]      total object count + size + top-level breakdown (Ctrl-C = partial)
  stat <key>            metadata: size, last-modified, etag, type, owner (best-effort)
  cat <key>             print value (text); large → preview, binary → hex
  save <key>            download to ./<basename>
  use [bucket]          switch active bucket (no arg = show current)
  buckets               list buckets (best-effort)
  help                  this help
  /exit, /quit, Ctrl-D  quit  (Ctrl-C cancels the current listing/scan)

  sort field F: name | size | date   (default direction: name↑, size↓, date↓)
  FILTER (ls/list): --today | --since S | --until S | --grep SUB
    S = YYYY-MM-DD (that day) or Nd/Nh/Nm ago (d=days, h=hours, m=minutes); range is [start, end)
    --grep = case-sensitive substring on the key; filters combine (AND) and run over scanned keys
    (add --all so 'list' searches the whole bucket, not just the first page).
  times use the display timezone — KST by default; set "display_timezone" in config.json.
  Tab completes commands, keys/prefixes, buckets and flags; ↑/↓ recalls history.""")


# --- dispatch -----------------------------------------------------------------
COMMANDS = {
    "ls": cmd_ls,
    "list": cmd_list,
    "summary": cmd_summary,
    "stat": cmd_stat,
    "head": cmd_stat,
    "cat": cmd_cat,
    "get": cmd_cat,
    "save": cmd_save,
    "use": cmd_use,
    "buckets": cmd_buckets,
    "help": cmd_help,
    "?": cmd_help,
}
# Commands that don't need an active bucket.
_NO_BUCKET_OK = {"use", "buckets", "help", "?"}
# Commands whose argument is an S3 key/prefix (→ Tab-complete against the bucket).
_KEY_COMMANDS = {"ls", "list", "summary", "stat", "head", "cat", "get", "save"}
_FILTER_FLAGS = ("--today", "--since", "--until", "--grep")
_LIST_FLAGS = ("--all", "--sort", "--asc", "--desc") + _FILTER_FLAGS
_LS_FLAGS = ("--sort", "--asc", "--desc") + _FILTER_FLAGS


# --- Tab completion -----------------------------------------------------------
def _complete_key(text: str):
    """Directory-style key/prefix completion via one Delimiter='/' list call, so a
    single Tab fetches only the current level rather than the whole bucket."""
    if STATE is None or not STATE.bucket:
        return []
    try:
        resp = STATE.client.list_objects_v2(
            Bucket=STATE.bucket, Prefix=text, Delimiter="/", MaxKeys=1000
        )
    except Exception:
        return []
    out = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]
    out += [o["Key"] for o in resp.get("Contents", [])]
    return out


def _complete_bucket(text: str):
    if STATE is None:
        return []
    try:
        resp = STATE.client.list_buckets()
    except Exception:
        return []
    return [b["Name"] + " " for b in resp.get("Buckets", []) if b["Name"].startswith(text)]


def complete(text: str, state: int):
    """readline completer: command names first, then per-command completions —
    S3 keys/prefixes, bucket names, flag names, or sort fields."""
    if state == 0:
        buf = readline.get_line_buffer()
        stripped = buf.lstrip()
        parts = stripped.split()
        if " " not in stripped:
            complete.matches = [c + " " for c in COMMANDS if c.startswith(text)]
        else:
            cmd = parts[0].lower()
            prev = parts[-1] if buf.endswith(" ") else (parts[-2] if len(parts) >= 2 else "")
            if prev.lower() in ("--sort", "-s"):
                complete.matches = [f + " " for f in ("name", "size", "date") if f.startswith(text)]
            elif prev.lower() in ("--since", "--until"):
                complete.matches = [s for s in ("1d ", "7d ", "30d ", "24h ") if s.startswith(text)]
            elif prev.lower() == "--grep":
                complete.matches = []  # free-form substring — nothing sensible to complete
            elif text.startswith("-"):
                flags = _LIST_FLAGS if cmd == "list" else (_LS_FLAGS if cmd == "ls" else ())
                complete.matches = [f + " " for f in flags if f.startswith(text)]
            elif cmd in _KEY_COMMANDS:
                complete.matches = _complete_key(text)
            elif cmd == "use":
                complete.matches = _complete_bucket(text)
            else:
                complete.matches = []
    try:
        return complete.matches[state]
    except (AttributeError, IndexError):
        return None


def dispatch(line: str) -> None:
    line = line.strip()
    if not line:
        return
    # First token is the command; the rest is the literal argument (keys/prefixes
    # may contain spaces, unicode, trailing '/', so we don't shell-split it).
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    fn = COMMANDS.get(cmd)
    if fn is None:
        print(common.color(f"unknown command: {cmd!r} (try 'help')", "31"))
        return
    if cmd not in _NO_BUCKET_OK and not STATE.bucket:
        print(common.color("no bucket selected — use 'use <bucket>', or set \"bucket\" in config.json", "33"))
        return
    try:
        fn(STATE, arg)
    except (bexc.ClientError, bexc.BotoCoreError) as e:
        print(common.friendly_error(e))
    except KeyboardInterrupt:
        print(common.color("\n[cancelled]", "33"))


# --- startup + main -----------------------------------------------------------
def startup_overview(st: State) -> None:
    """Cheap, O(1)-ish overview: verify the bucket, then a single Delimiter='/'
    page for the top-level shape. No full scan — that's what `summary` is for."""
    if not st.bucket:
        print(common.color("no bucket selected — 'buckets' to list, 'use <name>' to pick one", "33"))
        return
    try:
        st.client.head_bucket(Bucket=st.bucket)
    except (bexc.ClientError, bexc.BotoCoreError) as e:
        print(common.friendly_error(e))
        return
    resp = st.client.list_objects_v2(Bucket=st.bucket, Delimiter="/", MaxKeys=st.max_keys)
    dirs = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]
    objs = resp.get("Contents", [])
    print(common.color(f"bucket '{st.bucket}':", "1;36"))
    if dirs:
        head = ", ".join(dirs[:20]) + (" …" if len(dirs) > 20 else "")
        print(f"  top-level folders ({len(dirs)}): {head}")
    more = " (bucket may be larger — run 'summary' for exact totals)" if resp.get("IsTruncated") else ""
    print(f"  top-level keys shown: {len(objs)}{more}")
    if not dirs and not objs:
        print("  (bucket appears empty)")
    print(common.color("  type 'help' for commands", "2"))


def main() -> int:
    global STATE

    p = argparse.ArgumentParser(
        description="Interactive read-only inspector for S3-compatible object stores.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config", help="path to config.json (else $S3_EXPLORER_CONFIG, then ./config.json)")
    p.add_argument("--endpoint-url", help="override endpoint_url from config")
    p.add_argument("--bucket", help="override bucket from config")
    p.add_argument("--verify-ssl", action=argparse.BooleanOptionalAction, default=None,
                   help="override verify_ssl from config")
    args = p.parse_args()

    cfg = common.load_config(args.config)
    if args.endpoint_url is not None:
        cfg["endpoint_url"] = args.endpoint_url
    if args.bucket is not None:
        cfg["bucket"] = args.bucket
    if args.verify_ssl is not None:
        cfg["verify_ssl"] = args.verify_ssl

    common.set_display_timezone(cfg.get("display_timezone", "Asia/Seoul"))
    client = common.make_client(cfg)
    STATE = State(client, cfg.get("bucket") or "", cfg)

    # Session log + startup overview + interactive REPL.
    common.install_logfile("s3")
    ep = cfg.get("endpoint_url") or "(AWS default)"
    print(common.color(
        f"[endpoint: {ep}]  [bucket: {STATE.bucket or '(none)'}]  [verify_ssl: {cfg.get('verify_ssl', True)}]",
        "2"))
    startup_overview(STATE)
    banner = common.color("\n[s3-explorer — read-only. 'help' for commands, '/exit' or Ctrl-D to quit]", "1;36")
    return common.repl(dispatch, banner, completer=complete)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
