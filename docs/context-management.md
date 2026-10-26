# Context management

SQLite is the durable source of truth. Each session has ordered user/assistant
messages, a rolling summary and summary cursor, preferences, and nullable active
event/story identifiers. Story rows retain character JSON, setting, plot summary,
the last paragraph, and active/completed status. Event rows retain the chief
guest, speakers/schedule JSON, tone, and current schedule index. Existing event
and schedule rows are preserved by additive migration.

For each message AVA assembles system instructions, language/style/voice
preferences, relevant active state, the summary, FTS5 matches, recent turns, and
the current message within an approximate character-based budget. Short-term
history is sent in chronological order. Once the message count exceeds the
configured short-term window, older messages not yet covered by the summary
cursor are summarized by the configured model and the cursor advances. Original
messages remain available for keyword retrieval after summarization.

Follow-up resolution uses active state plus reference cues and the selected
Story/Anchoring mode. Starting a new explicit story does not inherit the prior
story. The regression test persists a robot-in-Kerala story and then resolves
“Now make it meet a school student” against the same story after reopening the
SQLite repository.

FTS5 uses keyword matching, relevance rank, and message recency. Embeddings are
not implemented in this version; `memory.embeddings_enabled` is reserved and
must remain `false`.
