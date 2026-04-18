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
import urllib.parse
import urllib.request
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# -------------------------------------------------------------------
# DATABASE FUNCTIONS (LONG-TERM MEMORY)
# -------------------------------------------------------------------
DB_FILE = "chat_history.db"
SKILLS_BASE_DIR = os.path.join(os.path.dirname(__file__), "skills", "request-flows")
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
            memory_scope TEXT DEFAULT 'session',
            fact TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute("PRAGMA table_info(long_term_memory)")
    memory_columns = [row[1] for row in c.fetchall()]
    if "session_id" not in memory_columns:
        c.execute("ALTER TABLE long_term_memory ADD COLUMN session_id TEXT")
    if "memory_scope" not in memory_columns:
        c.execute("ALTER TABLE long_term_memory ADD COLUMN memory_scope TEXT DEFAULT 'session'")
    c.execute("UPDATE long_term_memory SET memory_scope = 'session' WHERE memory_scope IS NULL OR memory_scope = ''")
    c.execute('CREATE INDEX IF NOT EXISTS idx_messages_session_timestamp ON messages(session_id, timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_memory_session_timestamp ON long_term_memory(session_id, timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_memory_scope_timestamp ON long_term_memory(memory_scope, timestamp)')
    conn.commit()
    conn.close()

def save_personal_memory(fact):
    """Saves a global personal memory fact shared across sessions."""
    cleaned_fact = (fact or "").strip()
    if not cleaned_fact:
        return

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM long_term_memory WHERE memory_scope = 'global_personal' AND fact = ? LIMIT 1",
        (cleaned_fact,),
    )
    if c.fetchone() is None:
        c.execute(
            "INSERT INTO long_term_memory (session_id, memory_scope, fact) VALUES (?, ?, ?)",
            (None, "global_personal", cleaned_fact),
        )
    conn.commit()
    conn.close()

def get_personal_memories(limit=20):
    """Retrieves global personal memories ordered by recency."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT fact FROM long_term_memory WHERE memory_scope = 'global_personal' ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    )
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def delete_personal_memory_fact(fact):
    """Deletes a single saved global personal memory fact."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "DELETE FROM long_term_memory WHERE memory_scope = 'global_personal' AND fact = ?",
        (fact,),
    )
    conn.commit()
    conn.close()

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

def fetch_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))

def load_request_flow_skills():
    skills = []
    if not os.path.isdir(SKILLS_BASE_DIR):
        return skills

    for entry in sorted(os.listdir(SKILLS_BASE_DIR)):
        flow_path = os.path.join(SKILLS_BASE_DIR, entry, "flow.json")
        if not os.path.isfile(flow_path):
            continue
        try:
            with open(flow_path, "r", encoding="utf-8") as flow_file:
                config = json.load(flow_file)
            config["slug"] = entry
            skills.append(config)
        except Exception as skill_error:
            logger.warning("Failed to load skill config %s: %s", flow_path, skill_error)

    return sorted(skills, key=lambda item: item.get("priority", 0), reverse=True)

def skill_matches_message(skill, text, has_images=False, has_audio=False):
    lowered = (text or "").lower().strip()
    if not lowered:
        return False

    constraints = skill.get("constraints", {})
    if has_images and not constraints.get("allow_images", False):
        return False
    if has_audio and not constraints.get("allow_audio", False):
        return False

    match = skill.get("match", {})
    strong_terms = [term.lower() for term in match.get("strong_terms", [])]
    weak_terms = [term.lower() for term in match.get("weak_terms", [])]
    question_cues = [term.lower() for term in match.get("question_cues", [])]

    if any(term in lowered for term in strong_terms):
        return True
    if weak_terms and any(term in lowered for term in weak_terms):
        if not question_cues:
            return True
        return any(cue in lowered for cue in question_cues)
    return False

def select_request_flow_skill(skills, text, has_images=False, has_audio=False):
    for skill in skills:
        if skill_matches_message(skill, text, has_images=has_images, has_audio=has_audio):
            return skill
    return None

