## Getting started with Langchain/Langgraph/Langsmith,copilot UI

### Create langsmith account and API key
 > Create account in - smith.langchain.com
 > Create an API Key

### Create a venv and activate it
```bash
python -m venv ~/langsmith.venv
source ~/langsmith.venv/bin/activate
```

### Install npm
```bash
sudo dnf install nodejs
```

### Install langgraph-cli and other tools
```bash
pip install langgraph-cli
pip install pip-tools
pip install pytest
```

### Create a new agent application (example: carqna-agent)
```bash
cd ~/git
langgraph new carqna-agent  --template new-langgraph-project-python
```

### Install runtime dependencies from pyproject.toml
```bash
cd ~/git/carqna-agent
# Extract requirements from the toml
python -m piptools compile pyproject.toml -o /tmp/requirements.txt
pip install -r /tmp/requirements.txt
pip install -e .
```

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
http://localhost:3000

### Running CarQnA using cli

#### Agent
```bash
cd ~/git/carqna-agent
python -m agent.carqna_cli
```

## Miscellaneous utilities
### List anthropic models
```bash
curl https://api.anthropic.com/v1/models   -H "x-api-key: $ANTHROPIC_API_KEY"   -H "anthropic-version: 2023-06-01" | jq .
```
