# Technoline raw-channel probe

Discovers the undocumented wire protocol of the Technoline PBX streaming
("raw") channel. One real test call is enough to answer every open question.

## The protocol (captured from a real call, 2026-08-18)

The PBX connects out to our endpoint as a WebSocket client and sends:

1. One **text frame** with call metadata — no auth header is used, the channel
   token travels in this message:

   ```json
   {"type": "start", "callId": "5752c0f5…", "caller": "0527180504",
    "system": "0765673575", "token": "<channel token>",
    "format": "pcm16;rate=8000;ch=1"}
   ```

2. Then **raw binary frames**: 320 bytes every 20ms — PCM 16-bit
   **little-endian**, 8kHz, mono. No JSON wrapping, no base64.

Notes:
- Silence is a constant sample value of `8`, not `0`. Harmless, but it means
  "is this silence" cannot be tested with `== 0`, and a naive energy VAD sees a
  small DC offset.
- The connection closes with WebSocket code 1006 (abnormal), not a clean 1000.
- 1323 echoed frames were accepted by the PBX without error, so the outbound
  direction takes the same framing. Whether the caller actually heard them is
  unconfirmed.

## AI mode (feasibility spike)

Set `PROBE_MODE=ai` and `GEMINI_API_KEY=…` and the same endpoint answers the
call with Gemini Live instead of echoing: greeting, Hebrew speech in and out,
server-side VAD for turn taking, and barge-in (queued output is dropped the
moment the caller starts talking). Every call logs per-turn reply latency and
both transcripts, and they are stored in the capture's `meta.json`.

Optional: `GEMINI_LIVE_MODEL`, `GEMINI_LIVE_VOICE`, `BOT_GREETING`,
`BOT_DB_URL` (default `sqlite:///./bot.db`).

## Dispatch layer

The conversation logic lives beside the transport, not inside it, so the wording
can change without touching the audio path. The system prompt is stored in the
database and edited at `/admin`; the bridge reads it when the call starts.

Four tools are exposed to the model, all lazy — nothing is loaded at call setup,
only when the conversation needs it:

| Tool | Purpose |
|---|---|
| `get_customer` | Name, preferred pickup address and notes for the caller |
| `get_recent_call` | The caller's previous call within 10 minutes, so a redial resumes instead of restarting |
| `lookup_price` | The only source of prices; the prompt forbids inventing one. Matches either direction |
| `save_order` | Writes the confirmed order |

The caller's number comes from the PBX `start` frame, so identification needs no
question. Orders are listed at `/admin/orders` and exported at
`/admin/orders.xlsx`; the price list is managed at `/admin/prices`.

SQLite is the store. On Render it needs a mounted disk, otherwise orders are
lost on redeploy.

Customers are managed at `/admin/customers`, and every call is listed at
`/admin/calls` with its transcript, per-turn latencies, turn and interruption
counts, and the tool calls the model made. Set `ORDER_WEBHOOK_URL` (plus
`ORDER_WEBHOOK_HEADER` if the receiver needs one, in `Name: value` form) and each
confirmed order is POSTed as JSON — WhatsApp, Slack or an internal endpoint alike.
Delivery is best effort and off the call's critical path: a failing webhook is
logged, never surfaced to the caller.

Exercise it without a phone call by replaying a recorded caller:

```bash
python tools/fake_pbx.py --mode replay --replay caller.raw --out reply.wav
```

## Cascaded path vs Gemini Live

`tools/cascade_bench.py` times the alternative architecture — STT, then LLM, then
TTS as three separate requests — against Live on the same utterance:

```bash
GEMINI_API_KEY=… python tools/cascade_bench.py --wav utterance.wav
```

On the recorded test call the cascade needed 7.5s end to end (STT 2.8s, LLM 0.6s,
TTS 4.1s) against 3.5s to first audio from Live. The gap is structural, not a
matter of model choice: each cascade stage must finish before the next begins,
and the TTS stage cannot emit a single byte until the whole reply is written,
whereas Live starts speaking while it is still generating. The cascade's
advantages are the ones that do not show up in latency — the transcript is
available as text before the reply, and each stage can be swapped for a
specialised Hebrew vendor. Keep Live for the phone path.