def extract_weather_location(text):
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    patterns = [
        r"(?:weather|forecast|temperature|temp|humidity|wind|rain|raining|snow|snowing)\s+(?:in|at|for|near)\s+([A-Za-z0-9 ,.\-]+)\??$",
        r"(?:in|at|for|near)\s+([A-Za-z0-9 ,.\-]+)\s+(?:weather|forecast|temperature|temp)\??$",
        r"(?:how(?:'s| is)?|what(?:'s| is)?)\s+(?:the\s+)?(?:weather|forecast|temperature|temp)\s+(?:in|at|for|near)\s+([A-Za-z0-9 ,.\-]+)\??$",
        r"(?:do i need|will it|is it|should i bring).*(?:in|at|for|near)\s+([A-Za-z0-9 ,.\-]+)\??$",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            location = match.group(1).strip(" ?.!,")
            location = re.sub(
                r"\b(today|tomorrow|now|right now|this morning|this afternoon|this evening|tonight)\b",
                "",
                location,
                flags=re.IGNORECASE,
            ).strip(" ,.-")
            if location:
                return location

    fallback = re.search(r"\b(?:in|at|near)\s+([A-Za-z][A-Za-z0-9 ,.\-]+)\??$", cleaned, flags=re.IGNORECASE)
    if fallback:
        location = fallback.group(1).strip(" ?.!,")
        location = re.sub(
            r"\b(today|tomorrow|now|right now|this morning|this afternoon|this evening|tonight)\b",
            "",
            location,
            flags=re.IGNORECASE,
        ).strip(" ,.-")
        return location
    return ""

def detect_weather_time_scope(text):
    lowered = (text or "").lower()
    if "tomorrow" in lowered:
        return "tomorrow"
    if "today" in lowered:
        return "today"
    return "current"

def is_umbrella_question(text):
    lowered = (text or "").lower()
    return "umbrella" in lowered or "raincoat" in lowered

def weather_code_to_text(code):
    mapping = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        56: "light freezing drizzle",
        57: "dense freezing drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        66: "light freezing rain",
        67: "heavy freezing rain",
        71: "slight snow fall",
        73: "moderate snow fall",
        75: "heavy snow fall",
        77: "snow grains",
        80: "slight rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        85: "slight snow showers",
        86: "heavy snow showers",
        95: "thunderstorm",
        96: "thunderstorm with slight hail",
        99: "thunderstorm with heavy hail",
    }
    return mapping.get(code, "unknown conditions")

def resolve_location_from_ip():
    location_data = fetch_json("https://ipapi.co/json/")
    latitude = location_data.get("latitude")
    longitude = location_data.get("longitude")
    if latitude is None or longitude is None:
        raise ValueError("IP geolocation did not return coordinates.")

    name_parts = [
        location_data.get("city"),
        location_data.get("region"),
        location_data.get("country_name"),
    ]
    location_name = ", ".join(part for part in name_parts if part)
    return {
        "name": location_name or "your current area",
        "latitude": latitude,
        "longitude": longitude,
        "timezone": location_data.get("timezone") or "auto",
        "source": "ip",
    }

def resolve_location_from_query(location_query):
    encoded_query = urllib.parse.quote(location_query)
    geocode_url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_query}&count=1&language=en&format=json"
    geocode_data = fetch_json(geocode_url)
    results = geocode_data.get("results") or []
    if not results:
        raise ValueError(f"Could not find a location matching '{location_query}'.")

    result = results[0]
    name_parts = [
        result.get("name"),
        result.get("admin1"),
        result.get("country"),
    ]
    location_name = ", ".join(part for part in name_parts if part)
    return {
        "name": location_name,
        "latitude": result["latitude"],
        "longitude": result["longitude"],
        "timezone": result.get("timezone") or "auto",
        "source": "query",
    }

