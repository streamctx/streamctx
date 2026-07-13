\# API Reference



\## `streamctx.get\_tracker(agent\_id: str | None = None) -> LLMTracker`



Returns the `LLMTracker` for a given `agent\_id`. Each distinct `agent\_id` gets its own isolated tracker instance, so multiple agents in the same process don't share sessions or checkpoints. Calling with no `agent\_id` returns the default tracker.



\## `streamctx.list\_active\_agents() -> list\[str]`



Returns the `agent\_id`s of all trackers that are currently active (started).



\## `LLMTracker`



| Method | Description |

|---|---|

| `start()` | Begins a tracked session and patches the OpenAI/Anthropic SDKs |

| `stop()` | Ends the session and unpatches the SDKs |

| `wrap(client)` | Wraps an OpenAI or Anthropic client so its calls are tracked |

| `checkpoint()` | Manually saves a checkpoint of the current message state |

| `resume(session\_id)` | Returns the messages from the latest checkpoint for a session |

| `get\_session\_id()` | Returns the current session's ID |

| `get\_stats()` | Returns call count, tokens, cost, reused tokens, and biggest waste category |

| `healing\_stats()` | Returns self-healing engine statistics |



\## `streamctx.scan(messages: list\[dict]) -> dict`



Runs the Context Poison Detector against a list of `{"role": ..., "content": ...}` messages. Returns a dict with `health\_score`, `is\_poisoned`, `warnings`, `details`, and `recommendation`.



\## `SessionStorage`



| Method | Description |

|---|---|

| `start\_session()` | Creates a new session row, returns its ID |

| `end\_session(session\_id)` | Marks a session as ended |

| `record\_call(...)` | Persists a `CallRecord` for a single LLM call |

| `save\_checkpoint(session\_id, step, messages)` | Saves a checkpoint |

| `get\_latest\_checkpoint(session\_id)` | Returns the most recent checkpoint, or `None` |

| `resume\_from\_checkpoint(session\_id)` | Returns the latest checkpoint's messages, or `\[]` |

| `get\_session\_stats(session\_id)` | Returns aggregate call/token/cost stats for a session |

| `get\_calls\_for\_session(session\_id)` | Returns every call row for a session, oldest first |



\## Environment Variables



| Variable | Purpose |

|---|---|

| `STREAMCTX\_BACKEND` | `sqlite` (default) or `supabase` |

| `SUPABASE\_URL` | Required if backend is `supabase` |

| `SUPABASE\_KEY` | Required if backend is `supabase` |

| `STREAMCTX\_HOME` | Override the default `\~/.streamctx` storage directory |



