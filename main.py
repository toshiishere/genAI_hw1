import streamlit as st
from openai import OpenAI
import base64
import re
import io
import contextlib
import os
import sqlite3
import json
import uuid
import threading
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# -------------------------------------------------------------------
# DATABASE FUNCTIONS (LONG-TERM MEMORY)
# -------------------------------------------------------------------
DB_FILE = "chat_history.db"

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
            fact TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_memory(fact):
    """Saves an extracted fact to the long-term memory table."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO long_term_memory (fact) VALUES (?)', (fact,))
    conn.commit()
    conn.close()

def get_all_memories():
    """Retrieves all long-term memories."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT fact FROM long_term_memory ORDER BY timestamp ASC')
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
        messages.append({"role": row[0], "content": json.loads(row[1])})
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
    """Deletes all messages for a specific session."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
    conn.commit()
    conn.close()

# Initialize the database when the app starts
init_db()

# -------------------------------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------------------------------
def encode_image(uploaded_file):
    return base64.b64encode(uploaded_file.getvalue()).decode('utf-8')

def background_memory_update(api_key, base_url, model_name, user_text):
    """Runs in the background to extract and save important information."""
    try:
        client = OpenAI(api_key=api_key or "local", base_url=base_url)
        sys_prompt = "You are a memory extraction assistant. Extract any new important facts, personal details, or preferences about the user from the message. If there is nothing worth remembering long-term, output exactly 'NONE'. Otherwise, output a concise summary of the fact."
        
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_text}
            ],
            stream=False
        )
        fact = response.choices[0].message.content.strip()
        if fact and fact.upper() != "NONE":
            save_memory(fact)
    except Exception:
        pass # Fail silently in background to avoid disrupting the UI

# -------------------------------------------------------------------
# STATE MANAGEMENT
# -------------------------------------------------------------------
# Generate a new session ID if one doesn't exist
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

if "messages" not in st.session_state:
    st.session_state.messages = []

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
    default_model = "gemini-3-flash"
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

enable_sandbox = st.sidebar.checkbox("🐍 Enable Python Sandbox", value=False)
system_prompt = st.sidebar.text_area("System Prompt:", value="You are a brilliant researcher.", height=100)

# -------------------------------------------------------------------
# MAIN UI
# -------------------------------------------------------------------
st.title("My Personal LLM 🧠")
st.caption(f"Current Session: `{st.session_state.session_id}`")

# Render existing conversation 
for message in st.session_state.messages:
    if message["role"] == "system" and message["content"] != system_prompt:
        continue
        
    with st.chat_message(message["role"]):
        if isinstance(message["content"], str):
            st.markdown(message["content"])
        elif isinstance(message["content"], list):
            for item in message["content"]:
                if item["type"] == "text":
                    st.markdown(item["text"])
                elif item["type"] == "image_url":
                    st.caption("📎 [Image attached in memory]")

# -------------------------------------------------------------------
# CHAT INPUT & EXECUTION
# -------------------------------------------------------------------
uploaded_image = st.file_uploader("Attach an image (Optional)", type=["jpg", "jpeg", "png"])

if prompt := st.chat_input("What's on your mind?"):
    
    with st.chat_message("user"):
        st.markdown(prompt)
        
        if uploaded_image:
            st.image(uploaded_image, caption="Uploaded Image", width=300)
            base64_img = encode_image(uploaded_image)
            mime_type = uploaded_image.type
            user_msg = {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_img}"}}
                ]
            }
        else:
            user_msg = {"role": "user", "content": prompt}

    # Append to memory AND save to database
    st.session_state.messages.append(user_msg)
    save_message(st.session_state.session_id, "user", user_msg["content"])

    with st.chat_message("assistant"):
        if not api_key and provider != "Local":
            st.warning(f"⚠️ Please enter your {provider} API Key.")
        else:
            try:
                # Fire background memory update
                threading.Thread(
                    target=background_memory_update, 
                    args=(api_key, base_url, model_name, prompt),
                    daemon=True
                ).start()

                client = OpenAI(api_key=api_key or "local", base_url=base_url)
                
                memories = get_all_memories()
                memory_context = ("\n\n### Long-Term Memory ###\n" + "\n".join(f"- {m}" for m in memories)) if memories else ""
                dynamic_system_prompt = system_prompt + memory_context
                
                api_messages = [{"role": "system", "content": dynamic_system_prompt}] + st.session_state.messages

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