def lookup_weather(location_query="", time_scope="current"):
    location = resolve_location_from_query(location_query) if location_query else resolve_location_from_ip()

    if time_scope == "tomorrow":
        params = urllib.parse.urlencode(
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,precipitation_sum,rain_sum,showers_sum,wind_speed_10m_max"
                ),
                "forecast_days": 3,
                "timezone": location["timezone"],
            }
        )
    else:
        params = urllib.parse.urlencode(
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "timezone": location["timezone"],
            }
        )
    weather_url = f"https://api.open-meteo.com/v1/forecast?{params}"
    weather_data = fetch_json(weather_url)

    if time_scope == "tomorrow":
        daily = weather_data.get("daily") or {}
        dates = daily.get("time") or []
        if len(dates) < 2:
            raise ValueError("Weather service did not return a forecast for tomorrow.")

        index = 1
        weather_code = daily.get("weather_code", [None])[index]
        return {
            "location_name": location["name"],
            "location_source": location["source"],
            "time_scope": "tomorrow",
            "date": dates[index],
            "weather_code": weather_code,
            "weather_text": weather_code_to_text(weather_code),
            "temp_max_c": daily.get("temperature_2m_max", [None])[index],
            "temp_min_c": daily.get("temperature_2m_min", [None])[index],
            "precipitation_probability_max": daily.get("precipitation_probability_max", [None])[index],
            "precipitation_sum": daily.get("precipitation_sum", [None])[index],
            "rain_sum": daily.get("rain_sum", [None])[index],
            "showers_sum": daily.get("showers_sum", [None])[index],
            "wind_kmh": daily.get("wind_speed_10m_max", [None])[index],
        }

    current = weather_data.get("current") or {}
    if not current:
        raise ValueError("Weather service did not return current conditions.")

    return {
        "location_name": location["name"],
        "location_source": location["source"],
        "time_scope": "current",
        "temperature_c": current.get("temperature_2m"),
        "apparent_temperature_c": current.get("apparent_temperature"),
        "humidity": current.get("relative_humidity_2m"),
        "wind_kmh": current.get("wind_speed_10m"),
        "weather_code": current.get("weather_code"),
        "weather_text": weather_code_to_text(current.get("weather_code")),
        "time": current.get("time"),
    }

def format_weather_response(weather_result, user_text="", used_ip_fallback=False):
    location_label = weather_result["location_name"]
    source_line = "I used your IP-based location." if used_ip_fallback else "I used the location you specified."

    if weather_result.get("time_scope") == "tomorrow":
        precip_probability = weather_result.get("precipitation_probability_max")
        precip_sum = weather_result.get("precipitation_sum")
        umbrella_question = is_umbrella_question(user_text)
        precip_signal = (
            (precip_probability is not None and precip_probability >= 40)
            or (precip_sum is not None and precip_sum > 1)
            or "rain" in weather_result["weather_text"]
            or "shower" in weather_result["weather_text"]
            or "thunderstorm" in weather_result["weather_text"]
        )

        if umbrella_question:
            recommendation = "Yes." if precip_signal else "No."
            reason = (
                f"Rain is likely tomorrow in {location_label}."
                if precip_signal
                else f"Rain does not look likely tomorrow in {location_label}."
            )
            return (
                f"{recommendation} {reason} Forecast: {weather_result['weather_text']}, "
                f"{weather_result['temp_min_c']} to {weather_result['temp_max_c']}°C, "
                f"precipitation chance {precip_probability}%, expected precipitation {precip_sum} mm.\n\n"
                f"{source_line} Forecast date: {weather_result['date']}."
            )

        return (
            f"Tomorrow's forecast for {location_label}: {weather_result['weather_text']}, "
            f"{weather_result['temp_min_c']} to {weather_result['temp_max_c']}°C, "
            f"precipitation chance {precip_probability}%, expected precipitation {precip_sum} mm, "
            f"wind up to {weather_result['wind_kmh']} km/h.\n\n"
            f"{source_line} Forecast date: {weather_result['date']}."
        )

    return (
        f"Current weather for {location_label}: {weather_result['weather_text']}, "
        f"{weather_result['temperature_c']}°C"
        f" (feels like {weather_result['apparent_temperature_c']}°C), "
        f"humidity {weather_result['humidity']}%, wind {weather_result['wind_kmh']} km/h.\n\n"
        f"{source_line} Observation time: {weather_result['time']}."
    )

def persist_user_message(user_content, session_id):
    user_msg = {"role": "user", "content": user_content}
    st.session_state.messages.append(user_msg)
    save_message(session_id, "user", user_msg["content"])
    return user_msg

