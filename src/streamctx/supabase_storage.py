"""Supabase storage for streamctx sessions and checkpoints."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import create_client
from dotenv import load_dotenv

# .env ફાઈલ લોડ કર
load_dotenv()


class SupabaseStorage:
    def __init__(self) -> None:
        # Supabase ક્યન્ટ સેટઅપ કરો
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        self.supabase = create_client(url, key)

    def start_session(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        result = (
            self.supabase.table("sessions")
            .insert({"started_at": now})
            .execute()
        )
        return int(result.data[0]["id"])

    def end_session(self, session_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.supabase.table("sessions").update(
            {"ended_at": now}
        ).eq("id", session_id).execute()

    def record_call(
        self,
        session_id: int,
        provider: str,
        model: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cost: float,
        reused_tokens: int,
        waste_category: Optional[str],
        messages: list[dict[str, Any]],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.supabase.table("calls").insert(
            {
                "session_id": session_id,
                "timestamp": now,
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost": cost,
                "reused_tokens": reused_tokens,
                "waste_category": waste_category,
                "messages_json": messages,
            }
        ).execute()

    def save_checkpoint(
        self,
        session_id: int,
        step_number: int,
        messages: list[dict[str, Any]],
    ) -> None:
        """Save current messages as a checkpoint after each LLM call."""
        now = datetime.now(timezone.utc).isoformat()
        self.supabase.table("checkpoints").insert(
            {
                "session_id": session_id,
                "step_number": step_number,
                "messages_json": messages,
                "timestamp": now,
            }
        ).execute()

    def get_latest_checkpoint(self, session_id: int) -> Optional[dict[str, Any]]:
        """Get the most recent checkpoint for a session."""
        result = (
            self.supabase.table("checkpoints")
            .select("step_number, messages_json, timestamp")
            .eq("session_id", session_id)
            .order("step_number", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        row = result.data[0]
        return {
            "step_number": row["step_number"],
            "messages": row["messages_json"],
            "timestamp": row["timestamp"],
        }

    def resume_from_checkpoint(self, session_id: int) -> list[dict[str, Any]]:
        """Return messages from the latest checkpoint to resume from."""
        result = self.get_latest_checkpoint(session_id)
        if result is None:
            return []
        return result["messages"]

    def get_session_stats(self, session_id: int) -> dict[str, Any]:
        calls = (
            self.supabase.table("calls")
            .select("input_tokens, output_tokens, cost, reused_tokens, waste_category")
            .eq("session_id", session_id)
            .execute()
        )
        rows = calls.data or []

        call_count = len(rows)
        input_tokens = sum(r.get("input_tokens", 0) or 0 for r in rows)
        output_tokens = sum(r.get("output_tokens", 0) or 0 for r in rows)
        total_cost = sum(r.get("cost", 0) or 0 for r in rows)
        reused_tokens = sum(r.get("reused_tokens", 0) or 0 for r in rows)

        waste_counts: dict[str, int] = {}
        for r in rows:
            cat = r.get("waste_category")
            if cat:
                waste_counts[cat] = waste_counts.get(cat, 0) + 1
        biggest_waste = max(waste_counts, key=waste_counts.get) if waste_counts else None

        return {
            "call_count": call_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "total_cost": float(total_cost),
            "reused_tokens": reused_tokens,
            "biggest_waste": biggest_waste,
        }


