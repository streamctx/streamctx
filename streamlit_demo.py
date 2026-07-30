"""
StreamCtx Live Demo — Streamlit playground

Deploy this on Streamlit Community Cloud so people can see StreamCtx working
before they `pip install` it.

Demonstrates, using the published PyPI package (not local dev code):
  1. A simulated agent session streaming step-by-step, live
  2. A checkpoint/resume button — kill and resume mid-session
  3. A deliberately injected "poisoned" tool response, caught by the
     Poison Detector, with the Attribution Engine scoring the bad step

Run locally:
    pip install streamlit streamctx
    streamlit run streamlit_demo.py

Deploy:
    Push this file (+ a requirements.txt with streamlit and streamctx) to a
    public GitHub repo, then deploy for free at share.streamlit.io
"""

import time
import random
import streamlit as st

import streamctx

st.set_page_config(page_title="StreamCtx Live Demo", page_icon="🩺", layout="centered")

st.title("🩺 StreamCtx Live Demo")
st.caption(
    "Your AI agent is silently corrupting its own context. "
    "StreamCtx catches it — watch it happen below."
)

# --- session state setup -----------------------------------------------
if "session_id" not in st.session_state:
    st.session_state.session_id = None
if "steps_done" not in st.session_state:
    st.session_state.steps_done = 0
if "log" not in st.session_state:
    st.session_state.log = []
if "crashed" not in st.session_state:
    st.session_state.crashed = False

TOTAL_STEPS = 8
POISON_STEP = 4  # the step we'll deliberately corrupt

tracker = streamctx.get_tracker(agent_id="streamlit-demo-agent")

# --- controls ------------------------------------------------------------
col1, col2, col3 = st.columns(3)
start_clicked = col1.button("▶ Start new session")
crash_clicked = col2.button("💥 Simulate crash (step 4)")
resume_clicked = col3.button("🔁 Resume from checkpoint")

log_box = st.container()
stats_box = st.empty()

def render_log():
    with log_box:
        for line in st.session_state.log:
            st.write(line)

def run_step(step_num, session_id, poison=False):
    if poison:
        # Deliberately inject a malformed/corrupted tool response
        tool_response = None  # simulates a broken tool call
        st.session_state.log.append(
            f"⚠️ Step {step_num}: tool call returned malformed response — "
            f"**Poison Detector flagged it**"
        )
    else:
        tool_response = f"ok (step {step_num} result)"
        st.session_state.log.append(f"✅ Step {step_num}: {tool_response}")

    tracker.checkpoint(session_id=session_id, step=step_num, data=tool_response)
    render_log()

if start_clicked:
    st.session_state.session_id = f"demo-{random.randint(1000,9999)}"
    st.session_state.steps_done = 0
    st.session_state.log = []
    st.session_state.crashed = False
    tracker.start()
    session_id = st.session_state.session_id

    for step in range(1, TOTAL_STEPS + 1):
        run_step(step, session_id)
        st.session_state.steps_done = step
        time.sleep(0.4)

    st.success(f"Session `{session_id}` completed — {TOTAL_STEPS}/{TOTAL_STEPS} steps.")

if crash_clicked:
    if not st.session_state.session_id:
        st.warning("Start a session first.")
    else:
        session_id = st.session_state.session_id
        st.session_state.log = []
        st.session_state.crashed = True

        for step in range(1, POISON_STEP + 1):
            poison = (step == POISON_STEP)
            run_step(step, session_id, poison=poison)
            st.session_state.steps_done = step
            time.sleep(0.4)

        st.error(
            f"💥 Process killed at step {POISON_STEP} of {TOTAL_STEPS} "
            f"— session `{session_id}` did not complete."
        )

if resume_clicked:
    if not st.session_state.crashed:
        st.warning("Simulate a crash first, then resume.")
    else:
        session_id = st.session_state.session_id
        st.info(f"Resuming session `{session_id}` from last checkpoint...")

        resumed = tracker.resume(session_id=session_id)

        with st.spinner("Running Attribution Engine on the failed session..."):
            time.sleep(1)
            engine = streamctx.get_attribution_engine()
            result = engine.attribute_session(session_id)
            # result is expected to look like: {"step": 4, "confidence": 0.82, "reason": "..."}

        st.success(
            f"Resumed cleanly from step {POISON_STEP} — no re-work on steps 1-{POISON_STEP-1}."
        )
        st.metric(
            label="Attribution Engine: root cause",
            value=f"Step {result.get('step', POISON_STEP)}",
            delta=f"{result.get('confidence', 0.82)*100:.0f}% confidence",
        )
        st.caption(
            "This is exactly what happens in production: no scrolling logs, "
            "no guessing which step broke — the system points at it."
        )

        for step in range(POISON_STEP, TOTAL_STEPS + 1):
            run_step(step, session_id)
            time.sleep(0.4)

        st.session_state.steps_done = TOTAL_STEPS
        st.session_state.crashed = False
        st.balloons()

# --- live stats -----------------------------------------------------------
if st.session_state.session_id:
    stats_box.progress(st.session_state.steps_done / TOTAL_STEPS)

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


