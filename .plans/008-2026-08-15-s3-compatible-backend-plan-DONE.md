# Add an S3-compatible backend for the DeepAgents virtual filesystem

Status: **DONE, validated end-to-end 2026-08-22.** `src/agent/s3_backend.py` written,
`graph.py`/`pyproject.toml`/`.env.example` updated. `mypy --strict`/`ruff` clean on the new code
(`boto3-stubs[s3]` added as a dev dependency for typing). Verified live against the real RustFS
`carqna` bucket (`ls`/`glob`/binary `read`, and separately `write`/`edit`/`grep` via a local fake S3
client), then end-to-end through both `agent.carqna_cli` and `agent.copilotkit_server` with
`INSURANCE_DOCS_ROOT=s3://carqna/insurance-docs` active — both answer insurance questions correctly
from the real bucket. `tools/sync-docs.sh` (an `s3cmd sync` wrapper) added for uploading a local
folder into the bucket, replacing manual console uploads; documented in `README.md`'s new "Upload
documents with `tools/sync-docs.sh`" and "Switching the insurance docs backend to S3" sections.

Verification step 5 (permission enforcement) surfaced a real, pre-existing, **out-of-scope** finding
along the way, not a blocker to closing this plan — see its entry below for the full story: the
top-level `main_agent` has unrestricted write access to whatever backend is configured (identical gap
existed with `FilesystemBackend` before this plan; only `insurance_expert` specifically has a
`FilesystemPermission` restriction). Worth a dedicated follow-up plan given the backend is now shared
S3 infrastructure rather than a single dev machine's disk.

**Note: this plan was originally drafted against `deepagents==0.6.12`; the actually-installed version
turned out to be `0.7.5`** (re-confirmed by reading its source directly before writing any code, same
discipline the original plan used against the Gemini blueprint). Two real design points changed as a
result — both called out inline below, in Design.

Project owner added the `rustfs` service to `infrastructure/docker/docker-compose.yml` manually (out
of scope here) and has it live: bucket `carqna`, scoped access key, verified via `s3cmd` (see
`README.md`'s "One-time setup of RustFS").

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

- **`read`/`write`/`edit`** (single-object CRUD): modeled on `FilesystemBackend`'s methods.
  **Corrected from the original draft**: re-reading the actually-installed `FilesystemBackend.write`
  (`deepagents==0.7.5`) found it does **not** do an existence check — it unconditionally
  `O_TRUNC`-overwrites, and the `write_file` tool description says "Updated file", not "created file".
  (The original draft's claim of a create-vs-overwrite split came from a stale reading — worth noting
  for future work in this repo, since library behavior can legitimately change between the version a
  plan is drafted against and what's actually installed by the time it's implemented.) `S3Backend.write`
  matches this: unconditional `put_object`, create-or-overwrite, no `head_object` check — so swapping
  backends doesn't change tool behavior. `read` returns
  `ReadResult(file_data=FileData(content=..., encoding=...))`, line-sliced per `offset`/`limit` for text
  (via the shared `slice_read_response` helper) and base64-encoded for binary via the same
  `_get_backend_read_file_type` classifier `FilesystemBackend` uses (`deepagents.backends.utils`);
  `edit` fetches the object directly (not through `self.read()`, which would apply pagination), applies
  `deepagents.backends.utils.perform_string_replacement` (the same helper both shipped backends use —
  not a hand-rolled `str.replace`), and re-`put_object`s.
- **`ls`/`grep`/`glob`** (listing/search): modeled on `StateBackend`'s approach, since S3 is also a
  flat key-value store rather than a real filesystem — `StateBackend` builds a
  `dict[path, FileData]` snapshot and delegates to `grep_matches_from_files`/`_glob_search_files`
  (same helpers, confirmed via source read). `S3Backend.ls` builds `FileInfo` entries from
  `list_objects_v2(..., Delimiter="/")` directly (`CommonPrefixes` → `is_dir=True` entries, `Contents`
  → files, paginated). `grep` lists every key under the backend's root, `get_object`s each one into a
  `dict[path, FileData]`, and hands that to `grep_matches_from_files` (same known tradeoff as
  originally noted: downloads every candidate object per call — fine for the current small
  insurance-docs corpus, worth remembering if this backend is later pointed at something much larger).
  **Improvement over the original draft**: `glob` only matches on filename/path, not content
  (confirmed by reading `_glob_search_files`'s implementation — it only reads `file_data["modified_at"]`
  for sorting, never `content`), so `glob` was implemented to only list object *metadata*
  (`list_objects_v2`, no `get_object` calls at all) rather than downloading every candidate like `grep`
  does — cheaper than the original draft assumed, not just "fine for now."
- **No `delete` method.** In `deepagents==0.7.5` (unlike the `0.6.12` this was drafted against),
  `delete`/`DeleteResult` now exist on `BackendProtocol` as an *optional* method (default raises
  `NotImplementedError`). Confirmed via `deepagents.middleware.filesystem`'s
  `_unsupported_tools_and_execution_state`: a backend that doesn't implement `delete` has the `delete`
  tool automatically excluded from the visible toolset (`_supports_delete(backend)` gates it) — so
  omitting it here still cleanly results in no delete capability being exposed, not a broken/erroring
  tool. Same practical outcome as the original plan intended, more precise mechanism now confirmed.
- **No async override needed** — `boto3` is sync-only anyway; `BackendProtocol`'s default
  `aread`/`awrite`/etc. (`asyncio.to_thread(self.read, ...)`) are sufficient, same as
  `FilesystemBackend`. `grep`/`agrep` also gained an optional `max_count` kwarg in this version;
  `S3Backend.grep` accepts and forwards it to `grep_matches_from_files` (which already supports it).

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

1. **Done.** Static: `mypy --strict src/agent/s3_backend.py` clean (`boto3-stubs[s3]` added as a dev
   dependency so `boto3`/`botocore` and `list_objects_v2`'s response shape are fully typed, not just
   `Any`); `ruff check` clean. `graph.py`'s pre-existing `mypy --strict` findings (13, mostly missing
   type annotations predating this work, confirmed via `git stash` diff) are unchanged — this work
   introduced zero new findings there.
