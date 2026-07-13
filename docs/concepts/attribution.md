\# Attribution Engine



The Causal Failure Attribution Engine answers the question every developer asks after a failure: \*\*"why did this call actually fail?"\*\*



\## The Problem



When an LLM call fails, the error message alone rarely tells the full story. Was it a bad prompt from two steps ago? Stale context? A cascading failure from an earlier call? Without attribution, developers manually replay logs to find root causes — slow and error-prone.



\## How It Works



StreamCtx scores four weighted signals for every failed call, using the calls that came before it in the same session:



| Signal | Weight | What it measures |

|---|---|---|

| \*\*Drift\*\* | 0.5 | How much the context has changed since the last known-good call |

| \*\*Compression\*\* | 0.3 | Whether aggressive compression removed information the call needed |

| \*\*Recency\*\* | 0.2 | How close in time/steps the candidate cause is to the failure |



The engine walks through `get\_calls\_for\_session()` chronologically and produces a ranked list of candidate root causes for any failed call, each with a confidence score.



\## Example Output



```python

tracker.healing\_stats()

\# {

\#   "call\_count": 12,

\#   "total\_tokens": 8420,

\#   "total\_cost": 0.14,

\#   "reused\_tokens": 1200,

\#   "biggest\_waste": "repeated system prompt"

\# }

```



For failed calls specifically, the attribution result includes a ranked list of likely causes with their contributing signal breakdown (drift/compression/recency), so you can see at a glance whether the failure was caused by prompt drift, over-aggressive compression, or a nearby recent event.



\## When to Use It



Attribution runs automatically on every failed call — no setup required. Check the `CallRecord.error\_message` and the attribution breakdown in your session report whenever you see a `failed=True` record.



