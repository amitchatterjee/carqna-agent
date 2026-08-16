# Add an S3-compatible backend for the DeepAgents virtual filesystem

Status: **INPROG** — plan only, implementation not started. Project owner is adding a `rustfs`
service to `infrastructure/docker/docker-compose.yml` manually (out of scope here — this plan covers
the application code only, gated so nothing breaks before that infra exists).

## Context

`insurance_expert` (`src/agent/graph.py`) currently reads its per-state insurance handbooks through
`FilesystemBackend(root_dir=INSURANCE_DOCS_ROOT, virtual_mode=True)` — a real local directory, shared
as the single `backend=` passed to `create_deep_agent(...)` for the whole graph (`main_agent`,
`car_price_expert`, `insurance_expert` all share it; `insurance_agent`'s `FilesystemPermission` is
what actually restricts it to read-only, not the backend type). The project owner is considering
swapping local disk for an S3-compatible object store (RustFS, added to docker-compose manually) so
the doc corpus isn't tied to a single host's filesystem.

A first blueprint for this (from an external LLM) was evaluated and found to target a stale/incorrect
version of `deepagents`'s `BackendProtocol` — wrong method names (`path` vs `file_path`), wrong return
types (raw `str`/`list`/`None` instead of the `WriteResult`/`ReadResult`/`LsResult`/`EditResult`/
`GrepResult`/`GlobResult` dataclasses), a `delete` method that isn't part of the protocol at all in
this version, and `grep`/`glob` stubbed to always return empty (would silently break subagent search).
This plan is written directly against the actually-installed `deepagents==0.6.12`
(`deepagents/backends/protocol.py`), confirmed by reading its source rather than assumed.

## Design

**Mechanism**: `S3Backend(BackendProtocol)`, talking to RustFS over `boto3`'s S3 client with a custom
`endpoint_url` — RustFS speaks the S3 API, so no separate client library is needed, just point `boto3`
at RustFS instead of AWS.

The two existing backend implementations split cleanly along how they model file identity, and
`S3Backend` borrows from both rather than either alone:

- **`read`/`write`/`edit`** (single-object CRUD): modeled on `FilesystemBackend`'s methods — `write`
  does a `head_object` existence check first and returns `WriteResult(error=...)` (not overwrite) if
  the key already exists, matching the write-creates/edit-modifies split the agent's tools depend on;
  `read` returns `ReadResult(file_data=FileData(content=..., encoding=...))`, line-sliced per
  `offset`/`limit` for text and base64-encoded for binary via the same `_get_file_type` helper
  `FilesystemBackend` already uses (`deepagents.backends.utils`); `edit` reads via its own `read`,
  applies `deepagents.backends.utils.perform_string_replacement` (the same helper both shipped
  backends use — not a hand-rolled `str.replace`), and re-`put_object`s.
- **`ls`/`grep`/`glob`** (listing/search): modeled on `StateBackend`'s approach, since S3 is also a
  flat key-value store rather than a real filesystem — `StateBackend` builds a
  `dict[path, FileData]` snapshot and delegates to `grep_matches_from_files`/`_glob_search_files`
  (same helpers, confirmed via source read). `S3Backend.ls` builds `FileInfo` entries from
  `list_objects_v2(..., Delimiter="/")` directly (`CommonPrefixes` → `is_dir=True` entries, `Contents`
  → files); `grep`/`glob` list matching keys, `get_object` each one into a `dict[path, FileData]`, and
  hand that to the same shared helpers `StateBackend` uses.
  **Known tradeoff**: this downloads every candidate object on each `grep`/`glob` call — fine for the
  current insurance-docs corpus (a few dozen small per-state handbooks) but worth remembering if this
  backend is later pointed at something much larger.
- **No `delete` method** — not part of `BackendProtocol` in this version; omit rather than adding dead
  code the agent's tools never call.
- **No async override needed** — `boto3` is sync-only anyway; `BackendProtocol`'s default
  `aread`/`awrite`/etc. (`asyncio.to_thread(self.read, ...)`) are sufficient, same as
  `FilesystemBackend`.

