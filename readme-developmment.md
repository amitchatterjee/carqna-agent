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