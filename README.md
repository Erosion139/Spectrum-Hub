# Spectrum Hub

The server side of Spectrum Player. It runs on TrueNAS and:

- takes voice commands from the phone app over a WebSocket,
- works out what you meant (quick rules for simple things, a local LLM for the rest),
- plays songs from Jellyfin, or **fetches them from Soulseek first** if they aren't there yet,
- sends playback and navigation commands back to the phone,
- exposes the same actions as an **MCP server** for other AI clients,
- shows all of it in a web dashboard: connections, requests, downloads, CPU/RAM/GPU/VRAM, LLM speed and live LLM output, plus model management.

```
phone (speech-to-text on the phone)
   │  wss://…/ws/phone   {"type":"voice","text":"play dreams by fleetwood mac"}
   ▼
Spectrum Hub ── rules / LLM (llama.cpp) → {"action":"play","artist":"Fleetwood Mac","title":"Dreams"}
   │
   ├─ Jellyfin: already there? ───────────────────────────────┐
   ├─ slskd: search Soulseek → rank files → download          │
   ├─ tag + file into /music/Soulseek/Artist/Album/           │
   └─ Jellyfin: index the new file ───────────────────────────┤
                                                               ▼
phone ◄── {"type":"command","action":"play_item","args":{"item_id":"…"}}
          (plays its own downloaded copy if it has one, otherwise streams from Jellyfin)
```

The LLM only turns speech into a small JSON command (the output is forced to match a schema);
everything after that is ordinary code. Small models are good at the first job and unreliable at
long tool-calling chains, and this way the hub still works with no model loaded.

## Repository layout

| Path | What it is |
|---|---|
| `truenas-app.yaml` | The stack to paste into TrueNAS (hub + slskd). |
| `hub/Dockerfile` | One image: Python app + llama-server built with CUDA 12.4 (supports the GTX 1060). |
| `hub/app/` | The hub (FastAPI). `pipeline.py` is the request flow, `matching.py` picks Soulseek files, `intents.py` the command parsing, `phone.py` the phone protocol, `mcp_tools.py` the MCP tools. |
| `hub/web/` | The dashboard (plain HTML/JS, no build step). |
| `hub/dev/mock_services.py` | Fake Jellyfin + slskd for trying everything on a PC. |
| `.github/workflows/build.yml` | Builds the image and publishes it to `ghcr.io/<you>/spectrum-hub`. |

## Install on TrueNAS

1. **Publish the image.** Push this repo to GitHub. The *Build image* workflow builds it (the first
   build compiles llama.cpp with CUDA and takes about 30–45 minutes; later ones use the cache). Then
   on GitHub open *Packages → spectrum-hub → Package settings* and set the visibility to **Public**,
   so TrueNAS can pull it without a login.
2. **NVIDIA driver.** In TrueNAS: *Apps → Configuration → Settings → Install NVIDIA Drivers*.
3. **Jellyfin API key.** Jellyfin *Dashboard → API Keys → +*.
4. **Install the stack.** *Apps → Discover Apps → ⋮ → Install via YAML*, paste
   `truenas-app.yaml`, and fill in everything marked `CHANGE`. The important part is the
   music folder: `/music` must be the same dataset Jellyfin's music library points at, so files
   the hub puts in `/music/Soulseek` show up in Jellyfin.
5. Open `http://<nas>:8780`, check that Jellyfin and Soulseek show green, and set a web UI password.
6. **Model.** *Models* tab → paste a Hugging Face repo → *List files* → *Download* → *Use*.
   For a 3 GB card, try `Qwen/Qwen2.5-3B-Instruct-GGUF` (`q4_k_m`, about 2 GB) or something
   smaller from the 1.5B range. The VRAM card on the dashboard shows whether it fits alongside the
   other apps on the GPU; if not, lower *GPU layers* so part of the model runs on the CPU.

### Reaching it from the phone

Add a public hostname to your existing cloudflared container, pointing at `http://<nas>:8780`.
Cloudflare passes WebSockets through, so the phone uses:

- at home: `ws://<nas>:8780/ws/phone`
- away: `wss://hub.your-domain.com/ws/phone`

and the token from *Settings → Phone and MCP token*. Set a **web UI password** before exposing
the port: the dashboard is otherwise open to anyone with the URL. The phone and MCP endpoints always
need the token.

### MCP clients

Streamable HTTP at `http(s)://<host>/mcp` with `Authorization: Bearer <token>`. Tools:
`voice_command`, `request_song`, `play_collection`, `get_request`, `list_requests`,
`search_library`, `phone_control`, `phone_status`.

## Phone protocol

JSON over the WebSocket. Phone → hub: `hello`, `voice {text}`, `status {state,title,artist,media_id}`,
`ack {id, ok, message}`, `ping`. Hub → phone: `command {id, action, args}` with actions
`play_item`, `queue_item`, `play_folder`, `skip`, `previous`, `pause`, `resume`, `navigate`,
`add_stop`, `stop_navigation`; `notice {text}`; `pong`. Details in `hub/app/phone.py`.

## Try it on a PC (no TrueNAS, no GPU)

```bash
cd hub
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Linux/macOS: .venv/bin/pip
python dev/mock_services.py --downloads ./scratch/downloads --music ./scratch/music/Soulseek
# second terminal:
DATA_DIR=./scratch/data MODELS_DIR=./scratch/models SLSKD_DOWNLOADS_DIR=./scratch/downloads \
IMPORT_DIR=./scratch/music/Soulseek JELLYFIN_URL=http://127.0.0.1:8790/jf JELLYFIN_API_KEY=x \
SLSKD_URL=http://127.0.0.1:8790/slskd SLSKD_API_KEY=y \
python -m uvicorn app.main:app --port 8780
```

Then type "play blue monday by new order" in the dashboard. To try the LLM, download a
[llama.cpp release](https://github.com/ggml-org/llama.cpp/releases) and set `LLAMA_SERVER_BIN` to its
`llama-server`.

## Not done yet

- **VPN** for slskd (gluetun supports Windscribe; slskd would use `network_mode: service:gluetun`).
- Downloading **whole albums** from Soulseek (songs only for now; albums and artists already in Jellyfin play fine).
- Streaming a song **while** it downloads (it plays once it's in Jellyfin).
- Multi-part GGUF models (`-00001-of-00002`): download both parts, then pick the first.
