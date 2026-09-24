<div align="center">
  <h1>🐾 LoMoBot 🏡</h1>
  <p><b>Lo</b>cal <b>Mo</b>del <b>Bot</b> — Slim and transparent AI assistant for fully local deployment</p>
  <p>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>



## 📋 Changelog

### v0.0.3-alpha (2026-09-24)
- Added MQTT channel (Fireside Chat webim protocol): TLS, media frames,
- Added paho-mqtt dependency
- Fix minor bugs

### v0.0.2-alpha (2026-05-31)
- Added Vision support
- Added multiple provider in config file for future development

### v0.0.1-alpha-1 (2026-05-24)
- Added detailed MEMORY debug message type
- Fixed web_fetch dependency (lxml-html-clean)
- Fixed web_search DuckDuckGo library rename (ddgs)

### v0.0.1-alpha (2026-05-20)
- Initial release from Nanobot



## What is LoMoBot?

**LoMoBot** = **Lo**cal **Mo**del **Bo**t — a personal AI assistant designed for fully local deployment.

LoMoBot is a **stripped-down, fully local** version of [nanobot](https://github.com/HKUDS/nanobot). It cuts out all complex cloud-dependent logic and keeps only what's needed to run a personal AI assistant entirely on your own hardware.

**No cloud API required.** Connect to local LLMs via Ollama, vLLM, or any OpenAI-compatible endpoint. For a minimal running setup, LoMoBot has been tested with [Ternary Bonsai 2 27B](https://prismml.com/news/bonsai-2-27b)
(`Ternary-Bonsai-2-27B-PTQ1_0` — 5.9 GB at 1.76 effective bits/weight, 262K-token
context, Apache 2.0) served through an OpenAI-compatible endpoint, completing
multiple real programming and system-administration tasks on this model.
Note: PTQ1_0 checkpoints require the [PrismML llama.cpp fork](https://github.com/PrismML-Eng/llama.cpp),
not stock llama.cpp.

**Transparency** — every interaction between the Agent and the LLM is visible.

**Channel support:** Telegram (tested), WhatsApp (ported, tested), and MQTT (Fireside Chat webim protocol — tested in production).



## Who Is It For?

1. **Believers in local LLM deployment** who want everything running on limited local hardware without cloud dependencies.

2. **Minimalists** who prefer a simple, single-agent architecture over complex multi-agent or sub-agent systems.

3. **Curious tinkerers** who want to understand what's happening between the LLM and the agent under the hood.

4. **Telegram users** — or anyone running an MQTT-based agent mesh — looking for a fully local AI assistant.



## Quick Start

### 1. Install

```bash
git clone <your-repo>/lomobot.git
cd lomobot
python3 -m venv venv
source venv/bin/activate
pip install .


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
    },
    "mqtt": {
      "enabled": false,
      "broker_host": "mqtt.example.com",
      "broker_port": 8883,
      "tls": true,
      "username": "ag_mybot",
      "password": "YOUR_MQTT_PASSWORD",
      "countersign": "YOUR_16_CHAR_SECRET",
      "allow_from": []
    }
  },
  "providers": {
    "master": {
      "api_key": "no-key",
      "api_base": "http://localhost:11434/v1",
      "model": "qwen3.8-27b",
      "max_tokens": 32768,
      "temperature": 0.4
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

### MQTT

Connects to an MQTT broker using the Fireside Chat webim payload format.

| Field | Description |
|---|---|
| `broker_host` / `broker_port` | MQTT broker address (TLS typically 8883) |
| `tls` | Enable TLS with `CERT_REQUIRED` |
| `username` / `password` | Broker credentials |
| `countersign` | 16-char secret; required on `am/` agent-to-agent messages |
| `allow_from` | Allowed senders; empty `[]` = allow all |

Behavior notes:

- Media: inbound/outbound images and files via Fireside Chat binary frames (≤2MB image / ≤10MB file)
- Broker reload only blocks *new* connections — running clients keep old credentials until reconnect

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
├── channels/       # Telegram, WhatsApp, MQTT
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
