# Live event control and speaker summaries

AVA's event-control workflow keeps the operator's latest attendance and schedule
edits authoritative while an event is active. It is deliberately separate from
the language model: the model receives a snapshot of the live schedule, and
application validation rejects absent/removed/skipped names before anchoring
output can be sent to the UI or synthesized.

```mermaid
flowchart LR
  OP[Operator action / confirmed chat command] --> API[FastAPI REST API]
  API --> ES[EventService]
  ES --> TX[SQLite transaction]
  TX --> V[Increment state_version + audit log]
  V --> WS[Session WebSocket broadcast]
  WS --> MAIN[Main anchoring UI]
  WS --> CTRL[/control console]
  CTRL --> MIC[Browser microphone, 25-second segments]
  MIC --> STT[Background Faster-Whisper]
  STT --> TR[Editable per-item transcript]
  TR --> SUM[Map-reduce speaker summarizer]
  SUM --> REVIEW[Operator review / edit / approval]
  REVIEW --> TTS[Browser audio and optional robot gesture]
  V --> PROMPT[Live schedule prompt snapshot]
  PROMPT --> LLM[Model route]
  LLM --> GUARD[Blocked-name and spoken-output validator]
  GUARD --> MAIN
```

## State and API

The first event-control schema is recorded in `schema_meta`. `event_control`
stores event metadata and current item; `schedule_items` stores ordered items,
attendance, duration, notes, transcript, and summary; `event_log` stores the
operator and old/new values. `event_undo` stores transactional snapshots used
for the **Undo last change** action. Every actual event edit increments
`state_version`; TTS cache keys for event speech include that version.

All writes go through `services/event_service.py`, including the compatibility
event-creation route used by the original anchoring form. Legacy event records
are imported into the normalized schema on initialization. Item removal is a
soft removal so audit history and speaker records remain available.

The `/control` page uses the existing conversation session WebSocket. Mutations
commit first, then broadcast the full state and version to all connected clients
in that session. Relevant REST routes include:

- `PATCH /api/live-events/{event_id}` for event metadata/status.
- `POST /api/live-events/{event_id}/start|pause|end|next|previous`.
- `POST /api/live-events/{event_id}/jump/{item_id}`.
- `POST /api/event/items`, `PATCH|DELETE /api/event/items/{item_id}`, and
  `POST /api/event/reorder`.
- `POST /api/live-events/{event_id}/stop-speaking` for browser and robot stop.
- Transcript, summary, announcement, and report routes under
  `/api/live-events/{event_id}`.

Absent, removed, and skipped speaker names are omitted from the safe schedule
sent to the prompt. Before delivering anchoring text, AVA scans the complete
response for blocked names and retries once with corrective instructions. If a
blocked name remains, the fallback introduces only an eligible schedule title.
If a live event version changes during generation, the draft is discarded and
regenerated from the newer event state. A change to the current item stops active
browser audio and requests the robot's stop gesture.

## Speaker capture and summary workflow

When a speech item becomes `on_stage`, `/control` requests browser microphone
permission and records independently decodable WebM segments every 25 seconds.
Each segment is uploaded separately; transcription runs via `asyncio.to_thread`
and Faster-Whisper's VAD filter. A server-side microphone is not required. If
the browser mic is unavailable, operators can paste notes or corrected text.
Malayalam transcription is editable because recognition accuracy may be lower.

Eight seconds of low input audio (configurable in `config/ava.yaml`) or a
transcribed “thank you” cue only reveals a suggestion. It never finishes a
speech without operator confirmation. On confirmation, AVA summarizes the
transcript in 2–3 short sentences; long transcripts use chunk summaries before
the final summary. The summarizer is instructed to use transcript facts only,
and validates the requested language and length. Empty/very short transcripts
produce an explicit “summary could not be generated” message.

The generated summary remains editable. **Speak it** uses browser playback;
**Auto-speak summary** is an explicit opt-in and defaults off. The operator may
include the summary in the transition to the next eligible schedule item.
Markdown export is built in; PDF and DOCX use ReportLab and python-docx from
`requirements.txt`.

## Run and test

```powershell
python run.py
python -m pytest -q tests/test_event_service.py tests/test_summarizer.py tests/test_app.py
```

Use <http://127.0.0.1:8000/control> after saving an event in the main UI.
Automated tests use temporary SQLite databases and simulated services; actual
microphone capture, Whisper model availability, Edge TTS, and physical robot
stop behavior depend on the host and connected hardware.
