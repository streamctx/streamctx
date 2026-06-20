import streamlit as st
import pandas as pd
import sqlite3
import time

st.set_page_config(page_title="StreamCtx Live Core", layout="wide", page_icon="📡")
st.title("📡 StreamCtx Live Production Dashboard")
st.markdown("🔄 **Real-Time Database Monitoring Engine (Auto-Refreshing)**")
st.divider()

# ડેટાબેઝમાંથી લાઈવ ડેટા વાંચવાનું ફંક્શન
def get_live_data():
    try:
        conn = sqlite3.connect("streamctx_live.db")
        df = pd.read_sql_query("SELECT * FROM live_stats", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

# પ્લેસહોલ્ડર્સ સેટ કરવા
metrics_placeholder = st.empty()
chart_placeholder = st.empty()

st.sidebar.markdown("### 🟢 System Status")
st.sidebar.success("Connected to streamctx_live.db")
st.sidebar.info("બેકએન્ડ રન કરશો એટલે આ ગ્રાફ ઓટોમેટીક દર સેકન્ડે અપડેટ થશે!")

# લાઈવ લૂપ (દર ૧ સેકન્ડે ડેટાબેઝ ચેક કરશે)
while True:
    df = get_live_data()

    if not df.empty:
        # મેથેમેટિકલ કેલ્ક્યુલેશન
        total_normal = df["normal_tokens"].sum()
        total_ctx = df["streamctx_tokens"].sum()
        saved_tokens = total_normal - total_ctx
        pct_saved = (saved_tokens / total_normal) * 100 if total_normal > 0 else 0

        latest_poison = df["poison_blocked"].iloc[-1]
        latest_heal = df["self_healed"].iloc[-1]

        # લાઈવ મેટ્રિક્સ ડિસ્પ્લે
        with metrics_placeholder.container():
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Tokens Saved", f"{saved_tokens} Tokens")
            col2.metric("Cost Reduction", f"{pct_saved:.1f}%")
            col3.metric("Poison Blocked", "1 Active" if latest_poison > 0 else "0")
            col4.metric("Self-Healing Triggers", "1 Repaired" if latest_heal > 0 else "0")

        # ગ્રાફ પ્રિન્ટ કરવો
        chart_df = pd.DataFrame({
            "Steps": df["step_name"],
            "Without StreamCtx": df["normal_tokens"].cumsum(),
            "With StreamCtx": df["streamctx_tokens"].cumsum()
        }).set_index("Steps")

        chart_placeholder.line_chart(chart_df)

    else:
        metrics_placeholder.warning("⏳ Waiting for Backend Agent to start writing data...")

    time.sleep(1) # દર ૧ સેકન્ડે ઓટો-રીફ્રેશ


