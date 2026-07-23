# s3-explorer

Interactive, **read-only** inspector for S3-compatible object stores (MinIO,
Ceph/RGW, self-hosted — and real AWS S3). List keys, drill into prefixes, print
values, and see basic metadata (owner best-effort, last-modified). It never
mutates the bucket (no put/delete/copy).

## Setup

```bash
cd s3-explorer
uv sync                              # install boto3 (uv manages Python too)
cp config.example.json config.json   # then edit config.json with your endpoint + keys
```

## Run

```bash
uv run explore.py     # interactive session (overview on start; /exit or Ctrl-D to quit)
uv run explore.py -h  # options (--config, --endpoint-url, --bucket, --no-verify-ssl)
```

On start it verifies the bucket and prints a cheap overview (top-level "folders"
+ a first-page key sample), then drops you at the `s3>` prompt.

## Commands

| Command | Does |
|---|---|
| `ls [--sort F] [--asc\|--desc] [--today\|--since S\|--until S] [--grep SUB] [prefix]` | Folder view — sub-prefixes + keys directly at this level (`Delimiter="/"`). Prefixes list first; `--sort` orders the keys; filters narrow them (see [Filtering](#filtering)). |
| `list [--all] [--sort F] [--asc\|--desc] [--today\|--since S\|--until S] [--grep SUB] [prefix]` | Flat recursive listing under `prefix`. Capped at `max_keys_default`; put `--all` first to remove the cap. Filters run over the scanned keys (see [Filtering](#filtering)). |
| `summary [prefix]` | Full object count + total size + top-level breakdown. `Ctrl-C` stops the scan and prints the partial total. |
| `stat <key>` | Metadata: size, last-modified (KST), etag, content-type, storage-class, custom `x-amz-meta-*`, and best-effort owner. |
| `cat <key>` | Print the value. Objects > 1 MiB are previewed; binary is shown as a hex dump. |
| `save <key>` | Download to `./<basename>` (streamed to disk — safe for large files). |
| `use [bucket]` | Switch the active bucket (no arg = show current). |
| `buckets` | List buckets visible to these credentials (best-effort). |
| `help` | Command help. |
| `/exit`, `/quit`, `Ctrl-D` | Quit. (`Ctrl-C` cancels the current listing/scan.) |

Keys and prefixes are taken **literally** — everything after the command word (and
any leading flags) is the key, so keys with spaces/unicode/trailing `/` work without
quoting. `--sort`/`--all` flags must come *before* the prefix.

### Tab-completion & history

In the REPL, **Tab** completes:
- command names (`l`↹ → `ls`/`list`),
- S3 keys/prefixes one path segment at a time (`cat orch/`↹ lists that "folder"),
- bucket names after `use`, and flag/sort-field names (`list --sort `↹ → `name`/`size`/`date`).

Completion is directory-style (one `Delimiter="/"` request per Tab), so it stays cheap
on large buckets. **↑/↓** recall previous commands within the session. (Requires the
`readline` module; if absent, the REPL still works without completion.)

### Sorting

`ls` and `list` accept `--sort name|size|date` with optional `--asc`/`--desc`.
Default direction is Unix-`ls`-like: **name ascending**, **size/date descending**
(largest / newest first). `list --sort` buffers the (capped) result before printing;
without `--sort`, `list` streams and stays interruptible on huge buckets.

```
list --sort size --desc orch/     # biggest objects first
list --sort date orch/logs/       # newest first (date defaults to descending)
ls --sort name orch/              # this level, keys A→Z
```

### Times & timezone

Timestamps (`ls`/`list` rows, `stat`, `buckets`) show in **KST** by default, labeled with the zone
(e.g. `2026-07-02 22:12:27 KST`). Set `"display_timezone"` in `config.json` to any IANA name
(`"UTC"`, `"America/New_York"`, …) to change it — the date filters below resolve "today"/dates in
that same zone, so filtering always matches what you see.

### Filtering

`ls` and `list` accept date and name filters (flags precede the prefix, like `--sort`):

- `--today` — keys last-modified **today** (in the display timezone).
- `--since S` / `--until S` — a half-open `[start, end)` range on last-modified. `S` is an absolute
  `YYYY-MM-DD` (midnight in the display timezone; `--until` includes the whole named day) or a
  relative `Nd`/`Nh`/`Nm` (N **d**ays / **h**ours / **m**inutes ago).
- `--grep SUB` — case-sensitive substring match on the key (also narrows folder names in `ls`).

Filters combine with **AND**; an explicit `--since`/`--until` overrides the bound implied by
`--today`. Date bounds apply to keys only (prefixes have no timestamp).

```
list --today orch/                 # modified today, under orch/
list --since 2026-07-01 orch/      # on/after 2026-07-01
list --until 7d --all              # older than 7 days ago, whole bucket
ls --grep item: orch/              # keys/prefixes containing "item:"
list --today --grep .json orch/    # today AND key contains ".json"
```

> **Caveat:** S3 has no server-side date filter, so filters run client-side over the keys actually
> scanned. Without `--all`, `list` scans only the first `max_keys_default` keys (lexicographic order)
> before filtering, so a late-sorting match can be missed — the summary line flags this and points you
> to `--all`. `summary` always full-scans, so it stays the reliable home for whole-bucket questions.

## Configuration (`config.json`)

Resolution order: `--config <path>` → `$S3_EXPLORER_CONFIG` → `./config.json`.
`config.json` is gitignored (it holds secrets); `config.example.json` is the
committed template.

| Key | Default | Notes |
|---|---|---|
| `endpoint_url` | — | Self-hosted endpoint. Omit / `null` for real AWS S3. |
| `region` | `us-east-1` | SigV4 needs a region string even if the server ignores it; for AWS it must match the bucket. |
| `access_key_id` / `secret_access_key` | — | Optional — if omitted, boto3's credential chain / `profile` resolves them. |
| `session_token` | `null` | Optional (STS). |
| `bucket` | — | Optional — if omitted, start with none and pick via `use <bucket>`. |
| `addressing_style` | `path` | `path` for self-hosted (MinIO/Ceph); `auto`/`virtual` for AWS. |
| `verify_ssl` | `true` | `false` for self-signed certs (also silences the urllib3 warning). |
| `ca_bundle` | `null` | Optional path to a private-CA bundle. |
| `signature_version` | `s3v4` | Rarely changed; `s3` only for very old Ceph. |
| `request_checksum_calculation` | `when_required` | See "quirks" below. `when_supported` for AWS-native behavior. |
| `response_checksum_validation` | `when_required` | Same. |
| `connect_timeout` / `read_timeout` | `10` / `60` | Fail fast on a dead endpoint. |
| `max_attempts` | `3` | Bounded retries (standard mode). |
| `max_keys_default` | `1000` | Page size / default listing cap. |
| `display_timezone` | `Asia/Seoul` | IANA timezone for displayed times **and** date filters (`UTC`, `America/New_York`, …). |
| `profile` | — | Optional named AWS profile (instead of inline keys). |

## Known S3-compatible quirks

- **Checksums.** Since early 2025, botocore defaults to `when_supported`, which
  adds a CRC32 that many S3-compatible servers reject or mishandle (opaque `400`s,
  failed downloads). We default both checksum settings to `when_required`; flip to
  `when_supported` if you're on AWS and want native integrity protection.
- **Addressing style.** Self-hosted servers usually need `"path"` (virtual-hosted
  style wants wildcard DNS they rarely have). A `SignatureDoesNotMatch` on a
  self-hosted server is very often this (or the checksum setting).
- **TLS.** For self-signed certs set `"verify_ssl": false`, or point `"ca_bundle"`
  at your CA.
- **Owner / ACL.** Owner is **best-effort**: many S3-compatible servers don't
  implement ACLs, and the value is a canonical ID / display name, not a username.
  There is **no reliable "who last modified" concept** in the S3 API. `stat` shows
  the owner when available, else "unavailable".

## Notes

- `cat` guards on size (`head_object` first): objects over 1 MiB are previewed via
  a `Range` request; binary content is shown as a hex dump — use `save` for the
  whole object.
- Listings use the boto3 paginator (correct across >1000 keys); `summary` is
  interruptible so it never hangs on a huge bucket.
- Session output is mirrored to `outputs/` (gitignored).
