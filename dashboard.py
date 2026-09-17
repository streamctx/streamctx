"""StreamCtx Live Dashboard - reads real session/call data from SQLite."""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="StreamCtx Dashboard", layout="wide", page_icon="🧠")


def _default_db_path() -> Path:
    base = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    return base / "sessions.db"


def get_connection():
    db_path = _default_db_path()
    if not db_path.exists():
        return None
    return sqlite3.connect(str(db_path))


def get_sessions(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT id, started_at, ended_at FROM sessions ORDER BY id DESC LIMIT 50",
        conn,
    )


def get_calls(conn, session_id: int) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT id, timestamp, provider, model, input_tokens, output_tokens,
               cost, reused_tokens, waste_category, failed, healed
        FROM calls
        WHERE session_id = ?
        ORDER BY id ASC
        """,
        conn,
        params=(session_id,),
    )


def get_checkpoints(conn, session_id: int) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT step_number, timestamp
        FROM checkpoints
        WHERE session_id = ?
        ORDER BY step_number ASC
        """,
        conn,
        params=(session_id,),
    )


def _evidence_db_path() -> Path:
    base = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    return base / "evidence_ledger.db"


def get_session_evidence_summary(session_id: int) -> dict:
    """Count this session's ledger entries and the latest tip hash.

    Read-only query against evidence_ledger.db — does not export or verify.
    """
    empty = {"count": 0, "tip_hash": None}
    db_path = _evidence_db_path()
    if not db_path.exists():
        return empty
    try:
        ev = sqlite3.connect(str(db_path))
        ev.row_factory = sqlite3.Row
        try:
            count_row = ev.execute(
                "SELECT COUNT(*) AS n FROM evidence_payloads WHERE session_id = ?",
                (int(session_id),),
            ).fetchone()
            count = int(count_row["n"]) if count_row is not None else 0
            tip_hash = None
            if count:
                tip_row = ev.execute(
                    """
                    SELECT e.entry_hash
                    FROM evidence_ledger e
                    JOIN evidence_payloads p ON p.entry_id = e.entry_id
                    WHERE p.session_id = ?
                    ORDER BY e.entry_id DESC
                    LIMIT 1
                    """,
                    (int(session_id),),
                ).fetchone()
                if tip_row is not None:
                    tip_hash = str(tip_row["entry_hash"])
            return {"count": count, "tip_hash": tip_hash}
        finally:
            ev.close()
    except sqlite3.Error:
        return empty


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

conn = get_connection()

if conn is None:
    st.warning(
        "⚠️ No database found yet. Run `streamctx.start()` in your app first — "
        f"expected at `{_default_db_path()}`."
    )
    st.stop()

sessions_df = get_sessions(conn)

if sessions_df.empty:
    st.info("No sessions recorded yet. Start tracking with `streamctx.start()`.")
    st.stop()

# ---- Sidebar ----
st.sidebar.markdown("## 🧠 StreamCtx")
st.sidebar.caption("Context Nervous System for AI Agents")
st.sidebar.divider()
st.sidebar.markdown("### 📂 Sessions")
session_options = sessions_df["id"].tolist()
selected_session = st.sidebar.selectbox(
    "Select a session",
    session_options,
    format_func=lambda sid: f"Session #{sid}",
)
auto_refresh = st.sidebar.checkbox("Auto-refresh (5s)", value=True)

calls_df = get_calls(conn, selected_session)
checkpoints_df = get_checkpoints(conn, selected_session)

# ---- Hero header ----
st.title("🧠 StreamCtx Live Dashboard")
st.caption("Real-time view of what your AI agent is actually doing — and what it's costing you.")

if not calls_df.empty:
    total_calls = len(calls_df)
    total_tokens = int(calls_df["input_tokens"].sum() + calls_df["output_tokens"].sum())
    total_cost = float(calls_df["cost"].sum())
    total_reused = int(calls_df["reused_tokens"].sum())
    failed_count = int(calls_df["failed"].sum()) if "failed" in calls_df else 0
    healed_count = int(calls_df["healed"].sum()) if "healed" in calls_df else 0
    reuse_pct = round(100 * total_reused / total_tokens, 1) if total_tokens else 0.0
