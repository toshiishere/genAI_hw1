import streamlit as st
from openai import OpenAI
import base64
import re
import io
import contextlib
import logging
import os
import sqlite3
import json
import uuid
import mimetypes
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# -------------------------------------------------------------------
# DATABASE FUNCTIONS (LONG-TERM MEMORY)
# -------------------------------------------------------------------
DB_FILE = "chat_history.db"
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

def init_db():
    """Creates the database and messages table if they don't exist."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS long_term_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            fact TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute("PRAGMA table_info(long_term_memory)")
    memory_columns = [row[1] for row in c.fetchall()]
    if "session_id" not in memory_columns:
        c.execute("ALTER TABLE long_term_memory ADD COLUMN session_id TEXT")
    c.execute('CREATE INDEX IF NOT EXISTS idx_messages_session_timestamp ON messages(session_id, timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_memory_session_timestamp ON long_term_memory(session_id, timestamp)')
    conn.commit()
    conn.close()

def save_memory(session_id, fact):
    """Saves a fact to the session-scoped long-term memory table."""
    cleaned_fact = (fact or "").strip()
    if not cleaned_fact:
        return

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT 1 FROM long_term_memory WHERE session_id = ? AND fact = ? LIMIT 1',
        (session_id, cleaned_fact),
    )
    if c.fetchone() is None:
        c.execute(
            'INSERT INTO long_term_memory (session_id, fact) VALUES (?, ?)',
            (session_id, cleaned_fact),
        )
    conn.commit()
    conn.close()

def get_session_memories(session_id, limit=10):
    """Retrieves session-scoped memories ordered by recency."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT fact FROM long_term_memory WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?',
        (session_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def save_message(session_id, role, content):
    """Saves a single message to the SQLite database."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # json.dumps ensures we can safely store both text and multimodal lists
    c.execute('INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)',
              (session_id, role, json.dumps(content)))
    conn.commit()
    conn.close()

def load_messages(session_id):
    """Retrieves all messages for a specific session."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp ASC', (session_id,))
    rows = c.fetchall()
    conn.close()
    
    messages = []
    for row in rows:
        raw_content = row[1]
        try:
            content = json.loads(raw_content)
        except json.JSONDecodeError:
            content = raw_content
        messages.append({"role": row[0], "content": content})
    return messages

def get_all_sessions():
    """Returns a list of unique session IDs, ordered by most recent."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT session_id, MAX(timestamp) FROM messages GROUP BY session_id ORDER BY MAX(timestamp) DESC')
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def delete_session(session_id):
    """Deletes all messages and session memories for a specific session."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
    c.execute('DELETE FROM long_term_memory WHERE session_id = ?', (session_id,))
    conn.commit()
    conn.close()

# Initialize the database when the app starts
init_db()

# -------------------------------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------------------------------
def encode_file(uploaded_file):
    return base64.b64encode(uploaded_file.getvalue()).decode("utf-8")

def guess_mime_type(uploaded_file):
    if uploaded_file.type:
        return uploaded_file.type
    guessed_type, _ = mimetypes.guess_type(uploaded_file.name)
    return guessed_type or "application/octet-stream"

def decode_data_url(data_url):
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None, None
    header, _, encoded = data_url.partition(",")
    mime_type = header[5:].replace(";base64", "")
    return mime_type, base64.b64decode(encoded)

def audio_format_from_mime_type(mime_type):
    if not mime_type:
        return "wav"
    return mime_type.split("/")[-1].lower()

def decode_base64_data(encoded_value):
    if not encoded_value:
        return None
    if isinstance(encoded_value, str) and encoded_value.startswith("data:"):
        _, decoded = decode_data_url(encoded_value)
        return decoded
    return base64.b64decode(encoded_value)

def render_message_content(content):
    if isinstance(content, str):
        st.markdown(content)
        return

    if not isinstance(content, list):
        st.markdown(str(content))
        return

    for item in content:
        item_type = item.get("type")

        if item_type == "text":
            st.markdown(item.get("text", ""))
        elif item_type == "image_url":
            image_url = item.get("image_url", {}).get("url")
            if image_url:
                st.image(image_url, caption=item.get("name") or "Attached image", width=320)
        elif item_type == "input_audio":
            audio_payload = item.get("input_audio", {})
            audio_data = audio_payload.get("data")
            mime_type = audio_payload.get("mime_type") or audio_payload.get("format", "audio/mpeg")
            audio_bytes = decode_base64_data(audio_data)
            if audio_bytes:
                st.audio(audio_bytes, format=mime_type)
            st.caption(f"Audio attached: {item.get('name', 'uploaded audio')}")
        elif item_type == "attachment_note":
            st.caption(item.get("text", "Attachment included"))

def sanitize_content_for_provider(provider, content):
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return str(content)

    sanitized_content = []
    for item in content:
        item_type = item.get("type")
        if item_type == "text":
            sanitized_content.append({"type": "text", "text": item.get("text", "")})
        elif item_type == "image_url":
            if provider in {"OpenAI", "Gemini"}:
                sanitized_content.append(
                    {
                        "type": "image_url",
                        "image_url": item.get("image_url", {}),
                    }
                )
            else:
                sanitized_content.append(
                    {
                        "type": "text",
                        "text": f"[Image attached: {item.get('name', 'uploaded image')}]",
                    }
                )
        elif item_type == "input_audio":
            audio_payload = item.get("input_audio", {})
            if provider == "Gemini":
                sanitized_content.append(
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_payload.get("data", ""),
                            "format": audio_payload.get("format", "wav"),
                        },
                    }
                )
            else:
                sanitized_content.append(
                    {
                        "type": "text",
                        "text": f"[Audio attached: {item.get('name', 'uploaded audio')}]",
                    }
                )
        elif item_type == "attachment_note":
            sanitized_content.append(
                {
                    "type": "text",
                    "text": item.get("text", "Attachment included"),
                }
            )

    if not sanitized_content:
        return ""

    text_only = all(item["type"] == "text" for item in sanitized_content)
    if text_only:
        return "\n".join(item["text"] for item in sanitized_content if item.get("text")).strip()

    return sanitized_content