The probe never rejects a connection and never assumes an encoding. It accepts
whatever arrives, records it byte for byte, and reports what it saw.

## What one test call tells you

| Open question | Where the answer shows up |
|---|---|
| Binary frames or JSON+base64? | `frame_kinds` in the summary; text frames are dumped verbatim |
| PCM16 LE/BE, mu-law or A-law? | `codec_verdict.ranked` — plus four WAV renderings to listen to |
| Frame size and cadence | `common_frame_sizes`, `inferred_frame_ms`, per-frame `dt_ms` |
| How the Bearer secret is sent | `handshake.headers` (redacted), `query_params`, first text frame |
| Is there a handshake with caller ID? | `first_text_frames` |
| Does the outbound direction work? | echo loopback — if the caller hears themselves, framing is right |

## Codec detection

Speech sampled at 8kHz is smooth: neighbouring samples are close together. A
wrong endianness or a wrong companding law scrambles the low bits and turns the
waveform into noise. The probe scores each candidate by mean absolute
sample-to-sample delta over RMS (`roughness`); the real encoding scores well
below 1.0 while the wrong ones sit far above it. The WAV renderings are the
human-audible confirmation — exactly one will sound like a voice.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
python tools/fake_pbx.py --mode binary-mulaw   # or binary-pcm16le / json-base64
```

Then open <http://127.0.0.1:8000/> for the capture list.

The dispatcher console is a separate app under `web/`:

```bash
cd web && npm install && npm run dev    # http://127.0.0.1:5173, /api proxied to :8000
```

## Two services, one repository

The backend answers the phone; the console is a static React bundle that talks
to it over `/api`. They deploy as separate Render services with `buildFilter`
rules, so a console commit never rebuilds the phone line and a backend commit
ships in seconds without waiting for npm. The console reaches the backend via
`VITE_API_BASE`, baked in at build time, and the backend must list the console's
domain in `CONSOLE_ORIGINS`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PROBE_ECHO` | `loopback` | `off`, `loopback` (echo inbound frames back), `tone` (periodic 440Hz) |
| `PROBE_ECHO_DELAY_MS` | `700` | Delay before echoing, so the caller can tell it apart from sidetone |
| `PROBE_TONE_CODEC` | `mulaw` | Encoding for `tone` mode: `mulaw`, `pcm16le`, `pcm16be` |
| `PROBE_BEARER_SECRET` | unset | Expected Bearer value; only reported, not enforced |
| `PROBE_ENFORCE_BEARER` | `0` | Set to `1` only after the protocol is known — rejecting during discovery hides data |
| `PROBE_CAPTURE_DIR` | `captures` | Where captures are written |
| `BOT_DB_URL` | `sqlite:///./bot.db` | Postgres URL in production; `postgres://` and `postgresql://` are rewritten onto psycopg 3 |
| `ADMIN_TOKEN` | unset | Required as `X-Admin-Token` on writing API calls; unset leaves the API open for local work |
| `CONSOLE_ORIGINS` | `*` | Comma-separated origins allowed by CORS — the console's Render domain |

## Deploy on Render

`render.yaml` covers it if the service is created as a Blueprint. For a service
configured through the dashboard, set:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/healthz`

`.python-version` pins 3.12.7 — Render otherwise defaults to the newest
interpreter, which often has no wheels for the pinned dependencies.

Captures are written under `PROBE_CAPTURE_DIR`. Without a persistent disk they
are lost on redeploy, so download anything worth keeping right after the test
call.

## Test call procedure

1. Deploy, confirm `/healthz`.
2. Point the PBX streaming channel at `wss://<host>/ws/ivr` (already registered).
3. Call in. Speak continuously for ~10 seconds — count out loud, do not stay
   silent, since silence is smooth under every candidate encoding and makes
   detection inconclusive. Listen for your own voice echoed back.
4. Open `/`, click the call, read the verdict and listen to the four WAVs.