else:
    total_calls = total_tokens = total_reused = failed_count = healed_count = 0
    total_cost = 0.0
    reuse_pct = 0.0

st.divider()

# ---- Big impact row ----
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Calls", total_calls)
c2.metric("Total Tokens", f"{total_tokens:,}")
c3.metric("Estimated Cost", f"${total_cost:.4f}")
c4.metric("Context Reused", f"{reuse_pct}%")
c5.metric(
    "Self-Healed Failures",
    healed_count,
    delta=f"{failed_count} total failures" if failed_count else None,
    delta_color="off",
)

st.divider()

# ---- Two-column layout: usage trend + waste ----
left, right = st.columns([2, 1])

with left:
    st.subheader("📈 Token Usage Per Call")
    if not calls_df.empty:
        chart_df = calls_df.copy()
        chart_df["call_number"] = range(1, len(chart_df) + 1)
        chart_df["total_tokens"] = chart_df["input_tokens"] + chart_df["output_tokens"]
        st.line_chart(chart_df.set_index("call_number")["total_tokens"])
    else:
        st.info("No calls recorded for this session yet.")

with right:
    st.subheader("⚠️ Context Waste")
    if not calls_df.empty and calls_df["waste_category"].notna().any():
        waste_counts = calls_df["waste_category"].value_counts()
        st.bar_chart(waste_counts)
    else:
        st.success("No waste detected — context is clean.")

st.divider()

# ---- Checkpoints + failures side by side ----
cp_col, fail_col = st.columns(2)

with cp_col:
    st.subheader("💾 Checkpoints")
    if not checkpoints_df.empty:
        st.dataframe(checkpoints_df, use_container_width=True, hide_index=True)
    else:
        st.info("No checkpoints saved yet for this session.")

with fail_col:
    st.subheader("🩹 Failures & Self-Healing")
    if failed_count > 0:
        failed_rows = calls_df[calls_df["failed"] == 1][
            ["timestamp", "model", "healed"]
        ]
        st.dataframe(failed_rows, use_container_width=True, hide_index=True)
    else:
        st.success("No failures in this session — all calls succeeded.")

st.divider()

# ---- Full call log (collapsed by default) ----
with st.expander("📋 Full Call Log", expanded=False):
    if not calls_df.empty:
        st.dataframe(
            calls_df[
                ["timestamp", "provider", "model", "input_tokens",
                 "output_tokens", "cost", "waste_category"]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No calls recorded for this session yet.")

st.divider()

# ---- Compliance bundle (this session only) ----
st.subheader("🔒 Compliance Evidence")
st.caption(f"Session #{selected_session} — tamper-evident attestation of attribution and repair records.")

evidence_summary = get_session_evidence_summary(int(selected_session))
evidence_count = int(evidence_summary["count"])
tip_hash = evidence_summary["tip_hash"]
tip_short = tip_hash[:8] if tip_hash else "—"

info_col, action_col = st.columns([2, 1])
with info_col:
    st.markdown(f"**Evidence entries:** {evidence_count}")
    st.markdown(f"**Chain tip:** `{tip_short}`")
with action_col:
    export_clicked = st.button("Export Compliance Bundle", use_container_width=True)

if evidence_count == 0:
    st.info("No compliance evidence recorded for this session yet.")
elif export_clicked:
    with st.spinner("Building compliance bundle..."):
        try:
            from streamctx.evidence import export_attestation

            bundle = export_attestation(int(selected_session))
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            st.session_state["compliance_bundle"] = {
                "session_id": int(selected_session),
                "filename": f"compliance_bundle_{selected_session}_{stamp}.json",
                "json": json.dumps(bundle, indent=2),
            }
        except Exception as exc:
            st.session_state.pop("compliance_bundle", None)
            st.error(f"Could not export the compliance bundle: {exc}")

cached_bundle = st.session_state.get("compliance_bundle")
if (
    cached_bundle
    and cached_bundle.get("session_id") == int(selected_session)
    and evidence_count > 0
):
    st.download_button(
        label="Download JSON bundle",
        data=cached_bundle["json"],
        file_name=cached_bundle["filename"],
        mime="application/json",
        use_container_width=True,
    )

conn.close()

if auto_refresh:
    time.sleep(5)
    st.rerun()