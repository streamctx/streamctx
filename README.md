# streamctx

Zero-configuration LLM call tracing for OpenAI and Anthropic SDKs. Track token usage, estimate costs, detect repeated context, and see how much you could save.

## Install

```bash
pip install streamctx
```

Optional SDK extras:

```bash
pip install streamctx[all]
```

## Usage

Only two lines needed:

```python
import streamctx
streamctx.start()
```

After your first LLM call, streamctx automatically prints a terminal summary:

```
___________________________________________________________________________________________________________________
streamctx :12 calls | 48210 tokens | $0.73 estimated
streamctx : 31% cached/reused context | saving : $0.18
streamctx : biggest waste: repeated system prompt
streamctx : you could have saved $0.18 (estimated)
_____________________________________________________________________________________________________________________
```

## API

| Function | Description |
|---|---|
| `streamctx.start()` | Enable tracing; auto-detects OpenAI and Anthropic SDK calls via monkeypatching |
| `streamctx.wrap(client)` | Manually instrument a specific client instance |
| `streamctx.report()` | Print a full Rich terminal summary |
| `streamctx.stop()` | Disable tracking and close the session |

### Manual instrumentation

```python
import streamctx
from openai import OpenAI

streamctx.start()
client = streamctx.wrap(OpenAI())
```

## Features

- **Monkeypatching** — Automatically hooks OpenAI `chat.completions.create` and Anthropic `messages.create`
- **Pricing table** — GPT-4o, GPT-3.5, Claude Sonnet, Claude Haiku
- **Context-diff engine** — Detects repeated system prompts and messages
- **SQLite storage** — Session history persisted to `~/.streamctx/sessions.db`
- **Rich output** — Beautiful terminal summaries with savings estimates
- **Zero config** — Works on first run with no setup

## Supported models

| Model | Input / 1M tokens | Output / 1M tokens |
|---|---|---|
| GPT-4o | $2.50 | $10.00 |
| GPT-3.5 | $0.50 | $1.50 |
| Claude Sonnet | $3.00 | $15.00 |
| Claude Haiku | $0.25 | $1.25 | 

## Why Streamctx Exists

Production LLM costs surprise every team.
in development becomes $47,000/month when agent scale.
StreamCtx give you token-level visibility on the first run- no configuration, no guessing.

## Built with 

- Python - core implementation
- OpenAI SDK + Anthropic SDK - Integration Layer
- SQLite - Session persistence
- Rich - Terminal formatting

 Devloped Using modern AI- assisted development tools.

## License

MIT