def extract_and_store_personal_memories(user_text, remember_personal_info, can_extract_memory, openai_route_config, memory_model):
    saved_memory_facts = []
    if not remember_personal_info or not user_text:
        return saved_memory_facts, None

    if not can_extract_memory:
        return saved_memory_facts, "Personal memory is enabled, but no OpenAI memory route is configured."

    try:
        memory_client = OpenAI(
            api_key=openai_route_config.get("api_key") or "local",
            base_url=openai_route_config["base_url"],
        )
        extracted_memories = extract_personal_memories(
            memory_client,
            memory_model,
            user_text,
        )
        for fact in extracted_memories:
            save_personal_memory(fact)
            saved_memory_facts.append(fact)
        return saved_memory_facts, None
    except Exception as memory_error:
        return saved_memory_facts, f"Memory extraction skipped: {memory_error}"

def execute_weather_skill(user_text):
    location_query = extract_weather_location(user_text)
    time_scope = detect_weather_time_scope(user_text)
    weather_result = lookup_weather(location_query, time_scope=time_scope)
    return format_weather_response(
        weather_result,
        user_text=user_text,
        used_ip_fallback=not bool(location_query),
    )

def execute_skill(skill, user_text):
    executor = skill.get("executor")
    if executor == "weather_lookup":
        return execute_weather_skill(user_text)
    raise ValueError(f"Unsupported skill executor: {executor}")

def generate_model_reply(user_content, saved_memory_facts, image_parts, audio_attachment_parts, analyze_recording, route_configs, system_prompt):
    selected_route, route_reason = route_request(
        user_content,
        route_configs,
        has_images=bool(image_parts),
        has_audio=bool(audio_attachment_parts),
        analyze_recording=analyze_recording,
    )
    if not selected_route:
        raise ValueError(f"Auto routing could not find a usable model: {route_reason}")

    active_provider = selected_route["provider"]
    active_api_key = selected_route.get("api_key", "")
    active_base_url = selected_route["base_url"]
    active_model_name = selected_route["model"]
    st.session_state.last_route_decision = {
        "provider": active_provider,
        "model": active_model_name,
        "base_url": active_base_url,
        "reason": route_reason,
    }

    with st.chat_message("assistant"):
        if not active_api_key and active_provider != "Local":
            st.warning(f"⚠️ Please enter your {active_provider} API Key.")
            return

        st.caption(f"Selected route: `{active_provider}` -> `{active_model_name}`")
        if saved_memory_facts:
            st.caption("Saved to memory: " + "; ".join(saved_memory_facts))

        client = OpenAI(api_key=active_api_key or "local", base_url=active_base_url)
        personal_memories = get_personal_memories()
        personal_memory_context = (
            "\n\n### Global Personal Memory ###\n" + "\n".join(f"- {m}" for m in personal_memories)
        ) if personal_memories else ""
        dynamic_system_prompt = system_prompt + personal_memory_context

        api_messages = build_api_messages(active_provider, dynamic_system_prompt, st.session_state.messages)
        st.session_state.last_api_messages = api_messages
        logger.info("api_messages=%s", json.dumps(api_messages, ensure_ascii=False))

        stream = client.chat.completions.create(
            model=active_model_name,
            messages=api_messages,
            stream=True,
        )
        full_response = st.write_stream(stream)

        st.session_state.messages.append({"role": "assistant", "content": full_response})
        save_message(st.session_state.session_id, "assistant", full_response)

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

def extract_text_from_content(content):
    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return str(content).strip()

    return "\n".join(
        item.get("text", "").strip()
        for item in content
        if item.get("type") == "text" and item.get("text", "").strip()
    ).strip()

def normalize_model_name(provider, model_name):
    cleaned = (model_name or "").strip()

    if provider == "Gemini" and cleaned == "gemini-3-flash":
        return "gemini-2.5-flash"

    return cleaned

def get_provider_defaults(provider_name):
    if provider_name == "Gemini":
        return {
            "provider": "Gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "model": "gemini-2.5-flash",
            "api_key": os.getenv("API_KEY_GEMINI", ""),
        }
    if provider_name == "OpenAI":
        return {
            "provider": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o",
            "api_key": os.getenv("API_KEY_OPENAI", ""),
        }
    return {
        "provider": "Local",
        "base_url": "http://localhost:11434/v1",
        "model": "llama3",
        "api_key": "",
    }