def build_api_messages(provider, system_prompt_text, messages):
    api_messages = [{"role": "system", "content": system_prompt_text}]
    for message in messages:
        api_messages.append(
            {
                "role": message["role"],
                "content": sanitize_content_for_provider(provider, message["content"]),
            }
        )
    return api_messages

def split_uploaded_files(uploaded_files):
    image_parts = []
    audio_files = []
    other_parts = []

    for uploaded_file in uploaded_files:
        mime_type = guess_mime_type(uploaded_file)
        encoded_file = encode_file(uploaded_file)

        if mime_type.startswith("image/"):
            image_parts.append(
                {
                    "type": "image_url",
                    "name": uploaded_file.name,
                    "image_url": {"url": f"data:{mime_type};base64,{encoded_file}"},
                }
            )
        elif mime_type.startswith("audio/"):
            audio_files.append(uploaded_file)
        else:
            other_parts.append(
                {
                    "type": "attachment_note",
                    "text": f"Unsupported file attached: {uploaded_file.name}",
                }
            )

    return image_parts, audio_files, other_parts

def build_audio_attachment_parts(audio_files):
    content_parts = []
    normalized_audio_files = []

    for uploaded_file in audio_files:
        mime_type = guess_mime_type(uploaded_file)
        encoded_file = encode_file(uploaded_file)

        content_parts.append(
            {
                "type": "input_audio",
                "name": uploaded_file.name,
                "input_audio": {
                    "data": encoded_file,
                    "format": audio_format_from_mime_type(mime_type),
                    "mime_type": mime_type,
                },
            }
        )
        normalized_audio_files.append(uploaded_file)

    return content_parts, normalized_audio_files

def build_transcript_sections(audio_files, transcripts):
    transcript_sections = []
    for audio_file, transcript in zip(audio_files, transcripts):
        cleaned_transcript = (transcript or "").strip()
        if cleaned_transcript:
            transcript_sections.append(f"Transcript from {audio_file.name}: {cleaned_transcript}")
    return transcript_sections

def build_user_content(prompt_text, text_sections, attachment_parts):
    prompt_text = (prompt_text or "").strip()
    combined_text_sections = []

    if prompt_text:
        combined_text_sections.append(prompt_text)
    combined_text_sections.extend(section for section in text_sections if section)

    if attachment_parts or combined_text_sections:
        content = []
        for text_section in combined_text_sections:
            content.append({"type": "text", "text": text_section})
        content.extend(attachment_parts)
        return content

    return ""

def normalize_model_name(provider, model_name):
    cleaned = (model_name or "").strip()

    if provider == "Gemini" and cleaned == "gemini-3-flash":
        return "gemini-2.5-flash"

    return cleaned

def transcribe_audio_file(client, uploaded_file, transcription_model):
    audio_bytes = uploaded_file.getvalue()
    audio_buffer = io.BytesIO(audio_bytes)
    audio_buffer.name = uploaded_file.name
    transcription = client.audio.transcriptions.create(
        model=transcription_model,
        file=audio_buffer,
    )
    text = getattr(transcription, "text", "") or ""
    return text.strip()

