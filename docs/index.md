# StreamCtx

**Context Nervous System for AI Agents**

StreamCtx is an open-source Python SDK that gives your AI agents real-time context observability, automatic checkpoint/resume, context compression, and self-healing — all in one lightweight package.

## Why StreamCtx?

Long-running AI agents accumulate context over time — repeated errors, contradictory facts, stale data. Left unchecked, this "context poisoning" causes agents to loop, hallucinate, or fail silently. StreamCtx watches every LLM call your agent makes and gives you:

* **Real-time step streaming** — see every call as it happens
* **Auto-checkpoint resume** — never lose progress on a crash or network cut
* **Context compression** — trim redundant tokens before they cost you money
* **Context Poison Detection** — catch loops, contradictions, and error accumulation before they derail your agent
* **Self-healing** — automatic recovery attempts on failed calls
* **Causal Failure Attribution** — when something breaks, know *why*

## Quick Example

```python
import streamctx

tracker = streamctx.get_tracker()
tracker.start()

client = tracker.wrap(openai_client)  # works with OpenAI or Anthropic SDKs

response = client.chat.completions.create(...)

tracker.stop()
```

