"""Counterfactual Time-Travel Debugger for StreamCtx.

Answers the question: "What would have happened if step N had different context?"

This module lets you rewind any session to a specific checkpoint step,
inject alternate context, and replay forward — either as a dry run
(context reconstruction only, no LLM calls) or as a live replay
(real LLM calls to generate alternate responses).

Core API::

    from streamctx.replay import CounterfactualReplayer

    replayer = CounterfactualReplayer()

    # Dry run — see what context would look like
    result = replayer.replay(
        session_id=8,
        from_step=7,
        with_context={"role": "user", "content": "different input"},
        dry_run=True,
    )
    print(result.counterfactual_messages)

    # Live replay — real LLM calls
    result = replayer.replay(
        session_id=8,
        from_step=7,
        with_context={"role": "user", "content": "different input"},
        dry_run=False,
        llm_fn=lambda messages: client.chat.completions.create(
            model="openrouter/free",
            messages=messages,
        ),
    )
    print(result.counterfactual_responses)

Or via the streamctx module-level API::

    import streamctx
    result = streamctx.replay(session_id=8, from_step=7, with_context={...})
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .storage import get_storage


@dataclass
class ReplayResult:
    """Result of a counterfactual replay run.

    Attributes
    ----------
    session_id:
        The session that was replayed.
    from_step:
        The step number at which the alternate context was injected.
    original_messages:
        The original messages at ``from_step`` (before injection).
    counterfactual_messages:
        The messages after injecting the alternate context — this is
        what the agent *would have seen* if the context had been different.
    counterfactual_responses:
        If ``dry_run=False``, the LLM responses generated during the
        live replay.  Empty list for dry runs.
    dry_run:
        Whether this was a dry run (no LLM calls) or a live replay.
    steps_replayed:
        Number of steps that were replayed from ``from_step`` onward.
    injection_summary:
        Human-readable description of what was changed.
    """

    session_id: int
    from_step: int
    original_messages: list[dict[str, Any]]
    counterfactual_messages: list[dict[str, Any]]
    counterfactual_responses: list[Any] = field(default_factory=list)
    dry_run: bool = True
    steps_replayed: int = 0
    injection_summary: str = ""


class CounterfactualReplayer:
    """Counterfactual Time-Travel Debugger.

    Rewinds a session to any checkpoint step, injects alternate context,
    and replays forward.  Supports both dry runs (no LLM cost) and live
    replays (real LLM calls via a user-supplied ``llm_fn``).
    """

    def __init__(self, storage: Any = None) -> None:
        self.storage = storage or get_storage()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def replay(
        self,
        session_id: int,
        from_step: int,
        with_context: dict[str, Any] | list[dict[str, Any]] | None = None,
        dry_run: bool = True,
        llm_fn: Optional[Callable[[list[dict[str, Any]]], Any]] = None,
        replace_step: bool = False,
    ) -> ReplayResult:
        """Replay a session from a given step with alternate context.

        Parameters
        ----------
        session_id:
            ID of the session to replay.
        from_step:
            Step number to rewind to (inclusive — this step's checkpoint
            is used as the base).
        with_context:
            Alternate context to inject.  Can be:
            - A single message dict: ``{"role": "user", "content": "..."}``
            - A list of message dicts (replaces / appends multiple messages)
            - ``None`` (just rewinds to the checkpoint, no injection)
        dry_run:
            If ``True`` (default), only reconstructs the counterfactual
            message list — no LLM calls, no API cost.
            If ``False``, calls ``llm_fn`` for each step from ``from_step``
            onward to generate real alternate responses.
        llm_fn:
            Required when ``dry_run=False``.  A callable that takes a
            ``list[dict]`` of messages and returns an LLM response object.
            Example::

                llm_fn=lambda msgs: client.chat.completions.create(
                    model="openrouter/free",
                    messages=msgs,
                )
        replace_step:
            If ``True``, the message at ``from_step`` in the checkpoint is
            *replaced* by ``with_context``.
            If ``False`` (default), ``with_context`` is *appended* after
            the checkpoint messages.

        Returns
        -------
        ReplayResult
        """
        if not dry_run and llm_fn is None:
            raise ValueError(
                "llm_fn is required for live replay (dry_run=False). "
                "Pass a callable: llm_fn=lambda msgs: client.chat.completions.create(...)"
            )

        # 1. Load the checkpoint at from_step
        
        _rows = self._get_all_checkpoint_rows(session_id)
        original_messages = self._load_checkpoint_at_step(session_id, from_step, rows=_rows)

        if not original_messages:
            return ReplayResult(
                session_id=session_id,
                from_step=from_step,
                original_messages=[],
                counterfactual_messages=[],
                dry_run=dry_run,
                injection_summary=f"No checkpoint found for session {session_id} at step {from_step}.",
            )

        # 2. Build counterfactual messages
        counterfactual_messages, injection_summary = self._inject_context(
            original_messages=original_messages,
            with_context=with_context,
            replace_step=replace_step,
        )

        # 3. Dry run — return immediately, no LLM calls
        if dry_run:
            return ReplayResult(
                session_id=session_id,
                from_step=from_step,
                original_messages=original_messages,
                counterfactual_messages=counterfactual_messages,
                dry_run=True,
                steps_replayed=0,
                injection_summary=injection_summary,
            )

        # 4. Live replay — call llm_fn for each subsequent step
        responses = []
        conversation = list(counterfactual_messages)

        # Load all checkpoints for this session to know how many steps to replay
        all_steps = self._get_all_steps(session_id, rows=_rows)
        steps_after = [s for s in all_steps if s > from_step]
        synthetic_step = not steps_after
        
        if not steps_after:
            # No further steps recorded — replay just the injected step
            steps_after = [from_step]

        for step in steps_after:
            try:
                response = llm_fn(conversation)
                responses.append(response)

                # Extract response text and append to conversation
                reply_text = self._extract_response_text(response)
                if reply_text:
                    conversation.append({"role": "assistant", "content": reply_text})

            except Exception as e:
                responses.append({"error": str(e), "step": step})
                break

        return ReplayResult(
            session_id=session_id,
            from_step=from_step,
            original_messages=original_messages,
            counterfactual_messages=conversation if synthetic_step else counterfactual_messages, # conversation ma assistant replies append thai chuki che
            counterfactual_responses=responses,
            dry_run=False,
            steps_replayed=len(responses),
            injection_summary=injection_summary,
        )

    def list_checkpoints(self, session_id: int) -> list[dict[str, Any]]:
        """List all available checkpoints for a session.

        Returns a list of dicts with ``step_number``, ``timestamp``,
        and ``message_count`` — useful for knowing which steps you can
        rewind to.
        """
        rows = self._get_all_checkpoint_rows(session_id)
        return [
            {
                "step_number": r["step_number"],
                "timestamp": r["timestamp"],
                "message_count": len(json.loads(r["messages_json"] or "[]")),
            }
            for r in rows
        ]

    def diff_replay(
        self,
        session_id: int,
        from_step: int,
        with_context: dict[str, Any] | list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compare original vs counterfactual messages at a given step.

        Returns a dict with ``added``, ``removed``, and ``unchanged``
        message counts, plus the full before/after message lists.
        Useful for quickly seeing what the injection changed without
        doing a full replay.
        """
        original = self._load_checkpoint_at_step(session_id, from_step)
        counterfactual, summary = self._inject_context(original, with_context)

        orig_set = {json.dumps(m, sort_keys=True) for m in original}
        cf_set = {json.dumps(m, sort_keys=True) for m in counterfactual}

        added = [m for m in counterfactual if json.dumps(m, sort_keys=True) not in orig_set]
        removed = [m for m in original if json.dumps(m, sort_keys=True) not in cf_set]
        unchanged_count = len(orig_set & cf_set)

        return {
            "session_id": session_id,
            "from_step": from_step,
            "injection_summary": summary,
            "added_messages": added,
            "removed_messages": removed,
            "unchanged_count": unchanged_count,
            "original_messages": original,
            "counterfactual_messages": counterfactual,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_checkpoint_at_step(
        self, session_id: int, step_number: int, rows: Optional[list[dict[str, Any]]] = None
    ) -> list[dict[str, Any]]:
        """Load messages from the checkpoint at a specific step number."""
        if rows is None:
            rows = self._get_all_checkpoint_rows(session_id)
        for row in rows:
            if row["step_number"] == step_number:
                try:
                    return json.loads(row["messages_json"] or "[]")
                except (TypeError, ValueError):
                    return []

        # If exact step not found, return closest step <= requested
        candidates = [r for r in rows if r["step_number"] <= step_number]
        if candidates:
            closest = max(candidates, key=lambda r: r["step_number"])
            try:
                return json.loads(closest["messages_json"] or "[]")
            except (TypeError, ValueError):
                return []
        return []

    def _get_all_checkpoint_rows(self, session_id: int) -> list[dict[str, Any]]:
        """Fetch all checkpoint rows for a session from SQLite."""
        try:
            with self.storage._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT step_number, messages_json, timestamp
                    FROM checkpoints
                    WHERE session_id = ?
                    ORDER BY step_number ASC
                    """,
                    (session_id,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _get_all_steps(
        self, session_id: int, rows: Optional[list[dict[str, Any]]] = None
    ) -> list[int]:
        """Return sorted list of step numbers available for a session."""
        if rows is None:
            rows = self._get_all_checkpoint_rows(session_id)
        return sorted(r["step_number"] for r in rows)

   
    def _inject_context(
        self,
        original_messages: list[dict[str, Any]],
        with_context: dict[str, Any] | list[dict[str, Any]] | None,
        replace_step: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        """Inject alternate context into the original messages.

        Returns (counterfactual_messages, injection_summary).
        """
        if with_context is None:
            return list(original_messages), "No injection — checkpoint messages returned as-is."

        # Normalize to list
        if isinstance(with_context, dict):
            injection = [with_context]
        else:
            injection = list(with_context)

        if replace_step:
            # Replace last user message with injection
            messages = list(original_messages)
            for i in reversed(range(len(messages))):
                if messages[i].get("role") == "user":
                    messages[i : i + 1] = injection
                    summary = (
                        f"Replaced last user message at step with "
                        f"{len(injection)} injected message(s)."
                    )
                    return messages, summary
            # No user message found — append
            messages.extend(injection)
            summary = f"No user message to replace — appended {len(injection)} message(s)."
            return messages, summary
        else:
            # Append injection after existing messages
            messages = list(original_messages) + injection
            roles = [m.get("role", "?") for m in injection]
            summary = (
                f"Appended {len(injection)} message(s) after checkpoint "
                f"(roles: {', '.join(roles)})."
            )
            return messages, summary

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """Extract text content from an LLM response object."""
        # OpenAI/OpenRouter style
        try:
            return response.choices[0].message.content or ""
        except (AttributeError, IndexError):
            pass
        # Dict style
        if isinstance(response, dict):
            return response.get("content", "") or response.get("text", "")
        return str(response)


def get_replayer() -> CounterfactualReplayer:
    """Convenience factory, mirrors get_storage() in storage.py."""
    return CounterfactualReplayer()