# -------------------------------------------------------------------
# STATE MANAGEMENT
# -------------------------------------------------------------------
# Generate a new session ID if one doesn't exist
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

if "messages" not in st.session_state:
    st.session_state.messages = []

if "last_api_messages" not in st.session_state:
    st.session_state.last_api_messages = []

# -------------------------------------------------------------------
# SIDEBAR: Chat History & Settings
# -------------------------------------------------------------------
st.sidebar.title("📚 Chat History")

# New Chat Button
if st.sidebar.button("➕ New Chat"):
    st.session_state.session_id = str(uuid.uuid4())
    st.session_state.messages = []
    st.rerun()

# Load Past Sessions
st.sidebar.subheader("Previous Sessions")
past_sessions = get_all_sessions()
for past_session in past_sessions:
    col1, col2 = st.sidebar.columns([4, 1])
    with col1:
        # Use the first 8 characters of the UUID as a readable label
        if st.button(f"Session {past_session[:8]}...", key=f"load_{past_session}"):
            st.session_state.session_id = past_session
            st.session_state.messages = load_messages(past_session)
            st.rerun()
    with col2:
        if st.button("❌", key=f"del_{past_session}"):
            delete_session(past_session)
            if st.session_state.session_id == past_session:
                st.session_state.session_id = str(uuid.uuid4())
                st.session_state.messages = []
            st.rerun()

st.sidebar.divider()
st.sidebar.title("⚙️ LLM Settings")

provider = st.sidebar.selectbox("Select Provider", ["Gemini", "OpenAI", "Local"])

if provider == "Gemini":
    default_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    default_model = "gemini-2.5-flash"
    default_key = os.getenv("API_KEY_GEMINI", "")
elif provider == "OpenAI":
    default_url = "https://api.openai.com/v1"
    default_model = "gpt-4o"
    default_key = os.getenv("API_KEY_OPENAI", "")
else:
    default_url = "http://localhost:11434/v1" 
    default_model = "llama3"
    default_key = ""

api_key = st.sidebar.text_input(f"{provider} API Key", type="password", value=default_key)
base_url = st.sidebar.text_input("Base URL", value=default_url)
model_name = st.sidebar.text_input("Model Name", value=default_model)
model_name = normalize_model_name(provider, model_name)
if provider == "Gemini" and model_name == "gemini-2.5-flash":
    st.sidebar.caption("Using Gemini OpenAI-compatible model `gemini-2.5-flash`.")
enable_audio_transcription = st.sidebar.checkbox("🎙️ Transcribe audio uploads", value=True)
transcription_model = st.sidebar.text_input(
    "Transcription Model",
    value="gpt-4o-mini-transcribe" if provider == "OpenAI" else "",
    help="Used for all audio inputs before chat submission. If unavailable, audio messages are blocked.",
)

enable_sandbox = st.sidebar.checkbox("🐍 Enable Python Sandbox", value=False)
show_debug_payload = st.sidebar.checkbox("🪵 Show API payload", value=True)
system_prompt = st.sidebar.text_area("System Prompt:", value="You are a brilliant researcher.", height=100)

can_transcribe_audio = provider == "OpenAI" and bool(transcription_model.strip())
if provider != "OpenAI":
    st.sidebar.caption("Live transcription is enabled only for the OpenAI provider in this app.")

# -------------------------------------------------------------------
# MAIN UI
# -------------------------------------------------------------------
st.title("My Personal LLM 🧠")
st.caption(f"Current Session: `{st.session_state.session_id}`")

# Render existing conversation 
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        render_message_content(message["content"])

if show_debug_payload and st.session_state.last_api_messages:
    with st.expander("Last API Payload", expanded=False):
        st.json(st.session_state.last_api_messages)

# -------------------------------------------------------------------
# CHAT INPUT & EXECUTION
# -------------------------------------------------------------------
recorded_audio = st.audio_input(
    "Start talking",
    sample_rate=16000,
    help="Record a voice message with your microphone. You can use this together with typed text and uploaded files.",
)

uploaded_files = st.file_uploader(
    "Attach images or audio files (optional)",
    type=["jpg", "jpeg", "png", "mp3", "wav", "m4a", "ogg"],
    accept_multiple_files=True,
)
analyze_recording = st.checkbox(
    "Analyze the recording itself",
    value=False,
    help="Off by default: audio is transcribed and the transcript becomes your user message. Turn this on only when you want the model to inspect the recording/audio itself.",
)

