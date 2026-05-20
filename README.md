<div align="center">
  <h1>🐾 LoMoBot 🏡</h1>
  <p><b>Lo</b>cal <b>Mo</b>del <b>Bot</b> — Slim and transparent AI assistant for fully local deployment</p>
  <p>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

## What is LoMoBot?

**LoMoBot** = **Lo**cal **Mo**del **Bo**t — a personal AI assistant designed for fully local deployment.

LoMoBot is a **stripped-down, fully local** version of [nanobot](https://github.com/HKUDS/nanobot). It cuts out all complex cloud-dependent logic and keeps only what's needed to run a personal AI assistant entirely on your own hardware.

**No cloud API required.** Connect to local LLMs via Ollama, vLLM, or any OpenAI-compatible endpoint.

**Transparency** — every interaction between the Agent and the LLM is visible.

**Channel support:** Telegram (tested) and WhatsApp (ported from nanobot, untested).



## Who Is It For?

1. **GPU-constrained users** who want everything running on limited local hardware without cloud dependencies.

2. **Believers in small models** — you think a 9B–30B agentic model can outperform cloud LLMs with the right prompts and instructions.

3. **Curious tinkerers** who want to understand what’s happening between the LLM and the agent under the hood.

4. **Telegram users** looking for a fully local AI assistant.



## Quick Start

### 1. Install

```bash
git clone <your-repo>/lomobot.git
cd lomobot
python3 -m venv venv
source venv/bin/activate
pip install -e .


```



### 2. Configure (`~/.lomobot/config.json`)


Running `lomobot onboard` creates the `.lomobot` directory and a `config.json` file.

```bash
lomobot onboard
```

Then update `config.json` with your settings.

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.lomobot/workspace",
      "max_tool_iterations": 20
    }
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allow_from": ["YOUR_TELEGRAM_USERNAME"]
    }
  },
  "providers": {
    "master": {
      "api_key": "no-key",
      "api_base": "http://localhost:11434/v1",
      "model": "qwen3.6-27b",
      "max_tokens": 32768,
      "temperature": 0.7
    }
  },
  "tools": {
    "web": {
      "search": {
        "api_key": "",
        "max_results": 5
      }
    }
  }
}
```

### 3. Run

```bash
lomobot gateway
```



## Configuration

### Providers

LoMoBot uses OpenAI-compatible APIs. Connect to any local or remote provider:

| Provider | api_base | api_key |
|---|---|---|
| **Ollama** | `http://localhost:11434/v1` | `no-key` |
| **vLLM** | `http://localhost:8000/v1` | `no-key` |
| **LM Studio** | `http://localhost:1234/v1` | `no-key` |
| **OpenRouter** | `https://openrouter.ai/api/v1` | your key |

### Telegram

| Field | Description |
|---|---|
| `token` | Bot token from @BotFather |
| `allow_from` | List of allowed usernames or user IDs. Empty `[]` = allow all |

### Web search

A Brave Search API key is required for Brave Search.

DuckDuckGo is used as a fallback when the search `api_key` is left empty.


### Tools

Built-in tools available to the agent:

| Tool | Description |
|---|---|
| `read_file` | Read file contents |
| `write_file` | Create or overwrite files |
| `edit_file` | Edit file content |
| `list_dir` | List directory contents |
| `exec` | Execute shell commands |
| `web_search` | Search the web (Brave Search)/DuckDuckGo for failover |
| `web_fetch` | Fetch and extract web page content |
| `message` | Send messages through channels |



## Project Structure

```
lomobot/
├── agent/          # Core agent logic
│   ├── loop.py     # Agent loop (LLM ↔ tool execution)
│   ├── context.py  # Prompt builder
│   ├── memory.py   # Session memory
│   └── tools/      # Built-in tools
├── channels/       # Telegram
├── bus/            # Message routing
├── cron/           # Scheduled tasks
├── providers/      # LLM provider (OpenAI-compatible)
├── session/        # Conversation sessions
├── config/         # Configuration schema
├── cli/            # CLI commands
└── utils/          # Helper functions
```

## Credits

Derived from [nanobot](https://github.com/HKUDS/nanobot) v0.1.3. LoMoBot removes cloud dependencies and complex features to focus on local-first deployment.
