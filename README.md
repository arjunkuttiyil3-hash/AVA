<<<<<<< HEAD
# AVA
=======
# AVA — AI Virtual Anchoring Assistant

AVA is a privacy-aware speech and event-anchoring assistant for English and
Malayalam. It combines a persistent local SQLite conversation store with a
VRE-inspired orchestration flow: the system identifies the task, selects a
configured model or fallback, validates the result, then streams text and speech
while asking a simulated or serial robot to react. This is an AVA architecture
description, not a claim that AVA is an existing VRE research platform.

## Start AVA

Requirements: Python 3.10+, `pip`, and a Google Gemini API key.
On Windows, use PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
# Set GEMINI_API_KEY and, optionally, GEMINI_MODEL and GEMINI_FALLBACK_MODEL in .env
python scripts/check_env.py
python app.py
```

Open <http://127.0.0.1:8000>. `python run.py --host 127.0.0.1 --port 8000`
overrides the configured bind address. Both launchers serve the same modular
Gemini-backed API (`interface.api:app`). The app defaults to a simulated robot
and text chat remains available if a microphone or speech service is absent.
The first Faster-Whisper or offline Malayalam TTS use may download a model.

## Architecture

```mermaid
flowchart LR
  I[Text / microphone] --> C[Context & intent analysis]
  C --> T[Task identification]
  T --> R[Config-driven model router]
  R --> P[Gemini / template fallback]
  P --> V[Response validation and one retry]
  V --> O[WebSocket text and sentence audio]
  O --> M[Edge TTS / local fallback]
  O --> B[Simulated or serial robot]
  C <--> D[(SQLite + FTS5 memory)]
  O --> U[FastAPI single-page UI]
  U <--> EC[Versioned live event service]
  EC --> DB[(SQLite event state + audit)]
  EC --> CC[/control operator console]
  CC --> SC[Speaker capture and summary]
```

### Components

| Component | Status | Responsibility |
|---|---|---|
| `interface/` (FastAPI API and browser UI) | REQUIRED | WebSocket response events, session/event/settings endpoints, responsive SPA |
| `context/` | REQUIRED | Follow-up resolution, prompt assembly, short-term history and rolling summaries |
| `nlu/` | REQUIRED | Fast intent rules and schema-constrained model classification when ambiguous |
| `orchestrator/` | REQUIRED | Task pipeline, configured routing, retries and model fallback |
| `modules/story_gen.py`, `modules/script_gen.py` | REQUIRED | Story continuity and fact-constrained anchoring prompts |
| `modules/stt.py` | OPTIONAL hardware/model | Faster-Whisper recognition and sounddevice capture; text entry remains available |
| `modules/tts.py` | OPTIONAL service/model | Edge TTS; local Malayalam MMS and English pyttsx3 fallbacks |
| `modules/robot.py` | REQUIRED simulator / OPTIONAL hardware | Animated simulation by default; pyserial controller when configured |
| `services/event_service.py` | REQUIRED | Versioned event state, attendance/schedule edits, undo, and audit trail |
| `modules/speaker_capture.py`, `modules/summarizer.py` | OPTIONAL microphone / REQUIRED operator workflow | Segment transcription plus reviewed, transcript-grounded summaries; typed notes are the fallback |
| `memory/` | REQUIRED | SQLite schema, repositories, legacy event migration, FTS5 |
| `validation/` | REQUIRED | Spoken response length, script, safety and repetition checks |
| Gemini route | REQUIRED for AI generation | Requires an API key in `.env` and a route configured in YAML |
| Semantic embeddings | OPTIONAL / not enabled | FTS5 is the implemented retrieval path; vector retrieval is not currently implemented |

## Configuration

Edit [`config/ava.yaml`](./config/ava.yaml). Environment variables override the
listed deployment settings; copy [`.env.example`](./.env.example) to `.env`.
Blank sample values are ignored.

| Setting | Default | Meaning |
|---|---|---|
| `llm.default_model` / `GEMINI_MODEL` | `gemini-3.6-flash` | Primary Gemini model identifier |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash-lite` | Optional Gemini model tried after primary model errors |
| `llm.timeout_seconds`, `llm.retries` | `60`, `2` | Per-attempt timeout and retry count |
| `llm.latency_budget_ms`, route candidates | `30000`, per-model estimates | Select a candidate by estimated latency and minimum available memory |
| `llm.routes` | task mappings | Select backend/model, optionally fallback and per-language routes |
| `speech.stt_model` | `small` | Faster-Whisper model |
| `speech.language` | `en` | Recognition language hint (`en` or `ml`) |
| `speech.vad_filter` | `true` | Filter non-speech audio |
| `speech.speaker_silence_suggestion_seconds` | `8` | Silence duration before the operator gets a finish suggestion (confirmation is required) |
| `speech.voices` | en-IN-Neerja / ml-IN-Sobhana | Edge TTS voice IDs; Malayalam Midhun is also available |
| `speech.cache_dir` | `audio/cache` | Content-hash audio cache, relative to the project |
| `memory.database_path` | `database/ava.db` | Persistent SQLite file |
| `memory.short_term_turns` | `12` | Maximum recent conversation messages included in each generation prompt, including the current request |
| `memory.retrieval_limit` | `5` | Older FTS5 snippets |
| `memory.token_budget` | `4096` | Approximate prompt budget |
| `robot.mode` | `simulated` | `simulated` or `serial` |
| `robot.serial_port`, `robot.baud_rate` | empty, `115200` | Hardware connection settings |

