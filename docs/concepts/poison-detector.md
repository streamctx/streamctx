\# Context Poison Detector



Long conversations with AI agents can become "poisoned" — filled with repeated errors, contradictory facts, or accumulating failures that push the agent into unproductive loops. The Poison Detector scans conversation history and produces a \*\*health score (0–100)\*\* along with actionable warnings.



\## What Counts as Poisoning



The detector runs four independent checks on every scan:



\### 1. Repeated Errors

Detects when the \*\*same\*\* error pattern (e.g. `"not found"`, `"failed"`, `"exception"`) appears 3 or more times across the conversation — a strong signal the agent is stuck retrying the same failing action.



\### 2. Contradictory Facts

Checks for known contradiction pairs (`"enabled"`/`"disabled"`, `"exists"`/`"not found"`, `"valid"`/`"invalid"`, etc.) both appearing in the conversation — a sign the context contains conflicting information.



\### 3. Error Accumulation

Looks at the most recent window of messages (last 6) and counts how many contain \*\*any\*\* error-like pattern, even if the specific wording differs each time. This catches agents that hit \*different\* errors in a row — e.g., a 404, then a timeout, then an invalid-request error — which the repeated-errors check alone would miss.



\### 4. Repetitive Assistant Responses

Flags when the assistant gives the same response content two or more times — often a sign of a hallucination loop.



\## Health Score \& Recommendations



```python

result = streamctx.scan(messages)

\# {

\#   "health\_score": 45,

\#   "is\_poisoned": True,

\#   "warnings": \[...],

\#   "details": {...},

\#   "recommendation": "🔧 Context needs cleaning — use streamctx.compress()"

\# }

```



| Score | Recommendation |

|---|---|

| 80–100 | ✅ Context is healthy |

| 60–79 | ⚡ Minor issues — monitor closely |

| 40–59 | 🔧 Needs cleaning — use `streamctx.compress()` |

| 0–39 | 🚨 Severely poisoned — reset or resume from checkpoint |



`is\_poisoned` is `True` whenever `health\_score < 60`.



\## Known Edge Cases (by design)



\- \*\*Substring matching\*\*: the contradiction pair `("valid", "invalid")` uses substring containment, so a message containing only the word "invalid" can technically match both sides of the pair. This is a known, low-risk trade-off in the current implementation.

\- \*\*None-safe\*\*: all content fields are normalized with `(m.get("content") or "")` so messages with explicit `null`/`None` content never crash the scanner (fixed after a chaos-testing run surfaced this).



\## Edge Cases Covered by Tests



`tests/test\_poison\_edge\_cases.py` verifies: empty message lists, exact boundary conditions (2 vs. 3 repeated errors), case-insensitivity, missing `role`/`content` keys, adversarial payloads with poison signals buried in long benign text, and compounding penalties when multiple signals fire together.



