# Methodology: VRE-inspired orchestration

AVA uses a virtual-robot-environment-inspired separation of sensing, context,
task selection, service routing, validation, output, and action. This is a
software architecture analogy for this project; AVA does not claim to be an
existing VRE research platform.

1. **Input:** a browser text message or a recorded microphone segment.
2. **Context and intent:** retrieve short-term turns, summaries, related older
   messages, preferences, and referenced story/event state. Fast deterministic
   intent rules avoid an LLM call for common requests.
3. **Task identification and service selection:** map the intent, language, and
   mode to an entry in `config/ava.yaml`.
4. **Processing:** stream the configured Gemini model with bounded retries and
   exponential backoff. A separately configured Gemini fallback model is tried
   after primary-model not-found, rate-limit, or timeout failures; template
   responses are the final fallback.
5. **Validation:** reject unsafe, empty, excessive, repetitive, or mismatched
   script output. One corrective generation is attempted before a concise
   built-in fallback.
6. **Output and action:** stream text, synthesize validated sentence segments,
   persist assistant output, and run the matching robot speech/gesture calls
   concurrently.

The UI exposes the intent, selected model/backend, latency, and retrieved memory
so model/service decisions are observable rather than hidden.
