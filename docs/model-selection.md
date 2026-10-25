# Model selection and routing

`ModelRouter` selects a route by task and language from `config/ava.yaml`.
Each task can provide ordered candidates with an estimated latency and a minimum
available-memory requirement; `llm.latency_budget_ms` bounds candidate selection.
Ollama's Python client streams `gemma3:4b` by default when its candidate meets
those configured constraints. Intent classification passes a JSON Schema to
Ollama's structured-output `format` field. Model failures are logged and retried
with exponential backoff; a configured alternate backend may be used before the
deterministic template fallback.

Gemini is optional. Install the declared `google-genai` dependency, set
`GEMINI_API_KEY` in `.env`, then configure a Gemini route or route fallback. No
key is required for the local Ollama setup. The application does not send
requests to Gemini unless the YAML route selects it.

Select a smaller local model when memory or latency is constrained and test
Malayalam output separately. AVA shows its selected and final backend/model and
measured end-to-end response time in the browser. Template fallback is
deliberately disclosed; it is not presented as model-generated content.
