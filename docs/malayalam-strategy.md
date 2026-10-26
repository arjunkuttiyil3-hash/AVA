# Malayalam strategy

AVA detects Malayalam script locally and labels Latin/Malayalam mixtures as
mixed. Faster-Whisper accepts an `ml` or `en` language hint and applies VAD.
Users can also select their preferred response language. Malayalam story and
anchoring prompts explicitly request natural spoken Malayalam in Malayalam
script, rather than transliterated English.

The response validator checks for the expected writing system. Before TTS,
AVA removes markdown, emoji, and decorative symbols while preserving Malayalam
letters and sentence punctuation. Edge TTS uses the configured Malayalam
Sobhana or Midhun neural voice. If Edge is unavailable, the optional local
`facebook/mms-tts-mal` Transformers model is used. English fallback uses
`pyttsx3`.

Model and voice quality varies by phrase, dialect, and hardware. Human review is
recommended for names, ceremonial protocol, and public announcements.