Supported environment overrides: `AVA_CONFIG_PATH`, `AVA_DB_PATH`, `AVA_HOST`,
`AVA_PORT`, `AVA_LOG_LEVEL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, and
`GEMINI_FALLBACK_MODEL`. `/api/health` checks API-key presence, performs a tiny
Gemini generation, synthesizes a short TTS sample, and checks SQLite. Results are
cached for five minutes; `?refresh=true` forces a new check. Do not expose the
development server to an untrusted network: authentication and multi-user
authorization are not included.

## Memory and speech behavior

Messages, preferences, active story/event state, and summaries persist in SQLite.
Older conversations are summarized using the configured model once they exceed
the recent-turn window. FTS5 retrieves keyword matches, ordered by relevance and
recency. See [context management](./docs/context-management.md).

Story state stores title, characters, setting, plot summary, last paragraph, and
status. Anchoring events retain guest, tone, schedule, and current item index.
Malayalam replies are requested in Malayalam script; TTS cleanup removes
markdown and pictographs before synthesis. After clicking **Enable audio**,
completed response sentences are synthesized with Edge TTS (Neerja for English,
Sobhana for Malayalam) and their MP3 bytes are sent as base64 WebSocket events.
The browser plays sentences in order; if server-side speech synthesis or MP3
playback fails, browser `speechSynthesis` is used when available. **Stop**
cancels current browser playback and signals the active response stream to stop.

## Live event control and speaker summaries

1. Create an event and schedule from **Anchoring** on the main page.
   In Anchoring Studio, **Speech rehearsal** can record a speech sample or take
   pasted text, transcribe it, extract key points, and draft a spoken response
   using the event context. Review the editable comment and use **Speak comment**
   to hear it; the generated response is kept separate from the saved event script.
2. Open **Live event control** or browse to <http://127.0.0.1:8000/control>.
   Event metadata, attendance, schedule order, and item status can be changed
   while AVA is running. The normalized SQLite `event_control`, `schedule_items`,
   and `event_log` tables record a monotonically increasing `state_version`;
   connected main/control pages receive committed changes over their session
   WebSocket.
3. Use per-item arrival/absence/skip/restore/edit/reorder controls. A person
   marked absent, removed, or skipped is filtered from anchoring prompts and
   checked against generated text before any anchoring text/audio is streamed.
   One corrective generation is attempted; a safe schedule-only fallback is
   used if a forbidden name remains. Removing the currently announced item
   requests an immediate browser/robot stop and selects the next eligible item.
4. When a speech item goes on stage, the browser asks for microphone permission
   and records independent 25-second segments. Faster-Whisper transcribes those
   segments in background workers. Malayalam transcripts can have lower
   recognition accuracy; edit them in the transcript field or paste notes when
   capture is unavailable. Silence and “thank you” cues only suggest a finish;
   the operator must confirm **Speech finished**.
5. AVA routes transcript-grounded summaries to Gemini when available, then
   checks that they contain 2–3 short sentences in the event language. Review/edit,
   regenerate, discard, or explicitly speak the result. Speaker summaries are
   existing speaker recaps remain available for review. For configured event
   types, `/api/transcribe` chunks are accumulated and a short recap is spoken
   once the stored local end time passes. This uses the **Speak event summary**
   setting in Live event control. Summary playback and operator announcements
   use the session's saved voice.
   The next transition includes the reviewed summary by default; the operator can
   opt out with the transition checkbox.
   Event reports are downloadable as Markdown, PDF, or DOCX.

The console API is implemented by `EventService`; REST and WebSocket routes
share the same service. Main-chat commands such as “Dr. Nair has not arrived,
remove him” are parsed into a proposed absent-status edit and require operator
confirmation before applying. The example control page has no authentication:
keep AVA bound to localhost and do not expose the operator API to an untrusted
network.

## Tests and checks

```powershell
python -m pytest -q
python scripts/check_env.py
```

`make run`, `make test`, and `make check` are also available where GNU Make is
installed. Test coverage uses temporary SQLite files and simulated model/robot
services; it does not require a live Gemini request or physical microphone.

## Troubleshooting

- **Gemini request fails:** check that `.env` contains a valid `GEMINI_API_KEY`
  and that `GEMINI_MODEL` names a model available to the key. Set
  `GEMINI_FALLBACK_MODEL` to a second available model if desired. Startup checks
  key configuration; `/api/health` performs a small, billable generation and a
  TTS sample check, with results cached for five minutes.
- **Microphone permission/device error:** allow the browser and host OS to use the
  microphone. Keep using the text composer if audio capture is unavailable.
- **Recognition fails on first use:** Faster-Whisper downloads the selected model
  on first use. Check network access, disk space, and the selected STT model.
- **Edge voice unavailable:** Edge TTS requires Internet access. English
  `pyttsx3` and Malayalam `facebook/mms-tts-mal` are fallback paths; the latter
  requires Transformers model downloads and ffmpeg for MP3 conversion.
- **Serial robot disconnected:** confirm the COM port, baud rate, board protocol,
  cable, and that no other application owns the serial port. AVA logs failures
  and keeps text/speech available.
- **Speaker capture unavailable:** allow microphone permission for the browser;
  paste operator notes into the transcript instead. Summary generation still
  uses the same approval/edit workflow.
- **PDF/DOCX export:** install the current `requirements.txt`; Markdown export
  does not require document-generation packages.
- **Database migration:** the original `events` and `schedule` tables are retained
  and extended with `ALTER TABLE` additions. Back up `database/ava.db` before
  manual schema changes.

## Limitations

The server binds to localhost and has no authentication. Continuous listening
transcribes browser audio in finite segments and is not a wake-word system.
Malayalam output quality depends on Gemini and the selected speech voice;
offline fallback quality may differ from Edge TTS. Embedding-based retrieval is not implemented;
FTS5 is the supported retrieval mechanism. The example Arduino sketch is a
protocol stub: it intentionally does not actuate real motors or synthesize
speech, and hardware-specific safety controls must be added and tested before
connecting actuators.

The Anchoring Studio saves script drafts in the current browser. Script text
generation uses the configured Gemini route, and a selected segment can be sent
through the live event announcement endpoint for speech. The API does not yet
provide a durable, interruptible multi-segment playback queue, so pause-after-
segment, queue replacement, and session history are explicitly unavailable.
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2
