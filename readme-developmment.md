## Getting started with Langchain/Langgraph/Langsmith,copilot UI

### Create langsmith account and API key
 > Create account in - smith.langchain.com
 > Create an API Key

### Create a venv and activate it
```bash
python3.13 -m venv ~/carqna.venv
source ~/carqna.venv/bin/activate
```

### Install sqlite3
This is needed if you want to examine the content of the sqlite3 databases that is used by the carqna_cli
```bash
sudo dnf install sqlite
```

### Install npm
```bash
sudo dnf install nodejs
```

### Install uv
`uv` (https://docs.astral.sh/uv/) drives dependency installation from here on — check it's
available:
```bash
uv --version
```
(installed system-wide already on most dev boxes; see astral's install docs if not.)

### Create a new agent application (example: carqna-agent)
```bash
cd ~/git
langgraph new carqna-agent  --template new-langgraph-project-python
```

### Install runtime + dev dependencies from pyproject.toml / uv.lock
```bash
cd ~/git/carqna-agent
uv sync --active
```
`--active` installs into the already-activated `~/carqna.venv` instead of `uv`'s own project-local
`.venv` (which is `uv sync`'s default, and would silently ignore the venv you just created/activated
above). This reads the pinned versions straight from `uv.lock`.

Whenever `pyproject.toml`'s dependencies change, re-run `uv lock` and commit the updated `uv.lock` —
it's checked into git specifically so everyone (and every fresh venv) resolves the identical
dependency graph instead of drifting apart over time.

# Launch application in development mode
```bash
cd ~/git/carqna-agent
langgraph dev
```

## CarQnA application

### Build infrastructure containers
```bash
cd ~/git/carqna-agent
docker compose -p '' -f ./infrastructure/docker/docker-compose.yml build
```

### Run infrastructure services
```bash
cd ~/git/carqna-agent
docker compose -p '' -f ./infrastructure/docker/docker-compose.yml up -d
```
Brings up `opensearch` and `postgres` (checkpointer storage for multi-turn conversation state).
Postgres needs no manual bootstrap — `infrastructure/docker/postgres/initdb.d/init_user.sh` creates
the `convmem` user/database automatically on first start.

### One-time setup of Opensearch

#### Add OpenSearch users and index permissions

Use the ndjson fixtures under `./infrastructure/admin/opensearch` to create users and bind them to index-scoped roles.

```bash
# Create or update roles
while IFS= read -r payload || [ -n "$payload" ]; do
  role_name=$(printf '%s' "$payload" | jq -r '.name')
  role_body=$(printf '%s' "$payload" | jq 'del(.name)')
  curl -k -u 'admin:openSearch$2025' \
    -H 'Content-Type: application/json' \
    -X PUT "https://localhost:9200/_plugins/_security/api/roles/$role_name" \
    --data-binary "$role_body"
done < "./infrastructure/admin/opensearch/roles.ndjson"

# Create or update internal users
while IFS= read -r payload || [ -n "$payload" ]; do
  user_name=$(printf '%s' "$payload" | jq -r '.name')
  user_body=$(printf '%s' "$payload" | jq 'del(.name)')
  curl -k -u 'admin:openSearch$2025' \
    -H 'Content-Type: application/json' \
    -X PUT "https://localhost:9200/_plugins/_security/api/internalusers/$user_name" \
    --data-binary "$user_body"
done < "./infrastructure/admin/opensearch/users.ndjson"

# Map users to roles
while IFS= read -r payload || [ -n "$payload" ]; do
  role_name=$(printf '%s' "$payload" | jq -r '.name')
  mapping_body=$(printf '%s' "$payload" | jq 'del(.name)')
  curl -k -u 'admin:openSearch$2025' \
    -H 'Content-Type: application/json' \
    -X PUT "https://localhost:9200/_plugins/_security/api/rolesmapping/$role_name" \
    --data-binary "$mapping_body"
done < "./infrastructure/admin/opensearch/rolesmapping.ndjson"
```

The example fixtures create two users:
- `alice` can read indices matching `msrp-*`
- `bob` can read and write indices matching `msrp-*`


#### Build Opensearch MCP content

```bash
# Clear existing msrp index data. Only execute this when you want to clear the data, the commands below this one will upsert the content if the content already exists
curl -k -u 'admin:openSearch$2025' -X DELETE "https://localhost:9200/msrp?ignore_unavailable=true"

# Load msrp index
curl -sS -H "Content-Type: application/x-ndjson" \
  -u 'bob:X5@mD8!zH3#uC1%w' \
  --data-binary @"$KNOWLEDGEXPERT_HOME/data/opensearch/msrp/toyota-2025-msrp-bulk.ndjson" \
  --insecure \
  "https://localhost:9200/_bulk"

curl -k -X PUT "https://localhost:9200/msrp/_mapping" \
  -H "Content-Type: application/json" \
  -u 'admin:openSearch$2025' \
  --data-binary @"$KNOWLEDGEXPERT_HOME/data/opensearch/msrp/msrp-mappings.json"

```


#### Setup Opensearch MCP

```bash
# get available plugins
curl -X GET 'https://localhost:9200/_cat/plugins?v' --insecure -u 'admin:openSearch$2025'

# get cluster settings
curl -X GET "https://localhost:9200/_cluster/settings" -u 'admin:openSearch$2025' --insecure

# create agents
curl --insecure \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @"./infrastructure/conf/mcp/opensearch/agent.ndjson" \
  "https://localhost:9200/_plugins/_ml/agents/_register" \
  -u 'admin:openSearch$2025'

# register tools
curl -X POST 'https://localhost:9200/_plugins/_ml/mcp/tools/_register' \
  --insecure \
  -u 'admin:openSearch$2025' \
  -H 'Content-Type: application/json' \
  --data-binary @"./infrastructure/conf/mcp/opensearch/mcp-tools.json"

# verify Alice can read the current msrp index
curl -sS \
  --insecure \
  -u 'alice:N7!qL2#vP9@tR4$k' \
  "https://localhost:9200/msrp/_search?size=1"

```

The AutoGeek MCP server uses an `Authorization` header in [infrastructure/conf/raven/mcp.json](infrastructure/conf/raven/mcp.json); that header is configured with Alice's credentials for the MCP endpoint.


### One-time setup of environment variables
```bash
cd ~/git/carqna-agent
cp .env.example to .env
vi .env
  # Update the API keys, etc. as needed
```

### One-time setup of Auth0/Okta

CarQnA uses Auth0 (Okta) for OAuth2/OIDC login: `carqna-copilot-ui` redirects users to Auth0 to log
in, and `carqna-agent` verifies the resulting access token on every request. See
`.plans/004-2026-08-09-oauth2-okta-auth-plan-DONE.md` for the full design and
`.plans/005-2026-08-10-user-tracking-plan-DONE.md` for how user identity gets tracked afterward.

1. **Create an Application** (Auth0 dashboard → Applications → Applications → Create Application):
   - Name: `carqna`
   - Application Type: `Regular Web Application`
   - Settings tab:
     - Allowed Callback URLs: `http://localhost:3000/auth/callback`
     - Allowed Logout URLs: `http://localhost:3000`

2. **Create a database connection** (Auth0 dashboard → Authentication → Database → Create DB
   Connection):
   - Name: `Username-Password-Authentication`, otherwise leave the defaults as-is.
   - Associate it with the Application: open the connection's settings, go to its **Applications
     tab**, and select `carqna`. Without this, `carqna` has no connection to actually authenticate
     users against.

3. **Create an API** (Auth0 dashboard → Applications → APIs → Create API):
   - Name: `carqna`
   - Identifier: `https://carqna-agent/api`
   - **Application Access tab**: grant `carqna` (the Application from step 1) access. Easy to miss —
     without it, login fails at the callback with `Client "..." is not authorized to access resource
     server "https://carqna-agent/api"` (an `invalid_request` OAuth2Error), even though the
     API/audience itself exists and every env var is already correct.

4. **Add a user** (Auth0 dashboard → User Management → Users → Create User):
   - Settings tab: email, name, etc.
   - Authorized Applications tab: add `carqna` as an authorized application for this user.

#### Environment variables

Both repos need Auth0 values, but split by role: `carqna-agent` only *verifies* tokens (resource
server), while `carqna-copilot-ui` is what actually performs the login/token-exchange flow (client).
Copy each repo's `.env.example` (`cp .env.example .env` / `cp .env.example .env.local`) and fill in:

**`carqna-agent/.env`**:

| Variable         | Where to find it                                              |
|------------------|-----------------------------------------------------------------|
| `AUTH0_DOMAIN`   | Application (step 1) → Settings tab → Domain                    |
| `AUTH0_AUDIENCE` | The API Identifier from step 2 (`https://carqna-agent/api`)     |

**`carqna-copilot-ui/.env.local`**:

| Variable              | Where to find it                                            |
|-----------------------|---------------------------------------------------------------|
| `AUTH0_DOMAIN`         | Same tenant domain as above                                   |
| `AUTH0_CLIENT_ID`      | Application (step 1) → Settings tab → Client ID                |
| `AUTH0_CLIENT_SECRET`  | Application (step 1) → Settings tab → Client Secret             |
| `AUTH0_SECRET`         | Generate with `openssl rand -hex 32`                          |
| `APP_BASE_URL`         | `http://localhost:3000`                                       |
| `AUTH0_AUDIENCE`       | Must match `carqna-agent`'s `AUTH0_AUDIENCE` exactly            |

### Running CarQnA from the web interface

#### Agent
```bash
cd ~/git/carqna-agent
python -m agent.copilotkit_server
```

#### UI
```bash
cd ~/git/carqna-copilot-ui
npm run dev
```

#### Access the user interface
Using a browser, open URL - http://localhost:3000 . Login using OKTA assigned user/password, if prompted. 

To logout, open URL - http://localhost:3000/auth/logout

### Running CarQnA using cli

#### Agent
```bash
cd ~/git/carqna-agent
python -m agent.carqna_cli --session <name>
```
`--session` is mandatory. If a session with that name already exists, the CLI continues it; otherwise
it creates a new one. Unlike the web UI (which uses the shared `convmem` Postgres database), the CLI
stores everything locally in `~/.carqna/carqna_cli.sqlite` (override with `CARQNA_CLI_SQLITE_PATH`) —
no Postgres involved, and no collision risk between different people running the CLI against the same
shared Postgres instance, since there's no shared resource at all. See
`.plans/006-2026-08-11-session-management-plan-INPROG.md` for the full design.

## Miscellaneous utilities
### List anthropic models
```bash
curl https://api.anthropic.com/v1/models   -H "x-api-key: $ANTHROPIC_API_KEY"   -H "anthropic-version: 2023-06-01" | jq .
```

### Access the `convmem` Postgres database
```bash
PGPASSWORD=convmem psql -h localhost -U convmem -d convmem
```
List tables from inside `psql` with `\dt`.

#### `checkpoints`, `checkpoint_writes`, `checkpoint_blobs`, `checkpoint_migrations`

LangGraph's own tables (`AsyncPostgresSaver`, created/migrated automatically by
`checkpointer.setup()` on every startup — never hand-edit these). Together they hold the full
multi-turn conversation state for every thread:

- `checkpoints` — one row per saved graph step. Key columns: `thread_id` (see below),
  `checkpoint_ns`, `checkpoint_id`, `parent_checkpoint_id` (links steps into a history chain),
  `checkpoint`/`metadata` (`jsonb` — the actual serialized graph state).
- `checkpoint_writes` — pending/intermediate channel writes within a step (`thread_id`,
  `checkpoint_id`, `task_id`, `channel`, `blob`).
- `checkpoint_blobs` — larger serialized channel values stored separately from `checkpoints.checkpoint`
  (`thread_id`, `channel`, `version`, `blob`).
- `checkpoint_migrations` — single `v` column, LangGraph's own internal schema-version marker.

**`thread_id` is `{auth0_user_id}:{client_supplied_thread_id}`** (e.g.
`auth0|6a78d5504c69cc8f16465b81:61d7d9f3-68b3-4bcf-aeff-f15b6e2a79cb`) — built server-side in
`copilotkit_server.py`'s `POST /` route from the verified JWT `sub` claim, never trusted from the
client. This is what makes one user structurally unable to read/continue another user's conversation
even if they somehow learned the raw thread id. See
`.plans/004-2026-08-09-oauth2-okta-auth-plan-DONE.md`. Rows with a bare UUID or names like
`debug-*`/`carqna-local-session` predate this and are orphaned pre-auth test data.

Useful query — list a given user's threads:
```sql
SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE 'auth0|6a78d5504c69cc8f16465b81:%';
```

#### `user_registry`

Created by `infrastructure/docker/postgres/initdb.d/users_registry.sh` (not by application code — see
`.plans/005-2026-08-10-user-tracking-plan-DONE.md`). Maps the same opaque `auth0|...` id used in
`checkpoints.thread_id` to a human identity, fetched from Auth0's `/userinfo` endpoint the first time
each user is ever seen:

| Column           | Meaning                                                              |
|------------------|-----------------------------------------------------------------------|
| `id`             | Auto-increment surrogate primary key — what other tables' foreign keys reference (e.g. `user_sessions.user_registry_id`), not `user_id` |
| `user_id`        | JWT `sub` claim (unique, not the primary key) — matches the prefix in `checkpoints.thread_id` |
| `email`          | From Auth0's `/userinfo`                                              |
| `name`           | From Auth0's `/userinfo` (falls back to the email string if no separate display name is set) |
| `first_seen_at`  | First authenticated request from this user                            |
| `last_seen_at`   | Updated on every authenticated request                                |

This is groundwork for the still-deferred multi-session picker feature (listing/switching between a
user's own named conversations, like Claude Code) — not itself surfaced in the UI yet.

### Access the CLI's local SQLite database

`carqna_cli.py` doesn't use Postgres at all — it's entirely local (see
`.plans/006-2026-08-11-session-management-plan-INPROG.md`). The `sqlite3` CLI (see "Install sqlite3"
above) is the direct equivalent of `psql` for this:

```bash
sqlite3 ~/.carqna/carqna_cli.sqlite
```

List tables from inside the shell with `.tables`, or run one-shot queries without entering it:
```bash
sqlite3 ~/.carqna/carqna_cli.sqlite "SELECT * FROM sessions;"
```

#### `checkpoints`, `writes`

LangGraph's own tables (`AsyncSqliteSaver` — same conceptual schema as the Postgres path's
`checkpoints`/`checkpoint_writes`, just fewer of them and SQLite-native types; created automatically on
first read/write, no explicit setup call needed unlike Postgres). `thread_id` here is just the local
session's `session_id` as a string (e.g. `"1"`) — no user namespacing, since the file itself is already
scoped to one person's machine.

#### `sessions`

Created on the fly by `carqna_cli.py` itself (not via an infra init script — see the CLI section of
`.plans/006-...-INPROG.md` for why this is a deliberate difference from the Postgres-backed
`user_sessions` table):

| Column         | Meaning                                                        |
|----------------|-------------------------------------------------------------------|
| `session_id`   | Autoincrement primary key — matches `checkpoints.thread_id`       |
| `session_name` | The `--session` value passed on the command line (unique)         |
| `created_at`   | When this session was first created                               |