**New file `src/agent/s3_backend.py`**: `S3Backend(BackendProtocol)` as above, constructed with
`bucket`, `prefix`, `endpoint_url`, `access_key`, `secret_key`, `region`. `prefix` (may be `""`) is
prepended to every key the backend builds/matches, so a single bucket can host multiple logical roots
(e.g. `s3://carqna/insurance-docs` vs. some other prefix later) without needing a dedicated bucket per
use case. Path-style addressing (`Config(s3={"addressing_style": "path"})`) is typically required for
self-hosted S3-compatible stores like RustFS/MinIO (they don't do virtual-hosted-style bucket DNS) —
confirmed against the live RustFS setup in `readme-developmment.md`'s "One-time setup of RustFS"
(`addressing_type = path` in its `s3cmd` config works there).

**`src/agent/graph.py`**: rather than a separate on/off switch env var, reuse `INSURANCE_DOCS_ROOT`
itself as a URI and dispatch on its scheme — this way there's exactly one setting that says both
*which* backend and *where*, and it can never disagree with itself the way a separate
`INSURANCE_DOCS_BACKEND` + `INSURANCE_DOCS_ROOT` pair could (e.g. backend set to `s3` while the root
still points at a stale local path). `urllib.parse.urlsplit` is stdlib, no new dependency:

```python
def _create_insurance_backend() -> BackendProtocol:
    docs_root = os.getenv("INSURANCE_DOCS_ROOT", default_filesystem_root)
    parsed = urllib.parse.urlsplit(docs_root)

    if parsed.scheme == "s3":
        # s3://<bucket>/<prefix...> -- bucket from netloc, everything after
        # the first "/" is an optional key prefix within that bucket.
        return S3Backend(
            bucket=parsed.netloc,
            prefix=parsed.path.lstrip("/"),
            endpoint_url=os.environ["S3_ENDPOINT_URL"],
            access_key=os.environ["S3_ACCESS_KEY_ID"],
            secret_key=os.environ["S3_SECRET_ACCESS_KEY"],
            region=os.getenv("S3_REGION", "us-east-1"),
        )

    # "file:///abs/path" or a bare path (no scheme, e.g. "~/foo" or
    # "./insurance_docs") -- bare-path form kept working as-is so today's
    # .env files don't need to change. "~" only expands in the bare-path
    # form: file:// URIs are taken as literal paths per normal URI
    # semantics, so use an absolute file:// path, not file://~/....
    filesystem_root = os.path.expanduser(
        parsed.path if parsed.scheme == "file" else docs_root
    )
    if not os.path.exists(filesystem_root):
        raise RuntimeError(f"Filesystem root not found: {filesystem_root}")
    return FilesystemBackend(root_dir=filesystem_root, virtual_mode=True)
```

`insurance_agent`'s `FilesystemPermission` block is backend-agnostic (a path-based permission layer
enforced by DeepAgents' middleware, not something `FilesystemBackend` itself implements) — no change
needed there regardless of which backend is active.

**`pyproject.toml`**: add `boto3` as a dependency.

**`.env.example`**: `INSURANCE_DOCS_ROOT` itself now selects the backend via scheme; the `S3_*` vars
supply the connection details a URI can't carry (endpoint/credentials — RustFS isn't AWS, so there's
no implicit default endpoint the way real S3 has). Names are S3-protocol-generic, not RustFS-specific,
since `boto3` talks to any S3-compatible endpoint the same way:
```
# Insurance docs root -- bare path or file:// URI for local filesystem (default,
# unchanged from today), or s3://<bucket>/<prefix> for an S3-compatible object
# store (e.g. RustFS via docker-compose). See
# .plans/008-2026-08-15-s3-compatible-backend-plan.
INSURANCE_DOCS_ROOT=~/git/knowledgexpert/data/linux-exec/insurance-docs
# INSURANCE_DOCS_ROOT=s3://carqna/insurance-docs

# Only read when INSURANCE_DOCS_ROOT uses the s3:// scheme above.
S3_ENDPOINT_URL=http://localhost:9000
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_REGION=us-east-1
```

## Explicitly out of scope

- `rustfs` docker-compose service and bucket creation/population — project owner is handling
  infrastructure directly.
- Any change to `FilesystemPermission`/the read-only enforcement on `insurance_expert`.
- `CompositeBackend` prefix-routing (e.g. S3 for durable docs, in-memory for scratch files) — only one
  backend is in use today (shared across the whole graph); revisit if/when a second, ephemeral use
  case actually appears.
- Migrating any other data (checkpoints, `user_registry`, sessions — all Postgres, unrelated) to S3.
- Async-native S3 client (`aioboto3`) — sync `boto3` + the protocol's default `asyncio.to_thread`
  wrapping is sufficient; nothing here is high-throughput enough to need it.
- Test suite — no tests exist for any backend today; not adding a first one as part of this plan.

## Verification

1. Static: confirm `S3Backend` type-checks against the installed `BackendProtocol` (`mypy --strict`),
   and that its return types match exactly (`WriteResult`/`ReadResult`/etc., not raw values).
2. With `INSURANCE_DOCS_ROOT` left as today's bare local path (no scheme): confirm the agent behaves
   exactly as today — no regression from adding the new scheme-dispatch code path.
3. Once RustFS is up and the `carqna` bucket has at least one uploaded handbook (already true per
   `readme-developmment.md`'s "One-time setup of RustFS"): set `INSURANCE_DOCS_ROOT=s3://carqna` (or
   `s3://carqna/<prefix>` if docs are placed under a prefix) + the `S3_*` vars, start the backend, and
   ask an insurance question through the real UI — confirm `insurance_expert` can `ls`/`read`/`grep`
   the bucket and answers correctly.
4. Confirm `write` on an existing key returns an error (not a silent overwrite) — matches
   `FilesystemBackend`'s contract.
5. Confirm `insurance_expert`'s write/delete `FilesystemPermission` denial still applies against the
   S3 backend (i.e. permission enforcement is genuinely backend-agnostic, not accidentally coupled to
   `FilesystemBackend`).
