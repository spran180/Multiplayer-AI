# Multiplayer AI Agent

A shared agent workspace with live room presence, transferable steering, approval gates, and a persistent execution graph. FastAPI serves the WebSocket API and browser room at `/app`; room invites use `/r/{room_id}`. Redis backs multi-replica pub/sub, graph state, presence, approval votes, and run leases. The SwiftUI macOS client is also included.

## Run the backend

Python 3.11 or later is required.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn backend.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the room REST endpoints. `LLM_PROVIDER=auto` preserves the existing behavior: use Groq when `GROQ_API_KEY` is set, otherwise use the deterministic mock planner. Set `LLM_PROVIDER=groq` to explicitly choose Groq, or `LLM_PROVIDER=ollama` to run a local model.

```sh
export GROQ_API_KEY="your-key"
export GROQ_MODEL="openai/gpt-oss-120b" # optional
export LLM_PROVIDER=groq
uvicorn backend.main:app --reload
```

### Run a local open model with Ollama

Install [Ollama](https://ollama.com/download), start it, and download a model:

```sh
ollama pull qwen2.5:7b
```

Choose Ollama in the repository-root `.env` file (create it from `.env.example` if needed):

```dotenv
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_TIMEOUT_SECONDS=180
```

Then start the backend as usual. The model runs on your machine; no Groq key is needed. To use a different Ollama model, pull it first and set `OLLAMA_MODEL` to its exact tag. The agent's search tool uses a public search endpoint and therefore still needs internet access; arithmetic and verification tools run locally. No model tool can access the host shell or filesystem.

For Docker Compose, set `LLM_PROVIDER=ollama` and `OLLAMA_MODEL` in `.env`; Compose points the API container at `http://host.docker.internal:11434` by default. Ollama binds to loopback by default. To let Docker reach it on macOS, run `launchctl setenv OLLAMA_HOST "0.0.0.0:11434"` and restart Ollama. This exposes the local API on network interfaces; Ollama does not authenticate local API requests, so keep port 11434 restricted by your firewall and do not forward it from your router. Running the backend directly on macOS avoids this extra host binding step.

## Run the SwiftUI app

Requires macOS 14 or later and Swift 5.9 or later.

```sh
cd frontend
swift run
```

The app connects to `http://127.0.0.1:8000`. Set `AGENT_SERVER` to use another backend URL, for example `AGENT_SERVER=https://agent.example.com`.

## Run with Docker

```sh
docker compose up --build
```

The API listens on port 8000 and Compose starts Redis for shared state. Open `http://localhost:8000/app` to create a room. Set `PUBLIC_BASE_URL` to the externally reachable URL when sharing invites outside your machine. Set `GROQ_API_KEY` in the shell or adjacent `.env` file to enable Groq. Without it, the offline planner remains available.

## How the room works

- Create a room with `POST /rooms`; the response includes `share_url`.
- The browser app is served at `/app/`; `/r/{room_id}` redirects to the room.
- Connect to `/ws/{room_id}?user_id=...&name=...`. Frames use the shared envelope defined in `shared/protocols`.
- Send `agent.start` to begin a run. Agent thoughts, tool calls/results, terminal status, presence, and graph updates are streamed to everyone in the room.
- `steering.acquire` returns the caller's private token. Include it in `steering.pause`, `steering.resume`, `steering.interrupt`, and `steering.inject` calls.
- `state.scrub` reads a historical checkpoint; `state.branch` forks from a checkpoint; `state.merge` joins two compatible branch tips.
- Proposed side-effect tools wait for approval from connected, eligible reviewers. In solo rooms the run starter can explicitly approve; in group rooms the starter is excluded and the configured quorum is capped to the available reviewers. If reviewers disconnect while a gate is open, its quorum shrinks to the remaining eligible voters. Rejection closes the gate and timeout fails closed.

Set `REDIS_URL` to enable cross-replica state and coordination. Without Redis, SQLite persists the graph while presence and coordination remain process-local. The included demo tools do not access the host shell or filesystem.

For a public invite URL, set `PUBLIC_BASE_URL` to the HTTPS hostname that routes to this server. The default is local-only (`http://localhost:8000`), so copied invite links will not work for remote teammates until public DNS and HTTPS routing are configured. The macOS client can continue using its local API address on the host; teammates can use the browser invite URL.

### Free demo hosting on Render

The repository includes a Render Blueprint in `render.yaml`. It deploys the Docker app as one free web service and uses Render's assigned HTTPS URL for room invites. The Docker entrypoint honors Render's `PORT` setting. The service runs as one instance, so in-process WebSocket presence and coordination work without Redis.

To deploy, push this repository to GitHub, connect it to Render, and create a Blueprint from `render.yaml`. Add `GROQ_API_KEY` in the Render service's environment settings if you want Groq-powered agent runs; without it, the mock planner is used. After deployment, open the assigned `onrender.com` URL.

This free setup is for demos: Render spins down the service after 15 minutes without activity, and room history in SQLite is lost on restarts, spin-downs, or redeploys. The next visit can take about a minute to wake the service. GitHub Pages alone cannot host this app's Python API or WebSocket server. The site has no sign-in, so anyone with a room invite can join; use non-sensitive demo data.

### Free DuckDNS hostname

The optional `public` Compose profile uses a free DuckDNS subdomain, keeps its DNS address current, and uses Caddy to obtain HTTPS and proxy both the web app and WebSocket API. Create a subdomain and token at [DuckDNS](https://www.duckdns.org/), then set these values in the ignored root `.env` file:

```dotenv
DUCKDNS_SUBDOMAIN=your-subdomain
DUCKDNS_HOSTNAME=your-subdomain.duckdns.org
DUCKDNS_TOKEN=your-DuckDNS-token
PUBLIC_BASE_URL=https://your-subdomain.duckdns.org
```

Forward TCP ports 80 and 443 from your router to the computer running Docker, then start the public profile with `docker compose --profile public up -d`. Keep that computer online for the hostname and app to remain reachable. Do not publish sensitive rooms: the current service does not require sign-in, and room IDs are the access links.