pending_files = list(uploaded_files or [])
if recorded_audio is not None:
    pending_files.append(recorded_audio)

if pending_files:
    preview_images = [f for f in pending_files if guess_mime_type(f).startswith("image/")]
    preview_audio = [f for f in pending_files if guess_mime_type(f).startswith("audio/")]

    if preview_images:
        st.caption("Images ready to send")
        for preview_image in preview_images:
            st.image(preview_image, caption=preview_image.name, width=220)

    if preview_audio:
        st.caption("Audio ready to send")
        for preview_file in preview_audio:
            st.audio(preview_file.getvalue(), format=guess_mime_type(preview_file))
            st.caption(preview_file.name)

prompt = st.chat_input("What's on your mind?")
send_attachments_only = st.button(
    "Send voice / attachments",
    disabled=not pending_files,
    help="Use this when you want to send recorded audio or attached files without typing a text message.",
)

submitted = prompt is not None or send_attachments_only

if submitted:
    image_parts, audio_files, other_parts = split_uploaded_files(pending_files)
    audio_attachment_parts = []
    transcript_sections = []

    if audio_files:
        if not api_key and provider != "Local":
            st.warning(f"⚠️ Please enter your {provider} API Key before sending audio.")
            st.stop()
        if not enable_audio_transcription:
            st.warning("Enable `Transcribe audio uploads` to send audio. Audio messages are transcript-first.")
            st.stop()
        if not can_transcribe_audio:
            st.warning("Audio messages require a working transcription model. Switch to the OpenAI provider or configure transcription first.")
            st.stop()
        transcription_client = OpenAI(api_key=api_key or "local", base_url=base_url)

    if audio_files:
        transcripts = []
        for audio_file in audio_files:
            try:
                transcript = transcribe_audio_file(transcription_client, audio_file, transcription_model)
                transcripts.append(transcript)
            except Exception as transcription_error:
                st.error(f"Failed to transcribe {audio_file.name}: {transcription_error}")
                st.stop()

        transcript_sections = build_transcript_sections(audio_files, transcripts)
        if not transcript_sections:
            st.warning("Audio was received, but no transcript text was produced.")
            st.stop()

        if analyze_recording:
            audio_attachment_parts, _ = build_audio_attachment_parts(audio_files)

    user_content = build_user_content(
        prompt if prompt is not None else "",
        transcript_sections,
        image_parts + audio_attachment_parts + other_parts,
    )
    if not user_content:
        st.warning("Add a message, record audio, or attach a file before sending.")
        st.stop()

    with st.chat_message("user"):
        render_message_content(user_content)

        user_msg = {"role": "user", "content": user_content}

    # Append to memory AND save to database
    st.session_state.messages.append(user_msg)
    save_message(st.session_state.session_id, "user", user_msg["content"])

    with st.chat_message("assistant"):
        if not api_key and provider != "Local":
            st.warning(f"⚠️ Please enter your {provider} API Key.")
        else:
            try:
                client = OpenAI(api_key=api_key or "local", base_url=base_url)
                
                memories = get_session_memories(st.session_state.session_id)
                memory_context = ("\n\n### Session Memory ###\n" + "\n".join(f"- {m}" for m in memories)) if memories else ""
                dynamic_system_prompt = system_prompt + memory_context

                api_messages = build_api_messages(provider, dynamic_system_prompt, st.session_state.messages)

                st.session_state.last_api_messages = api_messages
                logger.info("api_messages=%s", json.dumps(api_messages, ensure_ascii=False))

                stream = client.chat.completions.create(
                    model=model_name,
                    messages=api_messages,
                    stream=True, 
                )
                full_response = st.write_stream(stream)
                
                # Append to memory AND save to database
                st.session_state.messages.append({"role": "assistant", "content": full_response})
                save_message(st.session_state.session_id, "assistant", full_response)

                # Sandbox Execution
                if enable_sandbox:
                    code_blocks = re.findall(r"```python\n(.*?)\n```", full_response, re.DOTALL)
                    for code in code_blocks:
                        st.info("🔧 Running Python code...")
                        f = io.StringIO()
                        with contextlib.redirect_stdout(f):
                            try:
                                exec(code, {})
                                output = f.getvalue()
                            except Exception as e:
                                output = f"Error:\n{str(e)}"
                        
                        if output.strip():
                            st.success("📊 Output:")
                            st.code(output)
                            
                            sys_note = f"System Note: Code executed with output:\n{output}"
                            st.session_state.messages.append({"role": "system", "content": sys_note})
                            save_message(st.session_state.session_id, "system", sys_note)

            except Exception as e:
                st.error(f"An error occurred: {e}")