def looks_complex(text):
    lowered = (text or "").lower()
    complexity_markers = [
        "debug",
        "refactor",
        "analyze",
        "architecture",
        "design",
        "algorithm",
        "compare",
        "tradeoff",
        "explain",
        "optimize",
        "performance",
        "security",
        "bug",
        "code",
        "python",
        "javascript",
        "sql",
    ]
    return len(text) > 500 or any(marker in lowered for marker in complexity_markers)

def pick_available_route(candidate_names, route_configs):
    for candidate_name in candidate_names:
        config = route_configs.get(candidate_name)
        if not config:
            continue
        if candidate_name == "Local":
            return config
        if config.get("api_key"):
            return config
    return None

def route_request(user_content, route_configs, has_images=False, has_audio=False, analyze_recording=False):
    prompt_text = extract_text_from_content(user_content)

    if has_audio:
        route = pick_available_route(["Gemini", "OpenAI"], route_configs)
        if route:
            return route, "explicit recording analysis routed to multimodal-capable model"
        return None, "recording analysis requires an available Gemini or OpenAI route"

    if analyze_recording or has_images:
        route = pick_available_route(["Gemini", "OpenAI"], route_configs)
        if route:
            return route, "multimodal content routed to vision-capable model"

    if looks_complex(prompt_text):
        route = pick_available_route(["OpenAI", "Gemini"], route_configs)
        if route:
            return route, "complex text/code request routed to stronger hosted model"

    route = pick_available_route(["Local", "Gemini", "OpenAI"], route_configs)
    if route:
        return route, "simple text request routed to fastest available model"

    return None, "no configured route is currently available"

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

def extract_personal_memories(client, model_name, user_text):
    """Extract stable, user-stated personal facts/preferences as JSON."""
    cleaned_text = (user_text or "").strip()
    if not cleaned_text:
        return []

    response = client.chat.completions.create(
        model=model_name,
        stream=False,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Extract only stable personal information the user explicitly states about themselves. "
                    "Prefer preferences, biography, habits, goals, location, role, recurring needs, or named constraints. "
                    "Do not infer. Do not include temporary task details. "
                    "Return JSON with this exact shape: {\"memories\": [\"...\"]}. "
                    "Each memory must be short, factual, and under 140 characters. "
                    "If nothing should be remembered, return {\"memories\": []}."
                ),
            },
            {"role": "user", "content": cleaned_text},
        ],
    )
    payload = response.choices[0].message.content or "{\"memories\": []}"
    data = json.loads(payload)
    raw_memories = data.get("memories", [])
    if not isinstance(raw_memories, list):
        return []

    normalized = []
    for item in raw_memories[:5]:
        if not isinstance(item, str):
            continue
        fact = item.strip()
        if not fact or len(fact) > 140:
            continue
        normalized.append(fact)
    return normalized

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

if "last_route_decision" not in st.session_state:
    st.session_state.last_route_decision = {}

if "input_widget_nonce" not in st.session_state:
    st.session_state.input_widget_nonce = 0

if "pending_skill_request" not in st.session_state:
    st.session_state.pending_skill_request = None

# -------------------------------------------------------------------
# SIDEBAR: Chat History & Settings
# -------------------------------------------------------------------
st.sidebar.title("📚 Chat History")

# New Chat Button
if st.sidebar.button("➕ New Chat"):
    st.session_state.session_id = str(uuid.uuid4())
    st.session_state.messages = []
    st.session_state.last_api_messages = []
    st.session_state.last_route_decision = {}
    st.session_state.pending_skill_request = None
    st.session_state.input_widget_nonce += 1
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
            st.session_state.last_api_messages = []
            st.session_state.last_route_decision = {}
            st.session_state.pending_skill_request = None
            st.session_state.input_widget_nonce += 1
            st.rerun()
    with col2:
        if st.button("❌", key=f"del_{past_session}"):
            delete_session(past_session)
            if st.session_state.session_id == past_session:
                st.session_state.session_id = str(uuid.uuid4())
                st.session_state.messages = []
                st.session_state.last_api_messages = []
                st.session_state.last_route_decision = {}
                st.session_state.pending_skill_request = None
                st.session_state.input_widget_nonce += 1
            st.rerun()

