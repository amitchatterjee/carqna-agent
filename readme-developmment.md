Setup langsmith
 > Create account in - smith.langchain.com

 > Create an API Key

Create venv and activate it
```bash
python -m venv ~/langsmith.venv
source ~/langsmith.venv/bin/activate
```

Install langgraph-cli and other tools
```bash
pip install langgraph-cli
pip install pip-tools
```

Create a new project
```bash
cd ~/git
langgraph new test-langgraph  --template new-langgraph-project-python
```

Install runtime dependencies
```bash
cd ~/git/test-langgraph
python -m piptools compile pyproject.toml -o /tmp/requirements.txt
pip install -r /tmp/requirements.txt
pip install -e .
```

Run docker services
TODO

List anthropic models
```bash
curl https://api.anthropic.com/v1/models   -H "x-api-key: $ANTHROPIC_API_KEY"   -H "anthropic-version: 2023-06-01" | jq .
```

Launch development tool
langgraph dev

## Running CarQnA (Local Interactive Agent)

CarQnA is an interactive local runner for testing the automobile help agent with persistent state management.

**Prerequisites:**
- Complete all setup steps above
- Ensure MCP config exists at `~/.knowledgexpert/conf/mcp/config.json`
- Ensure insurance documents exist (default: `~/git/knowledgexpert/data/linux-exec/insurance-docs`)

**Environment variables (optional):**
```bash
export CHECKPOINT_DB_PATH=./.db.sqlite3          # SQLite database for state persistence
export INSURANCE_DOCS_ROOT=~/git/knowledgexpert/data/linux-exec/insurance-docs
export MCP_CONFIG_PATH=~/.knowledgexpert/conf/mcp/config.json
export LLM_MODEL=claude-sonnet-4-5-20250929
```

**Run the interactive agent:**
```bash
cd ~/git/test-langgraph
python -m agent.carqna
```

**Usage:**
- Ask your automobile questions (pricing, insurance, etc.)
- Type `quit`, `exit`, or `q` to exit
- State is persisted across turns using SQLite checkpointer
- Each session uses thread_id `carqna-local-session` for continuity