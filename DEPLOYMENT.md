\# Production Deployment Guide



This guide covers running StreamCtx reliably in production — beyond the

local quickstart in the main README. It assumes you've already validated

StreamCtx locally with the default SQLite backend.



> This is a living document. If something here doesn't match your setup or

> breaks in your environment, please open a GitHub issue — that's exactly

> the kind of gap this guide exists to close.



\## 1. Choosing a backend: SQLite vs Supabase



| | SQLite (default) | Supabase |

|---|---|---|

| Best for | Single-instance, local/dev, small-team use | Multi-instance deployments, horizontal scaling, team dashboards |

| Setup | Zero config, works out of the box | Requires a Supabase project + connection string |

| Concurrency | Hardened for concurrent workers on \*\*one\*\* machine (WAL mode, connection pooling) | Handles concurrency across \*\*multiple\*\* machines/processes |

| When to switch | You're running a single agent process, even with many concurrent workers | You're running StreamCtx across multiple servers/containers that all need to see the same session state |



\*\*Rule of thumb:\*\* if all your agent processes run on one machine, SQLite is

fine and already battle-tested (50 concurrent workers, zero errors, sub-5s).

Switch to Supabase only when you have more than one machine/instance writing

to the same session store.



\### Switching to Supabase

```python

import streamctx



streamctx.start(

&#x20;   backend="supabase",

&#x20;   supabase\_url=os.environ\["STREAMCTX\_SUPABASE\_URL"],

&#x20;   supabase\_key=os.environ\["STREAMCTX\_SUPABASE\_KEY"],

)

```

Never hardcode the URL/key — always read from environment variables (see

Section 3).



\## 2. Running StreamCtx as a persistent process



Pick one based on your existing infra. All three achieve the same goal:

restart the agent process automatically after a crash, and preserve the

`\~/.streamctx/sessions.db` (or Supabase equivalent) across restarts.



\### Option A — systemd (Linux servers)

```ini

\# /etc/systemd/system/streamctx-agent.service

\[Unit]

Description=StreamCtx Agent

After=network.target



\[Service]

Type=simple

User=youruser

WorkingDirectory=/opt/your-agent

EnvironmentFile=/opt/your-agent/.env

ExecStart=/usr/bin/python3 /opt/your-agent/agent.py

Restart=on-failure

RestartSec=5



\[Install]

WantedBy=multi-user.target

```

```bash

sudo systemctl daemon-reload

sudo systemctl enable --now streamctx-agent

```



\### Option B — pm2 (Node-adjacent teams, quick setup)

```bash

pm2 start agent.py --interpreter python3 --name streamctx-agent

pm2 save

pm2 startup   # configures pm2 to restart on server reboot

```



\### Option C — Docker

```dockerfile

FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install -r requirements.txt

CMD \["python", "agent.py"]

```

Run with a restart policy so crashes auto-recover:

```bash

docker run -d --restart unless-stopped \\

&#x20; --env-file .env \\

&#x20; -v streamctx\_data:/root/.streamctx \\

&#x20; your-agent-image

```

The volume mount is important if you're using the SQLite backend — without

it, the session DB is lost when the container is recreated.



\## 3. Environment variables



| Variable | Required for | Notes |

|---|---|---|

| `STREAMCTX\_SUPABASE\_URL` | Supabase backend only | Your Supabase project URL |

| `STREAMCTX\_SUPABASE\_KEY` | Supabase backend only | Use a service-role key, not the public anon key, for server-side agents |

| `OPENAI\_API\_KEY` / `ANTHROPIC\_API\_KEY` | Whichever LLM you're wrapping | Passed through to the client you wrap with `tracker.wrap(client)` |



Store these in a `.env` file (never commit it — add to `.gitignore`) and load

via `EnvironmentFile=` (systemd), `--env-file` (Docker), or `python-dotenv`

locally.



\## 4. What happens on process restart



This is the part that matters most for production reliability.



1\. On crash or restart, StreamCtx does \*\*not\*\* lose in-flight session state —

&#x20;  the last completed `tracker.checkpoint()` is already persisted (SQLite

&#x20;  WAL / Supabase row).

2\. On the next startup, call `tracker.resume(session\_id)` instead of starting

&#x20;  a fresh session. This reconstructs messages from the last checkpoint, not

&#x20;  step 1.

3\. If the crash happened \*mid-step\* (after a partial tool call but before the

&#x20;  checkpoint for that step was written), StreamCtx resumes from the \*\*last

&#x20;  fully-checkpointed step\*\* — meaning that one in-flight step will be

&#x20;  re-run. This is intentional: it trades a small amount of redundant work

&#x20;  for guaranteed consistency, rather than risking a resume from corrupted

&#x20;  partial state.

4\. If Self-Healing is enabled, a corrupted or hallucinated step detected

&#x20;  before the crash is rolled back automatically on resume — you don't need

&#x20;  to handle this manually.



\### Recovery checklist after an incident

```python

tracker = get\_tracker(agent\_id)

tracker.resume(session\_id=last\_known\_session\_id)

stats = tracker.get\_stats()

\# Confirm call\_count and last checkpoint step match what you expect

\# before letting the agent proceed with new work.

```



\## 5. Monitoring (minimal, until you have real usage)

At this stage, you don't need a full observability stack. At minimum:

\- Log `tracker.get\_stats()` output periodically (token usage, call counts,

&#x20; compression ratio) so you have a paper trail if something goes wrong.

\- Set up a basic uptime check (even a free service like UptimeRobot) against

&#x20; your agent's health endpoint, if it exposes one.



Revisit this once you have paying customers — see the roadmap for planned

status-page and alerting work.







