# AgentLink — AI Agent Communication Bridge
*Prototype middleware for observable, auditable agent-to-agent communication.*

**Honesty note:** agents did NOT invent a language. AgentLink uses a designed protocol (**AL/1**) and an *experimental, proof-of-concept* audio layer. The Prototype Security Layer is not production-grade.

## Flow
Human → Agent A (LLM → JSON) → AgentLink broker (validate, permission check, message ID, AL/1 frame) → **audio encode → you hear it → audio decode** → Agent B (works only from the *decoded* frame, LLM) → AgentLink → audio → decode → Human.

## Structure
    backend/main.py     FastAPI: agents, broker, permissions, registry, metrics, SQLite, LLM calls (key stays server-side)
    frontend/index.html Dashboard, pipeline animation, audio encoder/decoder (Web Audio), registry, gateway, metrics
    database/           SQLite file (created on first run)
    .env.example · requirements.txt

## Install & run
    python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    cp .env.example .env        # then edit .env: LLM_PROVIDER=openai|anthropic, LLM_API_KEY=...
    uvicorn backend.main:app --reload
Open http://localhost:8000 . The key is only read by the backend; the browser never sees it.

## Audio protocol (AL/1-audio)
Frame text → UTF-8 bytes + 1-byte checksum → hex nibbles. Each nibble = one 20 ms tone from 16 frequencies (800 Hz + n·160 Hz). Header = 4 tones at 600 Hz, end = 2 tones at 3600 Hz. Decoding uses the Goertzel algorithm per 20 ms window; checksum verifies integrity. Sample rate 22.05 kHz, generated in-browser. Timing is known (no clock recovery), so it works on the generated signal, not over a microphone/speaker air gap.
Limit: audio carries a compact frame (task ≤100 chars, response summary ≤90 chars); the full answer is displayed from the broker.

## Components (for your supervisor)
- **Agent A** — turns natural language into structured JSON (intent, priority, action, task).
- **AgentLink broker** — validates, permission-checks, assigns message ID, builds the AL/1 frame, runs the audio layer, logs everything.
- **Agent B** — receives only the decoded frame, answers via the LLM (no real dataset; findings are illustrative).
- **Registry** — searchable agent directory (SQLite). **Gateway** — routing/validation view + permission tests (ACCESS DENIED demo).
- **Semantic compression** — chars/estimated tokens of original vs `INTENT|TARGET|OUTPUT` form; experimental metric (token = chars/4 estimate).
- **Metrics/Logs** — computed from stored events: counts, success/fail, A/B/encode/decode times, decode accuracy, compression ratio.
- **Demo Mode** — rule-based Agent A and canned Agent B so the demo works without an API.

## Test the workflow
1. Demo Mode: send the example sentence; hear request + response tones; see ✔ checksum OK. 2. Add your key, switch to Live AI, send again. 3. Gateway page → "A → SEND_RESPONSE" shows ACCESS DENIED. 4. Metrics page shows computed values.

## Future work
Microphone/speaker transmission with sync + error correction, real agent connectors (`/ingest`), auth, React/TypeScript frontend, evaluation of compression on real token counts.

## Accounts
Register/login (PBKDF2-hashed passwords, token sessions). First account = admin (sees all data, unlimited Live AI). Other users = `user`: only their own logs/metrics/agents, Live AI limited by `LIVE_LIMIT` per day. Educational-level security; use HTTPS if deployed and never enter real personal data.

## Experiments
`node experiments/audio_experiments.js` reproduces the audio-codec experiments (results in `experiments/audio_results.json`).

## New: voice, your own data, tasks, Telegram
- **Voice:** mic button (Chrome/Edge) and optional read-aloud.
- **My Data:** each account uploads its own text/CSV documents (private to that account); Agent B answers only from them, with optional masking of emails/numbers before they reach the AI.
- **Tasks:** agents run a job once or on a schedule on the server.
- **Telegram:** set `TELEGRAM_BOT_TOKEN`; link a chat with `/link CODE`; then chat with your agents from your phone. Prototype only: do not upload real patient/customer data.
