"""LLM call tracker with SDK monkeypatching and context-diff engine."""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
import weakref
from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

from . import pricing
from .storage import get_storage
from .healer import SelfHealingEngine


OPENAI_CREATE_KEY = "openai.resources.chat.completions.Completions.create"
ANTHROPIC_CREATE_KEY = "anthropic.resources.messages.Messages.create"

# Process-wide SDK originals and a single class-level patch.
# Per-tracker start() used to replace Completions.create and save whatever
# was currently installed — often the previous tracker's wrapper. After a
# start/stop cycle that wrapper called itself (RecursionError) or looked
# up a cleared _originals key (KeyError with the key as the message).
_sdk_lock = threading.Lock()
_sdk_originals: dict[str, Any] = {}
_sdk_started: list[Any] = []
_client_owners: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


def _is_patched(fn: Any) -> bool:
    if fn is None:
        return False
    if getattr(fn, "_streamctx_patched", False):
        return True
    inner = getattr(fn, "__func__", None)
    return bool(inner and getattr(inner, "_streamctx_patched", False))


def _register_owner(obj: Any, tracker: Any) -> None:
    try:
        _client_owners[obj] = tracker
    except TypeError:
        pass


def _invoke_original(original: Any, bound_self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Call a captured create() without injecting a second ``self``."""
    if inspect.ismethod(original) and args and args[0] is bound_self:
        args = args[1:]
    return original(*args, **kwargs)


def _resolve_openai_cls() -> Any:
    try:
        import openai.resources.chat.completions as chat_completions
        return chat_completions.Completions
    except ImportError:
        return None


def _resolve_anthropic_cls() -> Any:
    try:
        import anthropic.resources.messages as messages
        return messages.Messages
    except ImportError:
        return None


def _dispatch_tracker(sdk_obj: Any) -> Optional["LLMTracker"]:
    try:
        owner = _client_owners.get(sdk_obj)
    except Exception:
        owner = None
    if owner is not None and owner.state.active:
        return owner
    with _sdk_lock:
        started = list(_sdk_started)
    for tracker in reversed(started):
        if getattr(tracker.state, "active", False):
            return tracker
    default = _trackers.get(DEFAULT_AGENT_ID)
    if default is not None and default.state.active:
        return default
    return None


def _patched_openai_create(self_completions: Any, *args: Any, **kwargs: Any) -> Any:
    original = _sdk_originals.get(OPENAI_CREATE_KEY)
    if original is None:
        cls = _resolve_openai_cls()
        if cls is None:
            raise RuntimeError("OpenAI Completions.create original is missing")
        original = cls.create
    def invoke() -> Any:
        return original(self_completions, *args, **kwargs)
    tracker = _dispatch_tracker(self_completions)
    if tracker is None:
        return invoke()
    return tracker._intercept_call(invoke, "openai", kwargs)


def _patched_anthropic_create(self_messages: Any, *args: Any, **kwargs: Any) -> Any:
    original = _sdk_originals.get(ANTHROPIC_CREATE_KEY)
    if original is None:
        cls = _resolve_anthropic_cls()
        if cls is None:
            raise RuntimeError("Anthropic Messages.create original is missing")
        original = cls.create
    def invoke() -> Any:
        return original(self_messages, *args, **kwargs)
    tracker = _dispatch_tracker(self_messages)
    if tracker is None:
        return invoke()
    return tracker._intercept_call(invoke, "anthropic", kwargs)


_patched_openai_create._streamctx_patched = True
_patched_anthropic_create._streamctx_patched = True


def _install_openai_patch() -> None:
    cls = _resolve_openai_cls()
    if cls is None:
        return
    current = cls.create
    if OPENAI_CREATE_KEY not in _sdk_originals and not _is_patched(current):
        _sdk_originals[OPENAI_CREATE_KEY] = current
    if not _is_patched(current):
        cls.create = _patched_openai_create


def _install_anthropic_patch() -> None:
    cls = _resolve_anthropic_cls()
    if cls is None:
        return
    current = cls.create
    if ANTHROPIC_CREATE_KEY not in _sdk_originals and not _is_patched(current):
        _sdk_originals[ANTHROPIC_CREATE_KEY] = current
    if not _is_patched(current):
        cls.create = _patched_anthropic_create


def _restore_sdk_patches() -> None:
    openai_cls = _resolve_openai_cls()
    openai_orig = _sdk_originals.pop(OPENAI_CREATE_KEY, None)
    if openai_cls is not None and openai_orig is not None and _is_patched(openai_cls.create):
        openai_cls.create = openai_orig
    anthropic_cls = _resolve_anthropic_cls()
    anthropic_orig = _sdk_originals.pop(ANTHROPIC_CREATE_KEY, None)
    if anthropic_cls is not None and anthropic_orig is not None and _is_patched(anthropic_cls.create):
        anthropic_cls.create = anthropic_orig


def _reset_sdk_patches() -> None:
    """Test helper: drop start() refcounts and restore real SDK methods."""
    with _sdk_lock:
        _sdk_started.clear()
        _restore_sdk_patches()
    try:
        _client_owners.clear()
    except Exception:
        pass


def _message_fingerprint(messages: list[dict[str, str]]) -> str:
    payload = json.dumps(messages, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _cached_response_from_row(row: dict[str, Any]) -> Any:
    text = row.get("response_text") or ""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        content=text,
        usage=SimpleNamespace(
            prompt_tokens=int(row.get("input_tokens") or 0),
            completion_tokens=int(row.get("output_tokens") or 0),
        ),
        _streamctx_cached=True,
    )


def _choice_message(response: Any) -> Any:
    """Return the first choice message (object or dict), or None."""
    try:
        choices = response.choices
        if choices:
            return choices[0].message
    except (AttributeError, IndexError, TypeError):
        pass
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if not choices:
            return None
        first = choices[0]
        if isinstance(first, dict):
            return first.get("message")
        return getattr(first, "message", None)
    return None


def _field(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return None
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return None


def _seq_nonempty(value: Any) -> bool:
    if not value:
        return False
    try:
        return len(value) > 0
    except TypeError:
        return True


def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return str(getattr(block, "type", "") or "")


def _visible_text_blocks(content: Any) -> str:
    """User-visible text only. Thinking/tool_use/reasoning blocks are ignored."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    blocks = content if isinstance(content, list) else [content]
    parts: list[str] = []
    for block in blocks:
        btype = _block_type(block)
        if btype in {"thinking", "tool_use", "tool_result", "reasoning", "redacted_thinking"}:
            continue
        if btype in {"text", ""} or btype == "output_text":
            if isinstance(block, dict):
                text = block.get("text")
            else:
                text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
            elif btype == "" and isinstance(block, str):
                parts.append(block)
    return "\n".join(parts)


def _response_text(response: Any, provider: str) -> str:
    """User-visible assistant text. Not reasoning, not tool payloads.

    OpenAI/OpenRouter: ``choices[0].message.content``, then ``refusal``.
    Anthropic: ``type=text`` blocks only. Dict fallbacks: content / text.
    """
    message = _choice_message(response)
    if message is not None:
        content = _field(message, "content")
        text = _visible_text_blocks(content) if not isinstance(content, str) else (content or "")
        if not str(text).strip():
            text = _field(message, "refusal") or ""
        if str(text).strip():
            return str(text)
    if provider == "anthropic":
        try:
            text = _visible_text_blocks(getattr(response, "content", None))
            if str(text).strip():
                return str(text)
        except (AttributeError, TypeError):
            pass
    if isinstance(response, dict):
        text = response.get("content") or response.get("text") or ""
        if isinstance(text, str) and text.strip():
            return text
        nested = _visible_text_blocks(text)
        if str(nested).strip():
            return str(nested)
    return ""


def _has_tool_payload(response: Any, provider: str) -> bool:
    """True when the model returned a tool/function call rather than text."""
    message = _choice_message(response)
    if message is not None:
        if _seq_nonempty(_field(message, "tool_calls")):
            return True
        function_call = _field(message, "function_call")
        if function_call:
            name = _field(function_call, "name")
            if name:
                return True
            if isinstance(function_call, dict) and function_call.get("name"):
                return True
        finish = None
        try:
            finish = response.choices[0].finish_reason
        except (AttributeError, IndexError, TypeError):
            if isinstance(response, dict):
                choices = response.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    finish = choices[0].get("finish_reason")
        if str(finish or "") in {"tool_calls", "function_call", "tool_use"}:
            return True
    content = None
    if provider == "anthropic":
        content = getattr(response, "content", None)
    if content is None and isinstance(response, dict):
        content = response.get("content")
    if isinstance(content, list):
        for block in content:
            if _block_type(block) == "tool_use":
                return True
    return False


def _provider_reported_output_tokens(response: Any) -> int:
    """Tokens the provider billed, not the len//4 estimate fallback."""
    usage = getattr(response, "usage", None)
    if usage is not None:
        raw = getattr(usage, "completion_tokens", None)
        if raw is None:
            raw = getattr(usage, "output_tokens", None)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        u = response["usage"]
        raw = u.get("completion_tokens")
        if raw is None:
            raw = u.get("output_tokens")
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _is_blank_billed_reply(
    response: Any,
    provider: str,
    reply_text: str,
) -> bool:
    """True when the model billed output tokens but said nothing usable.

    Tool/function calls with no text are valid. Refusals are visible text.
    Empty content with *zero reported* output tokens is not this case
    (stubs, no-ops, missing usage).
    """
    if _has_tool_payload(response, provider):
        return False
    if str(reply_text or "").strip():
        return False
    return _provider_reported_output_tokens(response) > 0


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif "text" in block:
                    parts.append(str(block["text"]))
            elif hasattr(block, "text"):
                parts.append(str(block.text))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if hasattr(content, "text"):
        return str(content.text)
    return str(content)


def _normalize_messages(raw: Any) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if raw is None:
        return messages
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if isinstance(item, dict):
            role = str(item.get("role", "user"))
            content = _extract_text(item.get("content", ""))
            messages.append({"role": role, "content": content})
        elif hasattr(item, "role"):
            content = _extract_text(getattr(item, "content", ""))
            messages.append({"role": str(item.role), "content": content})
    return messages


@dataclass
class CallRecord:
    provider: str
    model: Optional[str]
    input_tokens: int
    output_tokens: int
    cost: float
    reused_tokens: int
    waste_category: Optional[str]
    messages: list[dict[str, str]]
    failed: bool = False
    healed: bool = False
    error_message: Optional[str] = None
    message_fingerprint: Optional[str] = None
    response_text: Optional[str] = None


@dataclass
class TrackerState:
    active: bool = False
    session_id: Optional[int] = None
    storage: Any = field(default_factory=get_storage)
    seen_hashes: set[str] = field(default_factory=set)
    waste_counter: Counter = field(default_factory=Counter)
    call_count: int = 0
    step_counter: int = 0
    auto_reported: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _originals: dict[str, Any] = field(default_factory=dict)
    _wrapped_clients: set[int] = field(default_factory=set)
    _last_messages: list[dict[str, str]] = field(default_factory=list)


class ContextDiffEngine:
    def __init__(self)->None:
        self._seen: dict[str,int]={}
        self._system_prompts: Counter = Counter()


    def analyze(self, messages: list[dict[str, str]]) -> tuple[int, Optional[str]]:
        reused = 0
        waste: Optional[str] = None
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if not content:
                continue
            h = _hash_text(f"{role}:{content}")
            tokens = _estimate_tokens(content)

            # NEW: system prompts are always 100% reusable across calls
            if role == "system":
                reused += tokens
                self._system_prompts[content[:120]] += 1
                waste = "repeated system prompt"
                continue   
 
                if h in self._seen:
                    reused += tokens
                    if role == "system":
                        if waste is None:
                            waste = "repeated user message"
            elif role == "assistant" and waste is None:
                waste ="repeated assistant context"

                    
                self._seen[h] = tokens
        if waste is None and self._system_prompts:
            waste = "repeated system prompt"
        return reused, waste

    def biggest_waste(self) -> Optional[str]:
        if self._system_prompts:
            return "repeated system prompt"
        return None


class LLMTracker:
    def __init__(self, agent_id: str = "default"):
        self.agent_id = agent_id
        self.session_id = None
        self.state = TrackerState()
        self.diff = ContextDiffEngine()
        self.healer = SelfHealingEngine()


    def start(self) -> None:
        with self.state._lock:
            if not self.state.active:
                self.state.active = True
                self.state.session_id = self.state.storage.start_session()
        # Always (re)register for the process-wide class patch. wrap() may
        # have already set active=True without installing Completions.create.
        self._patch_sdks()

    def stop(self) -> None:
        with self.state._lock:
            if not self.state.active:
                return
            session_id = self.state.session_id
            self.state.active = False
        self._unpatch_sdks()
        if session_id is not None:
            self.state.storage.end_session(session_id)

    def _ensure_session(self) -> None:
        if not self.state.active:
            self.state.active = True
            self.state.session_id = self.state.storage.start_session()

    def checkpoint(self) -> None:
        self._ensure_session()
        if self.state.session_id is None:
            return
        with self.state._lock:
            messages = list(self.state._last_messages)
            step = self.state.step_counter
        self.state.storage.save_checkpoint(
            self.state.session_id, step, messages
        )

    def resume(self, session_id: int) -> list[dict[str, str]]:
        """Restore tracker state from the latest *valid* checkpoint.

        Returns the conversation snapshot (including the last assistant
        reply when one was stored) so the next ``create()`` is step N+1,
        not a replay of step N. Corrupt checkpoints are skipped.
        """
        ckpt = None
        try:
            ckpt = self.state.storage.get_latest_valid_checkpoint(session_id)
        except Exception:
            ckpt = None
        if ckpt is None:
            try:
                return self.state.storage.resume_from_checkpoint(session_id)
            except Exception:
                return []
        messages = list(ckpt.get("messages") or [])
        with self.state._lock:
            self.state.session_id = session_id
            self.state.step_counter = int(ckpt.get("step_number") or 0)
            self.state._last_messages = list(messages)
            self.state.active = True
        self.session_id = session_id
        if messages:
            self.healer.record_success(messages, None)
        return messages

    def get_session_id(self) -> Optional[int]:
        return self.state.session_id

    def healing_stats(self) -> dict[str, Any]:
        return self.healer.get_stats()

    def wrap(self, client: Any) -> Any:

        self._ensure_session()
        with _wrapped_client_lock:
            if client in _wrapped_clients:
                return client

        if hasattr(client, "chat") and hasattr(client.chat, "completions"):
            completions = client.chat.completions
            _register_owner(client, self)
            _register_owner(completions, self)
            current = completions.create
            if _is_patched(current):
                with _wrapped_client_lock:
                    _wrapped_clients.add(client)
                return client
            original_create = current
            tracker = self

            def patched_create(*args: Any, **kwargs: Any) -> Any:
                return tracker._intercept_call(
                    lambda: _invoke_original(original_create, completions, args, kwargs),
                    provider="openai",
                    kwargs=kwargs,
                )
            patched_create._streamctx_patched = True

            completions.create = patched_create
            with _wrapped_client_lock:
                _wrapped_clients.add(client)
            return client

        if hasattr(client, "messages") and hasattr(client.messages, "create"):
            messages = client.messages
            _register_owner(client, self)
            _register_owner(messages, self)
            current = messages.create
            if _is_patched(current):
                with _wrapped_client_lock:
                    _wrapped_clients.add(client)
                return client
            original_create = current
            tracker = self

            def patched_create(*args: Any, **kwargs: Any) -> Any:
                return tracker._intercept_call(
                    lambda: _invoke_original(original_create, messages, args, kwargs),
                    provider="anthropic",
                    kwargs=kwargs,
                )
            patched_create._streamctx_patched = True

            messages.create = patched_create
            with _wrapped_client_lock:
                _wrapped_clients.add(client)
            return client

        return client



    

    def get_stats(self) -> dict[str, Any]:
        if self.state.session_id is None:
            return {
                "call_count": 0,
                "total_tokens": 0,
                "total_cost": 0.0,
                "reused_tokens": 0,
                "biggest_waste": None,
            }
        return self.state.storage.get_session_stats(self.state.session_id)

    def _patch_sdks(self) -> None:
        with _sdk_lock:
            if self not in _sdk_started:
                _sdk_started.append(self)
            _install_openai_patch()
            _install_anthropic_patch()
            if OPENAI_CREATE_KEY in _sdk_originals:
                self.state._originals[OPENAI_CREATE_KEY] = _sdk_originals[OPENAI_CREATE_KEY]
            if ANTHROPIC_CREATE_KEY in _sdk_originals:
                self.state._originals[ANTHROPIC_CREATE_KEY] = _sdk_originals[ANTHROPIC_CREATE_KEY]

    def _unpatch_sdks(self) -> None:
        with _sdk_lock:
            try:
                _sdk_started.remove(self)
            except ValueError:
                pass
            self.state._originals.clear()
            self.state._wrapped_clients.clear()
            if not _sdk_started:
                _restore_sdk_patches()

    def _resolve_patch_target(self, key: str) -> Any:
        if key.startswith("openai."):
            return _resolve_openai_cls()
        if key.startswith("anthropic."):
            return _resolve_anthropic_cls()
        return None

    def _patch_openai(self) -> None:
        with _sdk_lock:
            _install_openai_patch()

    def _patch_anthropic(self) -> None:
        with _sdk_lock:
            _install_anthropic_patch()

    def _intercept_call(
        self,
        fn: Callable[[], Any],
        provider: str,
        kwargs: dict[str, Any],
    ) -> Any:
        if not self.state.active:
            return fn()

        model = kwargs.get("model")
        messages = _normalize_messages(kwargs.get("messages"))
        if provider == "anthropic" and not messages:
            system = kwargs.get("system")
            if system:
                messages = [{"role": "system", "content": _extract_text(system)}]
            user_msgs = _normalize_messages(kwargs.get("messages"))
            messages.extend(user_msgs)

        fingerprint = _message_fingerprint(messages)
        cached = self._lookup_completed_step(fingerprint, messages)
        if cached is not None:
            return cached

        reused, waste = self.diff.analyze(messages)

        from .compressor import compress_messages

        outbound = kwargs.get("messages")
        compress_source = outbound if isinstance(outbound, list) and outbound else messages
        compressed, orig_tok, comp_tok = compress_messages(compress_source)
        compression_savings = max(0, orig_tok - comp_tok)
        context_savings_tokens = compression_savings + reused
        if (
            compression_savings > 0
            and isinstance(outbound, list)
            and compressed is not outbound
        ):
            kwargs["messages"] = compressed

        call_failed = False
        call_healed = False
        call_error_message: Optional[str] = None
        try:
            response = fn()
        except Exception as e:
            call_error_message = str(e)[:500]
            self.healer.record_failure()
            self.healer.ingest_valid_context(
                self.state.storage, self.state.session_id
            )
            self._persist_failure(
                provider, model, messages, fingerprint, call_error_message, healed=False
            )
            if not self.healer.can_heal():
                raise
            recovery_msgs = self.healer.get_recovery_messages(messages)
            if "messages" in kwargs:
                kwargs["messages"] = recovery_msgs
            try:
                response = fn()
            except Exception:
                raise
            call_healed = True
            messages = _normalize_messages(kwargs.get("messages")) or recovery_msgs
            fingerprint = _message_fingerprint(messages)

        reply = _response_text(response, provider)
        input_tokens, output_tokens = self._extract_usage(
            response, provider, messages, kwargs
        )

        if _is_blank_billed_reply(response, provider, reply):
            # Provider returned 200 and billed output tokens but produced
            # no user-visible text and no tool call. This is a content
            # failure: do not move the resume checkpoint, do not treat
            # it as healer success. error_message stays None so Layer 3
            # shadow-repair (empty-error content_error) still fires.
            self.healer.record_failure()
            self._persist_failure(
                provider,
                model,
                messages,
                fingerprint,
                error_message=None,
                healed=False,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=pricing.estimate_cost(model, input_tokens, output_tokens),
                reused_tokens=context_savings_tokens,
                waste_category=waste,
                response_text=reply,
            )
            return response

        self.healer.record_success(messages, response)
        conversation = list(messages)
        if reply:
            conversation.append({"role": "assistant", "content": reply})

        cost = pricing.estimate_cost(model, input_tokens, output_tokens)

        record = CallRecord(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            reused_tokens=context_savings_tokens,
            waste_category=waste,
            messages=messages,
            failed=call_failed,
            healed=call_healed,
            error_message=call_error_message if call_healed else None,
            message_fingerprint=fingerprint,
            response_text=reply,
        )
        call_id = self._persist_success(record, conversation)
        if call_id and reply and str(reply).strip():
            self._review_success_facts(int(call_id))

        with self.state._lock:
            self.state.call_count += 1
            if waste:
                self.state.waste_counter[waste] += 1
            first_call = self.state.call_count == 1 and not self.state.auto_reported
            if first_call:
                self.state.auto_reported = True

        if first_call:
            from .reporter import print_auto_summary
            print_auto_summary(self)

        return response

    def _lookup_completed_step(
        self,
        fingerprint: str,
        messages: list[dict[str, str]],
    ) -> Any:
        """Skip provider re-entry when the caller re-submits a completed snapshot.

        Resume returns the post-response conversation (including the assistant
        reply). Re-sending that snapshot must not re-invoke the provider or
        any tool side effect attached to create(). A second live call with
        the *same request* (no assistant tail) is a new step and is not skipped.
        """
        if self.state.session_id is None:
            return None
        storage = self.state.storage
        try:
            latest = storage.get_latest_valid_checkpoint(self.state.session_id)
            if not latest or latest.get("messages") != messages:
                return None
            row = storage.get_last_successful_call(self.state.session_id)
            if row is None:
                return None
            return _cached_response_from_row(row)
        except Exception:
            return None

    def _persist_failure(
        self,
        provider: str,
        model: Optional[str],
        messages: list[dict[str, str]],
        fingerprint: str,
        error_message: Optional[str],
        healed: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0.0,
        reused_tokens: int = 0,
        waste_category: Optional[str] = None,
        response_text: Optional[str] = None,
    ) -> None:
        """Record a failed call without moving the resume checkpoint.

        Exception failures keep the historical zero-token shape. Blank
        billed replies pass through the real usage so the row still
        shows that output tokens were charged.
        """
        with self.state._lock:
            self.state.call_count += 1
        self._persist(
            CallRecord(
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
                reused_tokens=reused_tokens,
                waste_category=waste_category,
                messages=messages,
                failed=True,
                healed=healed,
                error_message=error_message,
                message_fingerprint=fingerprint,
                response_text=response_text,
            )
        )

    def _review_success_facts(self, call_id: int) -> None:
        """Layer 2 review signal for session-grounded fact contradictions.

        Never flips ``failed`` and never raises into the caller.
        """
        try:
            from .facts import fact_review_enabled

            if not fact_review_enabled():
                return
            session_id = self.state.session_id
            if session_id is None:
                return
            from .attribution import AttributionEngine

            AttributionEngine(storage=self.state.storage).review_success_reply(
                int(session_id), int(call_id)
            )
        except Exception:
            return

    def _persist_success(
        self,
        record: CallRecord,
        conversation: list[dict[str, str]],
    ) -> Optional[int]:
        with self.state._lock:
            self.state.step_counter += 1
            self.state._last_messages = list(conversation)
            step = self.state.step_counter
            session_id = self.state.session_id
        if session_id is None:
            return None
        persist_step = getattr(self.state.storage, "persist_step", None)
        try:
            if persist_step is not None:
                return persist_step(
                    session_id=session_id,
                    provider=record.provider,
                    model=record.model,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost=record.cost,
                    reused_tokens=record.reused_tokens,
                    waste_category=record.waste_category,
                    request_messages=record.messages,
                    checkpoint_messages=conversation,
                    step_number=step,
                    failed=False,
                    healed=record.healed,
                    error_message=record.error_message,
                    message_fingerprint=record.message_fingerprint,
                    response_text=record.response_text,
                    checkpoint_valid=True,
                )
            call_id = self._persist(record)
            self.state.storage.save_checkpoint(session_id, step, conversation)
            return call_id
        except Exception:
            # Provider already succeeded — do not drop the response on a
            # storage failure. In-memory snapshot still lets the process resume.
            return None

    def _persist(self, record: CallRecord) -> Optional[int]:
        if self.state.session_id is None:
            return None
        try:
            return self.state.storage.record_call(
                session_id=self.state.session_id,
                provider=record.provider,
                model=record.model,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cost=record.cost,
                reused_tokens=record.reused_tokens,
                waste_category=record.waste_category,
                messages=record.messages,
                failed=record.failed,
                healed=record.healed,
                error_message=record.error_message,
                message_fingerprint=record.message_fingerprint,
                response_text=record.response_text,
            )
        except TypeError:
            try:
                return self.state.storage.record_call(
                    session_id=self.state.session_id,
                    provider=record.provider,
                    model=record.model,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost=record.cost,
                    reused_tokens=record.reused_tokens,
                    waste_category=record.waste_category,
                    messages=record.messages,
                    failed=record.failed,
                    healed=record.healed,
                    error_message=record.error_message,
                )
            except Exception:
                return None
        except Exception:
            return None

    def _extract_usage(
        self,
        response: Any,
        provider: str,
        messages: list[dict[str, str]],
        kwargs: dict[str, Any],
    ) -> tuple[int, int]:
        input_tokens = 0
        output_tokens = 0
        usage = getattr(response, "usage", None)
        if usage is not None:
            input_tokens = int(
                getattr(usage, "prompt_tokens", None)
                or getattr(usage, "input_tokens", None)
                or 0
            )
            output_tokens = int(
                getattr(usage, "completion_tokens", None)
                or getattr(usage, "output_tokens", None)
                or 0
            )
        elif isinstance(response, dict) and "usage" in response:
            u = response["usage"]
            input_tokens = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
            output_tokens = int(u.get("completion_tokens") or u.get("output_tokens") or 0)

        if input_tokens == 0:
            input_tokens = sum(_estimate_tokens(m["content"]) for m in messages)
            max_tokens = kwargs.get("max_tokens")
            if max_tokens:
                input_tokens += int(max_tokens) // 10

        if output_tokens == 0:
            if provider == "openai":
                try:
                    output_tokens = _estimate_tokens(response.choices[0].message.content)
                except (AttributeError, IndexError, TypeError):
                    output_tokens = 0
            elif provider == "anthropic":
                try:
                    output_tokens = _estimate_tokens(_extract_text(response.content))
                except (AttributeError, IndexError, TypeError):
                    output_tokens = 0

        return input_tokens, output_tokens


_wrapped_clients: "weakref.WeakSet" = weakref.WeakSet()
_wrapped_client_lock = threading.Lock()

_trackers: dict[str, "LLMTracker"] = {}
_trackers_lock = threading.Lock()

DEFAULT_AGENT_ID = "default"


def get_tracker(agent_id: str | None = None) -> "LLMTracker":
    """
    Return the LLMTracker for a given agent_id.

    Each distinct agent_id gets its own isolated LLMTracker instance,
    so multiple agents running in the same process don't mix contexts,
    sessions, or checkpoints.

    Calling get_tracker() with no agent_id returns the default tracker
    (fully backward-compatible with single-agent usage).
    """
    key = agent_id or DEFAULT_AGENT_ID
    with _trackers_lock:
        if key not in _trackers:
            _trackers[key] = LLMTracker(agent_id=key)
        return _trackers[key]


def list_active_agents() -> list[str]:
    """Return agent_ids of all trackers currently active (started)."""
    with _trackers_lock:
        return [aid for aid, t in _trackers.items() if t.state.active]





  





