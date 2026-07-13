\# Context Compression



As conversations grow, so does the token bill. StreamCtx's compressor identifies and removes redundant content — most commonly repeated system prompts — before sending context to the LLM, without changing the meaning of the conversation.



\## How It Works



On every tracked call, `compress\_messages()` runs alongside the Attribution Engine's diff analysis:



```python

from .compressor import compress\_messages



\_, orig\_tokens, comp\_tokens = compress\_messages(messages)

compression\_savings = max(0, orig\_tokens - comp\_tokens)

context\_savings\_tokens = compression\_savings + reused\_tokens

```



The `reused\_tokens` figure (from the `ContextDiffEngine`) captures messages that are identical to ones already seen in the session — system prompts are treated as \*\*100% reusable across calls\*\* since they rarely change mid-session.



\## What Gets Tracked



Every `CallRecord` stores:



\- `reused\_tokens` — tokens saved by not re-sending duplicate content

\- `waste\_category` — a human-readable label like `"repeated system prompt"` or `"repeated user message"`



\## Viewing Compression Savings



```python

stats = tracker.get\_stats()

print(stats\["reused\_tokens"])

print(stats\["biggest\_waste"])

```



The Streamlit dashboard visualizes these savings per session, so you can see exactly how much of your token spend was avoidable.



\## Design Note



Compression in StreamCtx is intentionally conservative — it only removes content that is provably redundant (exact duplicates via content hashing), never content that might change the model's behavior. This is a deliberate trade-off: safety over maximum compression ratio.