st.sidebar.divider()
st.sidebar.title("⚙️ LLM Settings")

provider = st.sidebar.selectbox("Select Provider", ["Auto", "Gemini", "OpenAI", "Local"])

if provider == "Auto":
    openai_defaults = get_provider_defaults("OpenAI")
    gemini_defaults = get_provider_defaults("Gemini")
    local_defaults = get_provider_defaults("Local")

    st.sidebar.caption("Auto routing chooses a model from the route table below for each message.")
    openai_key = st.sidebar.text_input("OpenAI API Key", type="password", value=openai_defaults["api_key"])
    openai_base_url = st.sidebar.text_input("OpenAI Base URL", value=openai_defaults["base_url"])
    openai_model = st.sidebar.text_input("OpenAI Model", value=openai_defaults["model"])
    gemini_key = st.sidebar.text_input("Gemini API Key", type="password", value=gemini_defaults["api_key"])
    gemini_base_url = st.sidebar.text_input("Gemini Base URL", value=gemini_defaults["base_url"])
    gemini_model = st.sidebar.text_input("Gemini Model", value=gemini_defaults["model"])
    local_base_url = st.sidebar.text_input("Local Base URL", value=local_defaults["base_url"])
    local_model = st.sidebar.text_input("Local Model", value=local_defaults["model"])

    route_configs = {
        "OpenAI": {
            "provider": "OpenAI",
            "api_key": openai_key,
            "base_url": openai_base_url,
            "model": normalize_model_name("OpenAI", openai_model),
        },
        "Gemini": {
            "provider": "Gemini",
            "api_key": gemini_key,
            "base_url": gemini_base_url,
            "model": normalize_model_name("Gemini", gemini_model),
        },
        "Local": {
            "provider": "Local",
            "api_key": "",
            "base_url": local_base_url,
            "model": normalize_model_name("Local", local_model),
        },
    }

    api_key = ""
    base_url = ""
    model_name = ""
else:
    provider_defaults = get_provider_defaults(provider)
    api_key = st.sidebar.text_input(f"{provider} API Key", type="password", value=provider_defaults["api_key"])
    base_url = st.sidebar.text_input("Base URL", value=provider_defaults["base_url"])
    model_name = st.sidebar.text_input("Model Name", value=provider_defaults["model"])
    model_name = normalize_model_name(provider, model_name)
    route_configs = {
        provider: {
            "provider": provider,
            "api_key": api_key,
            "base_url": base_url,
            "model": model_name,
        }
    }
    if provider == "Gemini" and model_name == "gemini-2.5-flash":
        st.sidebar.caption("Using Gemini OpenAI-compatible model `gemini-2.5-flash`.")

enable_audio_transcription = st.sidebar.checkbox("🎙️ Transcribe audio uploads", value=True)
transcription_model = st.sidebar.text_input(
    "Transcription Model",
    value="gpt-4o-mini-transcribe" if provider in {"Auto", "OpenAI"} else "",
    help="Used for all audio inputs before chat submission. If unavailable, audio messages are blocked.",
)
remember_personal_info = st.sidebar.checkbox("🧠 Remember personal info", value=True)
memory_model = st.sidebar.text_input(
    "Memory Model",
    value="gpt-4o-mini" if provider in {"Auto", "OpenAI"} else "",
    help="Used to extract stable user preferences and personal facts from your messages.",
)
enable_weather_skill = st.sidebar.checkbox("🧩 Enable request-flow skills", value=True)

enable_sandbox = st.sidebar.checkbox("🐍 Enable Python Sandbox", value=False)
show_debug_payload = st.sidebar.checkbox("🪵 Show API payload", value=True)
system_prompt = st.sidebar.text_area("System Prompt:", value="You are a brilliant secretary.", height=100)

openai_route_config = route_configs.get("OpenAI")
can_transcribe_audio = bool(openai_route_config and openai_route_config.get("api_key") and transcription_model.strip())
can_extract_memory = bool(openai_route_config and openai_route_config.get("api_key") and memory_model.strip())
request_flow_skills = load_request_flow_skills()
if not can_transcribe_audio:
    st.sidebar.caption("Audio transcription needs an OpenAI route plus a transcription model.")
