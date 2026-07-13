\# Checkpoint \& Resume



StreamCtx automatically saves a checkpoint after \*\*every\*\* LLM call in a tracked session. If your process crashes, loses network connectivity, or is killed mid-run, you can resume exactly where you left off — no manual state management required.



\## How Checkpoints Work



Every call to `tracker.checkpoint()` (called automatically after each wrapped LLM call) does the following:



1\. Takes the current message history (`self.state.\_last\_messages`)

2\. Increments the step counter

3\. Writes both to the `checkpoints` table in SQLite (or Supabase), tagged with the session ID and step number



```python

def checkpoint(self) -> None:

&#x20;   self.\_ensure\_session()

&#x20;   with self.state.\_lock:

&#x20;       messages = list(self.state.\_last\_messages)

&#x20;       step = self.state.step\_counter

&#x20;   self.state.storage.save\_checkpoint(

&#x20;       self.state.session\_id, step, messages

&#x20;   )

```



\## Resuming a Session



```python

tracker = streamctx.get\_tracker()

messages = tracker.resume(session\_id)

```



Internally, `resume()` calls `resume\_from\_checkpoint()`, which fetches the checkpoint with the \*\*highest step number\*\* for that session — always the most recent one, even if earlier checkpoints exist.



\## What Happens on Failure Mid-Call



If an LLM call fails partway through:



1\. The failure is caught and recorded via `self.healer.record\_failure()`

2\. If the healer determines it can attempt recovery, recovery messages are generated and stored

3\. A `CallRecord` documenting the failure (with `failed=True`) is persisted \*\*before\*\* the exception is re-raised

4\. The \*\*last successful checkpoint remains untouched\*\* — nothing is lost



This means a network cut mid-call never corrupts your resumable state; you always fall back to the last complete checkpoint.



\## Verified Edge Cases



The following scenarios are covered by `tests/test\_checkpoint\_network\_cut.py`:



\- Checkpoint survives a simulated network cut on the \*next\* call

\- Resume works correctly after a full process restart (new `SessionStorage` instance, same DB file)

\- Multiple checkpoints in a session always resume to the \*latest\*, never a stale one

\- Resuming a session with zero checkpoints returns an empty list, not an error

\- Resuming a non-existent `session\_id` does not crash

\- Checkpoints persist after `end\_session()` is called

\- Unicode, special characters, and embedded quotes survive the JSON round-trip

\- Concurrent checkpoint writes from multiple threads do not crash or corrupt data

\- Checkpoints from different sessions are fully isolated from each other



\## Multi-Agent Isolation



Each `agent\_id` passed to `get\_tracker()` gets its own `TrackerState`, including its own `session\_id` and checkpoint history. Two agents running in the same process never share or overwrite each other's checkpoints.



