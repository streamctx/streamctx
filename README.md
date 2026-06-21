# StreamCtx 🧠

**Your AI agent is silently corrupting its own context. StreamCtx detects it — and fixes it.**

[![PyPI](https://img.shields.io/pypi/v/streamctx)](https://pypi.org/project/streamctx/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Free Forever](https://img.shields.io/badge/core-free%20forever-blue)]()

## Install

```bash
pip install streamctx
```

## 2-Line Setup

```python
import streamctx
streamctx.start()  # patches OpenAI + Anthropic automatically
```

---

## The Problem Nobody Talks About

You ship an AI agent. It works perfectly in demos.

Then in production:
- Agent gets stuck repeating the same failed action 58 times
- Context from step 3 contradicts context from step 7
- Agent hallucinates a tool call, writes it to memory, references it forever
- Your $0.50 task costs $50 because nobody set a limit

Every LLM observability tool tracks tokens. Nobody tracks context health.

Until now.

---

## What StreamCtx Does

### 1. Context Poison Detection

```python
result = streamctx.scan(messages)
print(result["health_score"])   # 25/100
print(result["warnings"])
# ⚠️  Repeated errors: 'failed' 4x — agent stuck in loop
# 🔴  Context severely poisoned — resume from checkpoint
```

### 2. Context Diff — See Exactly What Changed

```python
diff = streamctx.context_diff(step3_msgs, step7_msgs, step_a=3, step_b=7)
print(diff["summary"])
# ⚠️  System prompt REMOVED — agent lost instructions
# ⚠️  Contradiction: 'use gpt' added but 'use claude' removed
# Drift Score: 50/100
```

### 3. Auto-Checkpoint + Resume

```python
session_id = streamctx.get_session_id()
messages = streamctx.resume(session_id)
# Pick up exactly where agent left off
```

### 4. 50% Token Compression

```python
result = streamctx.compress(messages, max_tokens=2000)
# 140 tokens → 70 tokens (50% reduction)
```

### 5. Self-Healing

```python
stats = streamctx.healing_stats()
# failures: 1, recoveries: 1
```

### 6. Full Session Report

```python
streamctx.report()
streamctx.stop()
```

---

## Storage Backends

StreamCtx works out of the box with **zero config** (SQLite, local file). For production, point it at **Supabase** for managed, multi-user persistence:

```bash
# .env
STREAMCTX_BACKEND=supabase
SUPABASE_URL=your-project-url
SUPABASE_KEY=your-api-key
```

No code changes needed — same API, different backend.

---

## Feature Comparison


Feature              | StreamCtx | Langfuse | LangSmith | Mem0
---------------------|-----------|----------|-----------|-----
Token tracking       |     YES   |    YES   |    YES    |  NO
Cost estimation      |     YES   |    YES   |    YES    |  NO
Context Poison Det.  |     YES   |    NO    |    NO     |  NO
Context Diff         |     YES   |    NO    |    NO     |  NO
Auto-checkpoint      |     YES   |    NO    |    NO     |  NO
50% Compression      |     YES   |    NO    |    NO     |  NO
Self-healing         |     YES   |    NO    |    NO     |  NO
Zero config          |     YES   |    NO    |    NO     |  NO
Open source          |     YES   |    YES   |    NO     |  NO

| **Core features free forever** | **YES** | Partial | NO | NO (Graph Memory paywalled) |

---

## Quick Start

```python
import streamctx
from openai import OpenAI

streamctx.start()
client = OpenAI()

messages = [{"role": "user", "content": "Hello!"}]
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=messages,
)

result = streamctx.scan(messages)
print(result["health_score"])
print(result["recommendation"])

streamctx.report()
streamctx.stop()
```

---

## API Reference

```python
streamctx.start()                        # start tracking
streamctx.stop()                         # stop tracking
streamctx.report()                       # print full report
streamctx.wrap(client)                   # manually wrap client

streamctx.scan(messages)                 # context health score
streamctx.context_diff(a, b)             # compare two steps

streamctx.checkpoint()                   # save checkpoint
streamctx.resume(session_id)             # resume from checkpoint
streamctx.get_session_id()               # current session ID

streamctx.compress(messages)             # 50% token compression
streamctx.healing_stats()                # self-healing stats
```

---

## Why StreamCtx?

Most tools answer: "How many tokens did I use?"

StreamCtx answers: "Why is my agent broken — and how do I fix it?"

---

## Pricing

The core SDK — all 6 features above, plus the SQLite backend — is **free forever**, MIT-licensed, with no feature gates. No credit card, no signup, no locked features.

A managed offering (hosted Supabase backend + observability dashboard + team access) is planned for teams who want infrastructure handled for them. The features themselves will never move behind a paywall.

---

## Roadmap

**Done:**
- Token tracking + cost estimation
- Context poison detection
- Context diff + drift scoring
- Auto-checkpoint + resume
- 50% token compression
- Self-healing engine
- Supabase storage backend

**Coming:**
- Context budget manager (v0.4.0)
- Visual dashboard
- Multi-agent support

---

## License

MIT — Sneh R Joshi

Built by a solo founder who got tired of AI agents silently going insane.

