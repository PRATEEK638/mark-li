# AURA Project Progress

## 🎯 Target Architecture
AURA is a local-first AI desktop assistant where the responsibilities are split as follows:

**AURA (Interface & Authority)**
- UI (Electron + React)
- Permissions & User-facing confirmation
- Status/Event rendering
- Memory & Settings management
- Process lifecycle (starts/stops Hermes)

**Hermes (Body & Runtime)**
- Agent Execution Loop (Plan $\rightarrow$ Act $\rightarrow$ Observe)
- Tool Selection & Execution
- OS/Filesystem/Browser/Terminal interactions
- Local LLM orchestration via Ollama

**Ollama (Inference Provider)**
- Local hosting of the reasoning model (Gemma 4 31B)

**Request Path:**
`React UI` $\rightarrow$ `Electron IPC` $\rightarrow$ `Hermes Gateway (WebSocket)` $\rightarrow$ `Hermes Agent` $\rightarrow$ `Ollama/Gemma` $\rightarrow$ `OS Action` $\rightarrow$ `Events streamed back to UI`

---

## ✅ Implemented So Far
- [x] **Hermes Gateway Identification:** Identified `hermes serve` as the correct entry point for JSON-RPC/WebSocket integration via `/api/ws`.
- [x] **Hermes Client:** Implemented `electron/hermes-client.cjs` to handle:
    - Spawning the `hermes serve` process.
    - Establishing a persistent WebSocket connection.
    - Managing JSON-RPC request IDs.
    - Handling the `prompt.submit` and `session.create` flow.
- [x] **Electron Integration:** Patched `electron/main.cjs` to initialize the `HermesClient` and forward events to the renderer.
- [x] **Dependency Management:** Installed `ws` package for WebSocket support.

---

## 🚧 Current Blocker
**WebSocket Authentication (403 Forbidden)**
- The `hermes serve` backend is rejecting the WebSocket handshake with a `403 Forbidden` error.
- Analysis suggests that the backend requires an authentication token (likely the SSH session token referenced in the `hermes serve` help menu).
- The current `HermesClient` attempt uses a raw WebSocket connection without tokens.

---

## 🛠 Next Steps (Vertical Slice)
**Goal:** "Create a folder called Aura Test on my Desktop."

1. **Solve Authentication:** Determine how to generate or provide the required session token for the `hermes serve` gateway.
2. **Verify connectivity:** Successfully establish a WebSocket connection to `/api/ws`.
3. **Execute Action:** Send `prompt.submit` $\rightarrow$ Observe `tool.start` $\rightarrow$ Verify folder creation on Desktop.
4. **Measure Latency:** Record T0-T7 timestamps to identify bottlenecks.
5. **Verify Persistence:** Execute a second command ("Create Aura Test 2") using the same process and session.
6. **Cleanup:** Once the Hermes path is stable, remove the legacy `brain.cjs` and `runtime.cjs` orchestration logic.