if remember_personal_info and not can_extract_memory:
    st.sidebar.caption("Personal memory extraction needs an OpenAI route plus a memory model.")
if request_flow_skills:
    st.sidebar.caption("Loaded request-flow skills: " + ", ".join(skill.get("display_name", skill["slug"]) for skill in request_flow_skills))

st.sidebar.divider()
st.sidebar.subheader("Global Personal Memory")
personal_memories = get_personal_memories()
if personal_memories:
    for memory_fact in personal_memories:
        memory_col, delete_col = st.sidebar.columns([4, 1])
        with memory_col:
            st.caption(memory_fact)
        with delete_col:
            if st.button("🗑️", key=f"global_mem_{memory_fact}"):
                delete_personal_memory_fact(memory_fact)
                st.rerun()
else:
    st.sidebar.caption("No saved global personal memory yet.")

st.sidebar.caption("Short-term session memory is the current chat history in this session.")

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

if st.session_state.last_route_decision:
    with st.expander("Last Route Decision", expanded=False):
        st.json(st.session_state.last_route_decision)

if st.session_state.pending_skill_request:
    pending = st.session_state.pending_skill_request
    with st.chat_message("user"):
        render_message_content(pending["user_content"])
    with st.chat_message("assistant"):
        st.markdown(pending["prompt"])
        activate_skill = st.button("Activate skill", key="activate_pending_skill")
        use_normal_chat = st.button("Use normal chat", key="decline_pending_skill")
        cancel_request = st.button("Cancel", key="cancel_pending_skill")

        if activate_skill:
            saved_memory_facts, memory_notice = extract_and_store_personal_memories(
                pending["user_text"],
                remember_personal_info,
                can_extract_memory,
                openai_route_config,
                memory_model,
            )
            if memory_notice:
                st.caption(memory_notice)
            persist_user_message(pending["user_content"], st.session_state.session_id)
            try:
                assistant_response = execute_skill(pending["skill"], pending["user_text"])
                st.caption(
                    f"Selected route: `{pending['skill'].get('display_name', pending['skill']['slug'])}` -> `{pending['skill'].get('executor', 'builtin')}`"
                )
                if saved_memory_facts:
                    st.caption("Saved to memory: " + "; ".join(saved_memory_facts))
                st.markdown(assistant_response)
                st.session_state.messages.append({"role": "assistant", "content": assistant_response})
                save_message(st.session_state.session_id, "assistant", assistant_response)
                st.session_state.last_route_decision = {
                    "provider": pending["skill"].get("display_name", pending["skill"]["slug"]),
                    "model": pending["skill"].get("executor", "builtin"),
                    "base_url": "local-app",
                    "reason": "approved skill activation",
                }
                st.session_state.last_api_messages = []
            except Exception as skill_error:
                st.error(f"Skill execution failed: {skill_error}")
            st.session_state.pending_skill_request = None
            st.rerun()

        if use_normal_chat:
            saved_memory_facts, memory_notice = extract_and_store_personal_memories(
                pending["user_text"],
                remember_personal_info,
                can_extract_memory,
                openai_route_config,
                memory_model,
            )
            if memory_notice:
                st.caption(memory_notice)
            persist_user_message(pending["user_content"], st.session_state.session_id)
            st.session_state.pending_skill_request = None
            generate_model_reply(
                pending["user_content"],
                saved_memory_facts,
                pending["image_parts"],
                pending["audio_attachment_parts"],
                pending["analyze_recording"],
                route_configs,
                system_prompt,
            )
            st.rerun()

        if cancel_request:
            st.session_state.pending_skill_request = None
            st.rerun()

# -------------------------------------------------------------------
# CHAT INPUT & EXECUTION
# -------------------------------------------------------------------
prompt_col, voice_col, upload_col, send_col = st.columns([8, 1.6, 1.8, 1.8], vertical_alignment="bottom")

with prompt_col:
    prompt = st.chat_input("What's on your mind?")

with voice_col:
    recorded_audio = st.audio_input(
        "Voice",
        sample_rate=16000,
        key=f"recorded_audio_{st.session_state.input_widget_nonce}",
        help="Record a voice message with your microphone.",
        label_visibility="collapsed",
        width="stretch",
    )
    st.caption("Voice")