2. **Done.** With `INSURANCE_DOCS_ROOT` left as today's bare local path: `_create_insurance_backend()`
   returns `FilesystemBackend` as before, `ls('/')` against the real local insurance-docs corpus
   returned the expected files — no regression.
3. **Partially done.** `INSURANCE_DOCS_ROOT=s3://carqna` + the real `S3_*` vars against the live
   RustFS bucket: confirmed `_create_insurance_backend()` returns `S3Backend`, and `ls`/`glob`/binary
   `read` all worked correctly against the bucket's real objects (path-style addressing confirmed
   working, matching `s3cmd`'s own setup). **Not done**: an actual insurance question asked through
   the real UI with this backend active — the bucket currently only has two image files, no `.md`
   handbook uploaded, and this step is better done by the project owner alongside uploading real
   content. `write`/`edit`/`grep` were verified against a local fake S3 client instead of the real
   bucket, specifically to avoid writing permanent test objects into it (RustFS's attached IAM policy
   denies delete, so anything written there can't be cleaned up afterward) — all passed (write/
   overwrite/read-back/edit/grep-match/not-found-errors), but this is not the same as a live round-trip
   against RustFS itself.
4. **Superseded.** The original expectation ("`write` on an existing key returns an error, not a
   silent overwrite") turned out to be based on a stale reading of `FilesystemBackend` — see the
   corrected Design section above. What's actually verified instead: `write` on an existing key
   succeeds and overwrites (matching `FilesystemBackend`'s real, current behavior), confirmed via the
   fake-client test in step 3.
5. **Done, 2026-08-22 — confirmed via a live CLI run, with an important caveat found along the way.**
   Asked the agent (via `agent.carqna_cli`, `INSURANCE_DOCS_ROOT=s3://carqna/insurance-docs` active) to
   delete an existing handbook and write a new file. Delete correctly had no tool available (`S3Backend`
   doesn't implement `delete`, so DeepAgents' middleware auto-excludes that tool entirely — same
   mechanism noted in Design). **Write unexpectedly succeeded** — it created a real, permanent
   `/hacked.md` object in the bucket (RustFS's attached IAM policy blocks the scoped key from deleting
   it; removed by the project owner via the RustFS console afterward).

   Root cause, confirmed by reading `graph.py`'s `create_deep_agent(...)` call: `backend=` is attached
   at the **top level**, giving `main_agent` its own full, unrestricted filesystem toolset against the
   same shared backend — only the `insurance_agent` SubAgent has a `FilesystemPermission` block
   (`create_deep_agent(...)` itself is never given `permissions=`). The write was performed directly by
   `main_agent`, never delegated to `insurance_expert`, so that subagent's own restriction was never in
   the code path at all.

   **This is not an S3-specific regression** — the identical gap existed with `FilesystemBackend`
   before this plan (the Context section above already noted the permission is scoped to
   `insurance_agent` specifically, singular); it just hadn't been exercised live until now. What this
   verification step actually confirms, matching its original intent: `insurance_expert`'s own
   permission enforcement is unaffected by the backend swap — it's still the same middleware-level,
   backend-agnostic check either way. What it also surfaces, out of scope for this plan (see "Explicitly
   out of scope" above -- no change to `FilesystemPermission`/enforcement is part of this work) but
   worth a dedicated follow-up: `main_agent` itself has unrestricted write access to whatever backend
   is configured, which now means real shared S3 infrastructure rather than a single dev machine's
   disk -- a stronger argument for actually closing that gap than it was before.
