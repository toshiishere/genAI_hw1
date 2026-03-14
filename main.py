import streamlit as st
from openai import OpenAI

# -------------------------------------------------------------------
# SIDEBAR: Customization (Provider, API Keys, Models, Prompts, Params)
# -------------------------------------------------------------------
st.sidebar.title("⚙️ LLM Settings")

# 1. Select the Provider
provider = st.sidebar.selectbox(
    "Select Provider", 
    ["Gemini", "OpenAI", "Local (e.g., Ollama, LM Studio)"]
)

# 2. Set defaults based on the chosen provider
if provider == "Gemini":
    default_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    default_model = "gemini-2.5-flash"
elif provider == "OpenAI":
    default_url = "https://api.openai.com/v1"
    default_model = "gpt-4o"
else:
    default_url = "http://localhost:11434/v1" # Standard Ollama port
    default_model = "llama3"

# 3. API Configuration Inputs
st.sidebar.subheader("API Configuration")
api_key = st.sidebar.text_input(f"{provider} API Key", type="password", help="Leave blank if using Local models.")
base_url = st.sidebar.text_input("Base URL", value=default_url)
model_name = st.sidebar.text_input("Model Name", value=default_model)

# 4. System Prompt Customization
st.sidebar.subheader("System Prompt")
system_prompt = st.sidebar.text_area("Define the AI's behavior:", value="You are a helpful, brilliant assistant.", height=150)

# 5. API Parameters Customization
st.sidebar.subheader("API Parameters")
temperature = st.sidebar.slider("Temperature (Creativity)", min_value=0.0, max_value=2.0, value=0.7, step=0.1)
max_tokens = st.sidebar.slider("Max Tokens", min_value=100, max_value=8192, value=1000, step=100)

# -------------------------------------------------------------------
# MAIN UI & SHORT-TERM MEMORY
# -------------------------------------------------------------------
st.title("My Personal LLM 🧠")

# Initialize short-term memory in Streamlit's session state
if "messages" not in st.session_state:
    st.session_state.messages = []

# Clear chat history button
if st.sidebar.button("Clear Chat History"):
    st.session_state.messages = []
    st.rerun()

# Render existing conversation (Memory)
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# -------------------------------------------------------------------
# CHAT INPUT & STREAMING RESPONSE
# -------------------------------------------------------------------
# Await user input
if prompt := st.chat_input("What's on your mind?"):
    
    # 1. Append user message to memory and display it
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # 2. Generate and stream the assistant's response
    with st.chat_message("assistant"):
        # Require API key for cloud providers
        if not api_key and provider != "Local (e.g., Ollama, LM Studio)":
            st.warning(f"⚠️ Please enter your {provider} API Key in the sidebar.")
        else:
            try:
                # Initialize the client with user's custom settings
                client = OpenAI(
                    api_key=api_key or "local-key-not-needed", 
                    base_url=base_url
                )

                # Construct the full message payload: System prompt + History
                api_messages = [{"role": "system", "content": system_prompt}] + st.session_state.messages

                # Call the API with streaming enabled
                stream = client.chat.completions.create(
                    model=model_name,
                    messages=api_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True, 
                )

                # Streamlit handles the streaming chunks automatically
                full_response = st.write_stream(stream)
                
                # Append the final assistant response to short-term memory
                st.session_state.messages.append({"role": "assistant", "content": full_response})

            except Exception as e:
                st.error(f"An error occurred: {e}")