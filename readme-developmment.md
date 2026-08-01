## Setup langsmith
 > Create account in - smith.langchain.com

 > Create an API Key

## Create venv and activate it
```bash
python -m venv ~/langsmith.venv
source ~/langsmith.venv/bin/activate
```

## Install langgraph-cli and other tools
```bash
pip install langgraph-cli
pip install pip-tools
pip install pytest
```

## How to create a new project scaffolding
```bash
cd ~/git
langgraph new carqna-agent  --template new-langgraph-project-python
```

## Install runtime dependencies from pyproject.toml
```bash
cd ~/git/carqna-agent
python -m piptools compile pyproject.toml -o /tmp/requirements.txt
pip install -r /tmp/requirements.txt
pip install -e .
```

## Build carqna-agent docker container
```bash
cd ~/git/carqna-agent
docker compose -f ./infrastructure/docker/docker-compose.yml build carqna-dev
```

## Run docker services
```bash
docker compose -p '' -f ./infrastructure/docker/docker-compose.yml up -d
```

## Add OpenSearch users and index permissions

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


## Setup Opensearch MCP

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

# List anthropic models
```bash
curl https://api.anthropic.com/v1/models   -H "x-api-key: $ANTHROPIC_API_KEY"   -H "anthropic-version: 2023-06-01" | jq .
```

# Launch development tool
langgraph dev

## Running CarQnA (Local Interactive Agent)

CarQnA is an interactive local runner for testing the automobile help agent with persistent state management.

**Prerequisites:**
- Complete all setup steps above
- Ensure MCP config exists at `./infrastructure/conf/mcp/config.json`
- Ensure insurance documents exist (default: `./data/virtual-fs/insurance-docs`)

**Environment variables (optional):**
```bash
export CHECKPOINT_DB_PATH=./.db.sqlite3          # SQLite database for state persistence
export INSURANCE_DOCS_ROOT=./data/linux-exec/insurance-docs
export MCP_CONFIG_PATH=./infrastructure/conf/mcp/config.json
export LLM_MODEL=claude-sonnet-4-5-20250929
```

**Run the interactive agent:**
```bash
cd ~/git/carqna-agent
python -m agent.carqna
```

**Usage:**
- Ask your automobile questions (pricing, insurance, etc.)
- Type `quit`, `exit`, or `q` to exit
- State is persisted across turns using SQLite checkpointer
- Each session uses thread_id `carqna-local-session` for continuity