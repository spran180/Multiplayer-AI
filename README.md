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

The API listens on port 8000 and Compose starts Redis for shared state, accounts, sessions, room memberships, invites, steering locks, and pub/sub. Open `http://localhost:8000/app` to create an account and workspace. Set `PUBLIC_BASE_URL` to the externally reachable URL when sharing invites outside your machine. Set `GROQ_API_KEY` in the shell or adjacent `.env` file to enable Groq. Without it, the offline planner remains available.

## How the room works

- Register or sign in with `POST /auth/register` or `POST /auth/login`. The server issues an HttpOnly session cookie; `/auth/me` returns the current account and `/auth/logout` revokes the session.
- Create a room with authenticated `POST /rooms`; the response includes an invitation URL. Invited users must sign in, and the invite grants their account room membership.
- The browser app is served at `/app/`; `/r/{room_id}?invite=...` validates the invitation and redirects to the workspace.
- Connect to authenticated `/ws/{room_id}`. Identity comes from the session cookie, not client-supplied user IDs. Frames use the shared envelope defined in `shared/protocols`.
- Send `agent.start` to begin a run. Agent thoughts, tool calls/results, terminal status, presence, and graph updates are streamed to everyone in the room.
- `steering.acquire` returns the caller's private token. Include it in `steering.pause`, `steering.resume`, `steering.interrupt`, and `steering.inject` calls.
- `state.scrub` reads a historical checkpoint; `state.branch` forks from a checkpoint; `state.merge` joins two compatible branch tips.
- Proposed side-effect tools wait for approval from connected, eligible reviewers. In solo rooms the run starter can explicitly approve; in group rooms the starter is excluded and the configured quorum is capped to the available reviewers. If reviewers disconnect while a gate is open, its quorum shrinks to the remaining eligible voters. Rejection closes the gate and timeout fails closed.

Set `REDIS_URL` to enable cross-replica graphs, run leases, approvals, presence, steering locks, pub/sub events, accounts, sessions, and room membership. Without Redis, SQLite stores account and graph data for local single-instance development; presence, run coordination, and active tasks remain process-local. Use a persistent Redis-compatible service for production multi-instance deployments. The included demo tools do not access the host shell or filesystem.

For a public invite URL, set `PUBLIC_BASE_URL` to the HTTPS hostname that routes to this server. The default is local-only (`http://localhost:8000`), so copied invite links will not work for remote teammates until public DNS and HTTPS routing are configured. The macOS client can continue using its local API address on the host; teammates can use the browser invite URL.

### Free demo hosting on Render

The repository includes a Render Blueprint in `render.yaml`. It deploys the Docker app as one web service and uses Render's assigned HTTPS URL for room invites. The Docker entrypoint honors Render's `PORT` setting. To scale across instances or keep account and room data outside an ephemeral service filesystem, add a persistent Redis-compatible service and set `REDIS_URL` in the Render service environment. A single instance can run without Redis, but active coordination is process-local and `/tmp` data is temporary.

To deploy, push this repository to GitHub, connect it to Render, and create a Blueprint from `render.yaml`. Add `GROQ_API_KEY` in the Render service's environment settings if you want Groq-powered agent runs; without it, the mock planner is used. After deployment, open the assigned `onrender.com` URL.

Render's free web service may spin down after inactivity, and SQLite data stored under `/tmp` is lost on restarts or redeploys. Configure persistent Redis before relying on accounts and room history. GitHub Pages alone cannot host this app's Python API or WebSocket server. Room invite links grant access to signed-in users; keep them private.

### Free DuckDNS hostname

The optional `public` Compose profile uses a free DuckDNS subdomain, keeps its DNS address current, and uses Caddy to obtain HTTPS and proxy both the web app and WebSocket API. Create a subdomain and token at [DuckDNS](https://www.duckdns.org/), then set these values in the ignored root `.env` file:

```dotenv
DUCKDNS_SUBDOMAIN=your-subdomain
DUCKDNS_HOSTNAME=your-subdomain.duckdns.org
DUCKDNS_TOKEN=your-DuckDNS-token
PUBLIC_BASE_URL=https://your-subdomain.duckdns.org
```

Forward TCP ports 80 and 443 from your router to the computer running Docker, then start the public profile with `docker compose --profile public up -d`. Keep that computer online for the hostname and app to remain reachable. Room invites grant access to signed-in accounts; protect the invite URLs.
