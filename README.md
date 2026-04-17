# genAI_hw1

A Streamlit chatbot with long-term memory, chat history, provider switching, and multimodal input support.

## Features

- Text chat with `OpenAI`, `Gemini`, or a local OpenAI-compatible endpoint
- Multimodal messages with multiple image uploads in the same prompt
- Audio uploads or live microphone recording routed through transcription before the model answers
- Persistent chat history and session-scoped memory storage in SQLite
- Optional Python sandbox execution for assistant-generated code blocks
- Debug view for the exact API payload sent to the model

## Run

```bash
./startup.sh
```

## Multimodal Usage

- Attach one or more `.jpg`, `.jpeg`, or `.png` files to ask image-aware questions.
- Use `Start talking` to record directly from your microphone.
- Attach `.mp3`, `.wav`, `.m4a`, or `.ogg` files if you want to import audio instead.
- Press Enter in the chat box to send text plus any pending images or audio.
- Use `Send voice / attachments` when you want to send a recording or uploaded files without typed text.
- Audio is transcript-first by default: the transcript is stored as the user message and sent to the model.
- Turn on `Analyze the recording itself` only when you explicitly want the model to inspect the raw recording in addition to its transcript.
- If transcription is unavailable, audio submission is blocked instead of silently falling back to raw audio.

## Notes

- Automatic background memory extraction is disabled to avoid storing hallucinated facts.
- Session memory is isolated by `session_id` and only that session's saved memory can be appended to prompts.
- Enable `Show API payload` in the sidebar to inspect the exact request context sent to the provider.
- In the current app, audio transcription requires the OpenAI provider plus a working transcription model.
