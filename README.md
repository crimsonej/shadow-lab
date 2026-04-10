<div align="center">
  <h1>Shadow-Lab AI Provider</h1>
  <p><b>Created by <a href="https://github.com/crimsonej/crimsonej">crimsonej</a></b></p>
  <p><i>A production-grade, self-healing, zero-hardcoding toolkit to turn remote Linux servers into an OpenAI-compatible API provider.</i></p>
</div>

---

## Overview

Shadow-Lab is an advanced AI infrastructure project orchestrating headless Linux VPS/GPU instances into a cohesive AI API cluster. It interfaces directly with **Ollama** and provides a fully robust, self-healing **OpenAI-compatible** `/v1` endpoint.

**Crucial Features:**
- 🚫 **Zero Hardcoding**: Every port, IP, path, and model name is determined dynamically or by environment variables.
- ⚡ **Auto-Healing**: The server agent actively monitors CPU/RAM/GPU, and if Ollama crashes, automatically re-initializes it via `systemctl`.
- 🔑 **Built-In API Keys**: Fully functioning SQLite-backed API Key generation, revocation, and tracking system directly in the Agent.
- 🔥 **Dynamic Port Binding**: If the preferred port is in use, the agent automatically scans to find the next available port.

## Architecture Structure

```text
/project-root
  /server
    /agent/             # Python-based FastAPI Server Agent
    /installer/         # Zero-touch setup scripts (bash)
    /logs/              # Output logs and system metrics
  /client
    /dashboard/         # Next.js Front-End Control panel
```

## 1. The Server Agent (`/server/agent`)

Written in FastAPI, the agent acts as the workhorse bridging your REST API client seamlessly into the Ollama service.
It exposes fully compatible `/v1/chat/completions` and `/v1/models` endpoints so that any standard AI tool (e.g. LangChain, Flowise, Next.js AI SDK) works instantly, zero modifications needed.

### Usage Instructions

To manually run the Server Agent without using the 1-Click Installer:

```bash
# 1. Navigate to the agent directory
cd server/agent

# 2. Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install requirements
pip install -r requirements.txt

# 4. Optional: Set custom Environment variables by creating a .env file
# AGENT_PORT=8000
# AGENT_HOST=0.0.0.0
# ADMIN_KEY=my_secure_admin_password

# 5. Run the agent
python main.py
```

## 2. 1-Click Auto-Installer (`/server/installer`)

*(Coming in Phase 3)*

This script turns any raw Debian/Ubuntu/Arch system into a Shadow-Lab API Node instantly:
```bash
curl -fsSL https://github.com/crimsonej/crimsonej/agent-install.sh | bash
```

## 3. Control Center (`/client/dashboard`)

*(Coming in Phase 4)*

A Next.js dashboard that aggregates multiple Shadow-Lab server agents, enabling 1-click model distribution, API key generating, and real-time GPU metrics polling.

---

### Attribution

*This project is explicitly developed and maintained by **[crimsonej](https://github.com/crimsonej/crimsonej)** under the Shadow-Lab moniker.* 
# shadow-lab