with upload_col:
    uploaded_files = st.file_uploader(
        "Upload",
        type=["jpg", "jpeg", "png", "mp3", "wav", "m4a", "ogg"],
        accept_multiple_files=True,
        key=f"uploaded_files_{st.session_state.input_widget_nonce}",
        label_visibility="collapsed",
    )
    st.caption("Upload")

with send_col:
    send_attachments_only = st.button(
        "Send",
        disabled=not (recorded_audio is not None or uploaded_files),
        help="Send recorded audio or uploaded files without typing a text message.",
        use_container_width=True,
    )

analyze_recording = st.checkbox(
    "Analyze the recording itself",
    value=False,
    key=f"analyze_recording_{st.session_state.input_widget_nonce}",
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

submitted = prompt is not None or send_attachments_only

if submitted:
    image_parts, audio_files, other_parts = split_uploaded_files(pending_files)
    audio_attachment_parts = []
    transcript_sections = []

    if audio_files:
        if not enable_audio_transcription:
            st.warning("Enable `Transcribe audio uploads` to send audio. Audio messages are transcript-first.")
            st.stop()
        if not can_transcribe_audio:
            st.warning("Audio messages require a working transcription model. Switch to the OpenAI provider or configure transcription first.")
            st.stop()
        transcription_route = pick_available_route(["OpenAI"], route_configs)
        if not transcription_route:
            st.warning("Audio transcription requires an available OpenAI route.")
            st.stop()
        transcription_client = OpenAI(
            api_key=transcription_route.get("api_key") or "local",
            base_url=transcription_route["base_url"],
        )

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

    user_text = extract_text_from_content(user_content)
    matched_skill = None
    if enable_weather_skill:
        matched_skill = select_request_flow_skill(
            request_flow_skills,
            user_text,
            has_images=bool(image_parts),
            has_audio=bool(audio_attachment_parts),
        )

    if matched_skill and matched_skill.get("requires_activation", True):
        activation_prompt = matched_skill.get(
            "activation_prompt",
            f"I found the skill `{matched_skill.get('display_name', matched_skill['slug'])}` for this request. Would you like to activate it?",
        )
        st.session_state.pending_skill_request = {
            "skill": matched_skill,
            "prompt": activation_prompt,
            "user_content": user_content,
            "user_text": user_text,
            "image_parts": image_parts,
            "audio_attachment_parts": audio_attachment_parts,
            "analyze_recording": analyze_recording,
        }
        st.rerun()

    with st.chat_message("user"):
        render_message_content(user_content)

    user_msg = persist_user_message(user_content, st.session_state.session_id)
    saved_memory_facts, memory_notice = extract_and_store_personal_memories(
        user_text,
        remember_personal_info,
        can_extract_memory,
        openai_route_config,
        memory_model,
    )
    if memory_notice:
        st.caption(memory_notice)

    if matched_skill:
        try:
            assistant_response = execute_skill(matched_skill, user_text)
            with st.chat_message("assistant"):
                st.caption(
                    f"Selected route: `{matched_skill.get('display_name', matched_skill['slug'])}` -> `{matched_skill.get('executor', 'builtin')}`"
                )
                if saved_memory_facts:
                    st.caption("Saved to memory: " + "; ".join(saved_memory_facts))
                st.markdown(assistant_response)
            st.session_state.messages.append({"role": "assistant", "content": assistant_response})
            save_message(st.session_state.session_id, "assistant", assistant_response)
            st.session_state.last_route_decision = {
                "provider": matched_skill.get("display_name", matched_skill["slug"]),
                "model": matched_skill.get("executor", "builtin"),
                "base_url": "local-app",
                "reason": "skill executed without activation gate",
            }
            st.session_state.last_api_messages = []
            st.stop()
        except Exception as skill_error:
            st.error(f"Skill execution failed: {skill_error}")
            st.stop()

    try:
        generate_model_reply(
            user_content,
            saved_memory_facts,
            image_parts,
            audio_attachment_parts,
            analyze_recording,
            route_configs,
            system_prompt,
        )
    except Exception as e:
        st.error(f"An error occurred: {e}")
