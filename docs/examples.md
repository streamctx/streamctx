\# Examples



\## Basic Tracked Call



```python

import streamctx

from openai import OpenAI



tracker = streamctx.get\_tracker()

tracker.start()



client = tracker.wrap(OpenAI())

response = client.chat.completions.create(

&#x20;   model="gpt-4",

&#x20;   messages=\[{"role": "user", "content": "Summarize this document."}],

)



tracker.stop()

```



\## Checking Context Health Before a Call



```python

import streamctx



result = streamctx.scan(conversation\_history)



if result\["is\_poisoned"]:

&#x20;   print(result\["recommendation"])

&#x20;   # e.g. "🔧 Context needs cleaning — use streamctx.compress()"

```



\## Resuming After a Crash



```python

import streamctx



tracker = streamctx.get\_tracker()

tracker.start()



\# Your app crashed here last time — resume instead of starting over

last\_session\_id = 42

messages = tracker.resume(last\_session\_id)



\# Continue the conversation from where it left off

client = tracker.wrap(OpenAI())

response = client.chat.completions.create(

&#x20;   model="gpt-4",

&#x20;   messages=messages + \[{"role": "user", "content": "Continue please."}],

)

```



\## Running Multiple Agents Safely



```python

import streamctx



research\_tracker = streamctx.get\_tracker(agent\_id="research-agent")

writer\_tracker = streamctx.get\_tracker(agent\_id="writer-agent")



research\_tracker.start()

writer\_tracker.start()



\# Each has its own session, checkpoints, and stats — fully isolated

```



\## Inspecting Session Stats After a Run



```python

stats = tracker.get\_stats()

print(f"Calls: {stats\['call\_count']}")

print(f"Total cost: ${stats\['total\_cost']:.4f}")

print(f"Tokens saved by reuse: {stats\['reused\_tokens']}")

print(f"Biggest waste: {stats\['biggest\_waste']}")

```



