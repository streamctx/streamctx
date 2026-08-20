"""
StreamCtx Live Demo — Streamlit playground (v2, matches real tracker.py API)

Deploy this on Streamlit Community Cloud so people can see StreamCtx working
before they `pip install` it.

Key correction from v1: StreamCtx does NOT support manual
tracker.checkpoint(step=..., data=...) calls. Checkpointing happens
automatically, internally, every time a call goes through a client wrapped
with tracker.wrap(client). So this demo uses a small fake OpenAI-shaped
client (same interface StreamCtx patches: client.chat.completions.create)
to drive real, wrapped calls through the actual installed streamctx package.

Run locally:
    pip install streamlit streamctx
    python -m streamlit run streamlit_demo_v2.py
"""

import time
import streamlit as st

import streamctx

st.set_page_config(page_title="StreamCtx Live Demo", page_icon="🩺", layout="centered")

st.title("🩺 StreamCtx Live Demo")
st.caption(
    "Your AI agent is silently corrupting its own context. "
    "StreamCtx catches it — watch it happen below."
)

TOTAL_STEPS = 8
POISON_STEP = 4  # the step where the fake tool call breaks


# --- a minimal fake client shaped exactly like the OpenAI SDK ------------
# StreamCtx's wrap() detects clients via: hasattr(client, "chat") and
# hasattr(client.chat, "completions"). We mimic that shape so wrap() patches
# this the same way it would patch a real openai.OpenAI() client.

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeUsage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeResponse:
    def __init__(self, content, prompt_tokens=60, completion_tokens=25):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(prompt_tokens, completion_tokens)


class _FakeCompletions:
    def __init__(self):
        self.call_count = 0

    def create(self, *args, **kwargs):
        self.call_count += 1
        step = self.call_count
        if step == POISON_STEP:
            # Simulates a broken/malformed tool response mid-session
            raise ValueError(f"Simulated malformed tool response at step {step}")
        return _FakeResponse(f"step {step} completed ok")


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()


# --- streamlit session state ---------------------------------------------
if "log" not in st.session_state:
    st.session_state.log = []
if "ran_once" not in st.session_state:
    st.session_state.ran_once = False
if "session_id" not in st.session_state:
    st.session_state.session_id = None

log_box = st.container()


def log(line):
    st.session_state.log.append(line)
    with log_box:
        st.write(line)


col1, col2 = st.columns(2)
run_clicked = col1.button("▶ Run demo session (crashes + self-heals at step 4)")
resume_clicked = col2.button("🔁 Resume last session from checkpoint")

if run_clicked:
    st.session_state.log = []

    tracker = streamctx.get_tracker(agent_id="streamlit-demo-agent")
    tracker.start()
    st.session_state.session_id = tracker.get_session_id()

    client = tracker.wrap(_FakeClient())  # this is the actual patched client

    for step in range(1, TOTAL_STEPS + 1):
        try:
            response = client.chat.completions.create(
                model="fake-demo-model",
                messages=[{"role": "user", "content": f"Step {step}: do the next part of the task"}],
            )
            log(f"✅ Step {step}: `{response.choices[0].message.content}`")
        except Exception as e:
            log(
                f"⚠️ Step {step}: tool call failed (`{e}`) — "
                f"**StreamCtx caught it, logged the failure, and attempted self-healing.**"
            )
        time.sleep(0.35)

    tracker.stop()
    st.session_state.ran_once = True

    stats = tracker.get_stats()
    st.success(f"Session `{st.session_state.session_id}` finished — {TOTAL_STEPS} calls attempted.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Calls made", stats.get("call_count", "—"))
    c2.metric("Tokens reused", stats.get("reused_tokens", "—"))
    c3.metric("Biggest waste found", stats.get("biggest_waste") or "none")

if resume_clicked:
    if not st.session_state.session_id:
        st.warning("Run a demo session first.")
    else:
        tracker = streamctx.get_tracker(agent_id="streamlit-demo-agent")
        with st.spinner("Reconstructing session from the last checkpoint..."):
            time.sleep(0.6)
            messages = tracker.resume(session_id=st.session_state.session_id)
        st.success(
            f"Resumed session `{st.session_state.session_id}` — "
            f"reconstructed {len(messages)} messages from the last saved checkpoint, "
            f"no re-work needed on steps before the failure."
        )
        with st.expander("See reconstructed messages"):
            for m in messages:
                st.write(f"**{m.get('role', '?')}**: {m.get('content', '')}")

st.divider()
st.markdown(
    """
    **Try it yourself:**
    ```bash
    pip install streamctx
    ```
    [GitHub](https://github.com/streamctx/streamctx) · [Docs](https://github.com/streamctx/streamctx#readme)
    """
)


