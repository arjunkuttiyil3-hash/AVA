# Model selection and routing

`ModelRouter` selects a route by task and language from `config/ava.yaml`.
Each task can provide ordered candidates with an estimated latency and a minimum
available-memory requirement; `llm.latency_budget_ms` bounds candidate selection.
AVA uses the official asynchronous `google-genai` client for all language-model
calls. Set `GEMINI_API_KEY` and `GEMINI_MODEL` in the project-root `.env`; the
default primary model is `gemini-3.6-flash`. Optionally set
`GEMINI_FALLBACK_MODEL` (default `gemini-3.5-flash-lite`). AVA retries transient 429
and 5xx responses, then tries the fallback after 404, 429, timeout, or 5xx
errors, and uses deterministic
templates if the configured models cannot complete the request. Structured
intent classification sends its JSON schema through Gemini's response
configuration.

Model candidates and resource/latency limits remain configured in
`config/ava.yaml`. AVA shows the routed model/backend and measured end-to-end
response time in the browser. Template fallback is deliberately disclosed; it
is not presented as model-generated content. Test Malayalam output separately
for natural spoken phrasing and script.
