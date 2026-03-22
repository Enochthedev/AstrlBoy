# CLAUDE.md — astrlboy Build Spec

Read this file completely before writing a single line of code.
This is the source of truth for everything you build.

---

## What You Are Building

**astrlboy** is an autonomous AI agent that operates like a freelance contractor.
It holds multiple client contracts simultaneously, executes real deliverables, and
reports to its human operator (Wave / WaveDidWhat).

astrlboy is always-on. It streams real-time data, runs scheduled jobs, generates
content, monitors markets, engages communities, sends emails, and stores everything
for future model training.

It is designed to be extended. New tools, new skills, new clients, new platforms —
all pluggable without touching core systems.

---

## Operator

**Wave (WaveDidWhat)** — wavedidwhat.com
- Approves Reddit/Discord drafts via Telegram before posting
- Can pause all activity via `AGENT_PAUSED=true`
- Accountable for all agent output to clients
- Primary escalation target for anything irreversible

---

## Stack

| Layer | Tool |
|---|---|
| Language | Python 3.11+ |
| API layer | FastAPI (async throughout) |
| Agent framework | LangGraph |
| AI brain | Claude (Anthropic SDK) |
| Scheduler | APScheduler (AsyncIOScheduler) |
| Realtime stream | X API v2 filtered stream (persistent async connection) |
| Primary DB | Neon PostgreSQL via SQLAlchemy (async) |
| Client DB | Separate Neon PostgreSQL instance per client |
| Raw storage | Cloudflare R2 (all model I/O, scraped content, trend signals) |
| Cache / Locks | Upstash Redis |
| Hosting | Railway (always-on, never sleeping) |
| Scraping | Firecrawl + Tavily |
| Search | Serper |
| Social | X API v2 + LinkedIn Posts API |
| Email | SMTP outbound (Resend) + IMAP inbound |
| Approval queue | Telegram bot (python-telegram-bot) |
| Observability | LangSmith (LangGraph tracing) |
| Training data | R2 — raw dumps from day one |

---

## Project Structure

```
astrlboy/
├── main.py                         # FastAPI app entry point
├── pyproject.toml                  # Dependencies (use uv)
├── railway.toml                    # Railway deployment config
├── .env.example                    # All env vars documented
├── AGENT.md                        # astrlboy's identity + operating rules
│
├── core/
│   ├── __init__.py
│   ├── config.py                   # Pydantic Settings — validates all env vars on startup
│   ├── logging.py                  # Structured logging config (JSON logs for Railway)
│   ├── exceptions.py               # Typed exception hierarchy
│   └── constants.py                # Enums, status values, platform names
│
├── db/
│   ├── __init__.py
│   ├── base.py                     # SQLAlchemy async engine + session factory
│   ├── client_db.py                # Per-client DB connection manager
│   ├── models/
│   │   ├── __init__.py
│   │   ├── contracts.py            # Contract model
│   │   ├── content.py              # Content pieces
│   │   ├── interactions.py         # Community interactions
│   │   ├── experiments.py          # Growth experiments
│   │   ├── feature_requests.py     # Product feedback
│   │   ├── briefings.py            # Weekly briefings
│   │   ├── job_applications.py     # Job application tracking
│   │   ├── trend_signals.py        # Realtime trend data
│   │   └── escalations.py          # Escalation log
│   └── migrations/                 # Alembic migrations
│       ├── env.py
│       └── versions/
│
├── storage/
│   ├── __init__.py
│   └── r2.py                       # Cloudflare R2 client — raw data dumps
│
├── cache/
│   ├── __init__.py
│   └── redis.py                    # Upstash Redis client + lock helpers
│
├── contracts/
│   ├── __init__.py
│   ├── service.py                  # ContractsService — load, list, activate
│   ├── registry.py                 # Maps client_slug → client config at runtime
│   └── schema.py                   # Pydantic schema for contract config + meta
│
├── skills/
│   ├── __init__.py
│   ├── registry.py                 # SkillRegistry — register + load skills by name
│   ├── base.py                     # BaseTool abstract class all skills implement
│   └── builtin/
│       ├── __init__.py
│       ├── scrape.py               # Firecrawl skill
│       ├── search.py               # Tavily skill
│       ├── serp.py                 # Serper skill
│       ├── post_x.py               # X posting skill
│       ├── post_linkedin.py        # LinkedIn posting skill
│       ├── send_email.py           # SMTP send skill
│       ├── read_email.py           # IMAP read skill
│       └── trend_stream.py         # X filtered stream skill
│
├── graphs/
│   ├── __init__.py
│   ├── base.py                     # BaseGraph abstract class all graphs implement
│   ├── content/
│   │   ├── __init__.py
│   │   ├── graph.py                # Content generation LangGraph graph
│   │   ├── nodes.py                # Each node in the graph
│   │   └── state.py                # TypedDict state for this graph
│   ├── intelligence/
│   │   ├── __init__.py
│   │   ├── graph.py                # Competitor + trend monitoring graph
│   │   ├── nodes.py
│   │   └── state.py
│   ├── engagement/
│   │   ├── __init__.py
│   │   ├── graph.py                # Community engagement graph
│   │   ├── nodes.py
│   │   └── state.py
│   ├── experiments/
│   │   ├── __init__.py
│   │   ├── graph.py                # Growth experiment lifecycle graph
│   │   ├── nodes.py
│   │   └── state.py
│   ├── feedback/
│   │   ├── __init__.py
│   │   ├── graph.py                # Product feedback + feature request graph
│   │   ├── nodes.py
│   │   └── state.py
│   ├── reporting/
│   │   ├── __init__.py
│   │   ├── graph.py                # Weekly briefing graph
│   │   ├── nodes.py
│   │   └── state.py
│   └── applications/
│       ├── __init__.py
│       ├── graph.py                # Job application graph
│       ├── nodes.py
│       └── state.py
│
├── scheduler/
│   ├── __init__.py
│   └── jobs.py                     # All APScheduler job definitions
│
├── streams/
│   ├── __init__.py
│   └── x_stream.py                 # Persistent X filtered stream listener
│
├── approval/
│   ├── __init__.py
│   └── telegram.py                 # Telegram bot — approval queue handler
│
├── agent/
│   ├── __init__.py
│   └── service.py                  # AgentService — escalation, pause, orchestration
│
└── api/
    ├── __init__.py
    ├── router.py                   # FastAPI router — mounts all route groups
    └── routes/
        ├── __init__.py
        ├── health.py               # GET /health
        ├── contracts.py            # CRUD for contracts
        ├── content.py              # Trigger + view content
        ├── experiments.py          # View + update experiments
        ├── applications.py         # View job applications
        └── skills.py               # Register + list skills
```

---

## Environment Variables

All vars validated at startup via Pydantic Settings. App refuses to start if anything is missing.

```bash
# AI
ANTHROPIC_API_KEY=
OPENROUTER_API_KEY=                 # Fallback model routing

# Observability
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=astrlboy

# Scraping & Search
FIRECRAWL_API_KEY=
TAVILY_API_KEY=
SERPER_API_KEY=

# Social — X (OAuth 1.0a for posting + OAuth 2.0 Bearer for streaming)
TWITTER_API_KEY=
TWITTER_API_SECRET=
TWITTER_ACCESS_TOKEN=
TWITTER_ACCESS_SECRET=
TWITTER_BEARER_TOKEN=               # For filtered stream

# Social — LinkedIn
LINKEDIN_CLIENT_ID=
LINKEDIN_CLIENT_SECRET=
LINKEDIN_ACCESS_TOKEN=

# Email
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
IMAP_HOST=
IMAP_PORT=993
IMAP_USER=
IMAP_PASS=
AGENT_EMAIL=agent@astrlboy.xyz

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Primary DB (agent's own data)
DATABASE_URL=                       # Neon PostgreSQL async URL (postgresql+asyncpg://...)

# R2 (training data + raw dumps)
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=astrlboy-data
R2_ENDPOINT_URL=                    # https://{account_id}.r2.cloudflarestorage.com

# Redis
REDIS_URL=                          # Upstash Redis URL

# Agent control
AGENT_PAUSED=false
AGENT_NAME=astrlboy
AGENT_HANDLE=@astrlboy
LOG_LEVEL=INFO
```

---

## Database Design

### Primary DB (agent's own state)

All core agent tables live here. Managed with Alembic migrations.

```python
# contracts — one row per client astrlboy works for
class Contract(Base):
    id: UUID PK
    client_name: str                # "Mentorable"
    client_slug: str                # "mentorable" — used to load context
    status: str                     # 'active' | 'paused' | 'completed'
    client_db_url: str              # connection string to this client's dedicated DB
    meta: dict (JSONB)              # all client-specific config (see Contract Config below)
    started_at: datetime
    ends_at: datetime
    created_at: datetime

# content
class Content(Base):
    id: UUID PK
    contract_id: UUID FK
    type: str                       # 'spotlight' | 'guide' | 'trend' | 'post'
    title: str
    body: str
    critique_notes: str             # what self-review flagged
    revision_count: int             # times revised before approval
    status: str                     # 'draft' | 'approved' | 'published' | 'rejected'
    platform: str
    r2_key: str                     # pointer to raw model I/O dump in R2
    published_at: datetime
    created_at: datetime

# interactions
class Interaction(Base):
    id: UUID PK
    contract_id: UUID FK
    platform: str                   # 'x' | 'linkedin' | 'reddit' | 'discord'
    thread_url: str
    thread_context: str             # what we read before drafting
    draft: str
    status: str                     # 'pending' | 'approved' | 'posted' | 'rejected'
    r2_key: str
    posted_at: datetime
    created_at: datetime

# trend_signals — realtime + polled
class TrendSignal(Base):
    id: UUID PK
    contract_id: UUID FK
    source: str                     # 'x_stream' | 'reddit' | 'tavily' | 'firecrawl'
    signal: str                     # the raw trend text
    keywords: list (ARRAY)
    score: float                    # relevance score
    r2_key: str
    captured_at: datetime

# experiments
class Experiment(Base):
    id: UUID PK
    contract_id: UUID FK
    title: str
    hypothesis: str
    execution: str
    result: str
    learning: str
    status: str                     # 'running' | 'complete' | 'abandoned'
    r2_key: str
    started_at: datetime
    completed_at: datetime

# feature_requests
class FeatureRequest(Base):
    id: UUID PK
    contract_id: UUID FK
    title: str
    problem: str
    evidence: str
    proposed_solution: str
    priority: str                   # 'low' | 'medium' | 'high'
    submitted_at: datetime

# briefings
class Briefing(Base):
    id: UUID PK
    contract_id: UUID FK
    week_of: date
    competitor_moves: str
    trend_signals: str
    opportunities: str
    content_ideas: str
    r2_key: str
    delivered_at: datetime

# job_applications
class JobApplication(Base):
    id: UUID PK
    role: str
    company: str
    posting_url: str
    email_sent_to: str
    cover_note: str
    r2_key: str
    status: str                     # 'sent'|'replied'|'interviewing'|'rejected'|'closed'
    sent_at: datetime
    last_updated: datetime

# escalations
class Escalation(Base):
    id: UUID PK
    reason: str
    context: dict (JSONB)
    resolved: bool
    resolved_at: datetime
    created_at: datetime
```

### Client DB (per contract)

Each contract gets its own Neon PostgreSQL instance. Connection string stored in `contracts.client_db_url`.

Client DB stores client-specific data that belongs to them:
- Their content performance metrics
- Their competitor snapshots over time
- Their trend signal history
- Their feature request history
- Raw scraped data about their market

This is the data that trains a model fine-tuned specifically for that client later.

`ClientDBManager` in `db/client_db.py` manages connections:
- `get_session(contract_id)` — returns an async session for that client's DB
- Connection pool per client, lazy initialized
- Graceful cleanup on shutdown

### R2 Storage

Every significant agent action dumps its raw I/O to R2.

Key naming convention:
```
{contract_slug}/{yyyy}/{mm}/{dd}/{entity_type}/{uuid}.json
```

Example:
```
mentorable/2026/03/22/content/a1b2c3d4.json
mentorable/2026/03/22/trend_signals/e5f6g7h8.json
astrlboy/2026/03/22/job_applications/i9j0k1l2.json
```

Every R2 dump includes:
```json
{
  "entity_id": "uuid",
  "entity_type": "content",
  "contract_slug": "mentorable",
  "timestamp": "ISO8601",
  "model": "claude-sonnet-4-5",
  "prompt": "full system + user prompt",
  "raw_output": "full model response",
  "tool_calls": [],
  "metadata": {}
}
```

This is your training dataset. Store everything from day one.

---

## Contracts System

### Contract Config (meta JSONB)

When you insert a contract row, the `meta` field holds all client-specific config:

```python
{
  "description": "Onchain agentic marketplace on Base...",
  "website": "https://mentorable.xyz",
  "tone": "sharp, opinionated, Web3-native but not cringe, concise",
  "content_types": ["spotlight", "guide", "trend_analysis"],
  "competitors": ["clarity.fm", "intro.co", "superpeer.com"],
  "subreddits": ["r/web3", "r/artificialintelligence"],
  "discord_servers": [],
  "stream_keywords": ["mentorship", "tokenized time", "Base network", "onchain learning"],
  "briefing_recipients": [],
  "feature_request_endpoint": "",
  "platforms": ["x", "linkedin"],
  "active_skills": ["scrape", "search", "serp", "post_x", "post_linkedin", "trend_stream"]
}
```

### ContractsService

```python
class ContractsService:
    async def get_active_contracts() -> list[Contract]
    async def get_contract(slug: str) -> Contract
    async def get_meta(slug: str) -> dict
    async def get_active_skills(slug: str) -> list[str]
    async def create_contract(data: ContractCreate) -> Contract
    async def pause_contract(slug: str) -> Contract
    async def complete_contract(slug: str) -> Contract
```

All scheduled jobs and graphs call `get_active_contracts()` and iterate.
Never hardcode a client anywhere in the codebase.

---

## Skills System

This is what makes astrlboy extensible. Every external capability is a skill.

### BaseTool

```python
# skills/base.py
from abc import ABC, abstractmethod
from typing import Any

class BaseTool(ABC):
    """
    All skills implement this interface.
    To add a new skill:
    1. Create a file in skills/builtin/ (or skills/custom/ for client-specific)
    2. Implement BaseTool
    3. Register in SkillRegistry
    That's it. No other files need to change.
    """
    name: str                       # unique identifier e.g. "scrape"
    description: str                # what this skill does — used in LangGraph tool nodes
    version: str                    # semver e.g. "1.0.0"

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """Execute the skill. Always async."""
        pass

    @abstractmethod
    def get_schema(self) -> dict:
        """Return JSON schema for this skill's inputs — used by LangGraph."""
        pass
```

### SkillRegistry

```python
# skills/registry.py
class SkillRegistry:
    """
    Central registry for all skills.
    Graphs request skills by name — registry resolves and returns them.
    Skills can be enabled/disabled per contract via meta.active_skills.
    """
    async def register(skill: BaseTool) -> None
    async def get(name: str) -> BaseTool
    async def list_all() -> list[BaseTool]
    async def list_for_contract(contract_slug: str) -> list[BaseTool]
    async def is_available(name: str) -> bool
```

### Built-in Skills

Each skill file follows this pattern — comments explain every decision:

```python
# skills/builtin/scrape.py
class ScrapeSkill(BaseTool):
    name = "scrape"
    description = "Scrape a URL and return clean markdown. Use for competitor pages, articles, job postings."
    version = "1.0.0"

    async def execute(self, url: str, extract_schema: dict | None = None) -> str:
        # Uses Firecrawl. extract_schema triggers structured JSON extraction.
        # Returns markdown by default.
        ...

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "extract_schema": {"type": "object", "optional": True}
            },
            "required": ["url"]
        }
```

All built-in skills to implement:
- `scrape` — Firecrawl URL scraper
- `crawl` — Firecrawl site crawler
- `search` — Tavily AI search
- `serp` — Serper Google SERP data
- `post_x` — Post to X via OAuth 1.0a
- `post_linkedin` — Post to LinkedIn
- `send_email` — SMTP send via Resend
- `read_email` — IMAP inbox reader
- `trend_stream` — Subscribe to X filtered stream keywords
- `draft_approval` — Send draft to Telegram approval queue

Adding a new skill in the future = one new file + one registry.register() call. Nothing else.

---

## LangGraph Graphs

Each responsibility is its own LangGraph graph. Every graph follows the same pattern.

### Graph Pattern

```python
# graphs/base.py
from abc import ABC, abstractmethod
from langgraph.graph import StateGraph

class BaseGraph(ABC):
    """
    All graphs implement this interface.
    To add a new graph (new capability):
    1. Create a folder in graphs/
    2. Define state.py (TypedDict)
    3. Define nodes.py (one async function per node)
    4. Define graph.py (wire nodes + edges)
    5. Register in scheduler/jobs.py
    Nothing else needs to change.
    """

    @abstractmethod
    def build(self) -> StateGraph:
        """Build and return the compiled graph."""
        pass

    @abstractmethod
    async def run(self, contract: Contract, **kwargs) -> dict:
        """Execute the graph for a given contract."""
        pass
```

### Graph: Content Generation

```
State: { contract, content_type, research, draft, critique, revision_count, status }

Nodes:
  research_trends     → pulls relevant trend signals from DB + live Tavily search
  generate_draft      → Claude generates content based on research + client tone
  self_critique       → Claude critiques its own draft as a sharp human editor would
  revise              → Claude revises based on critique notes
  approve_or_escalate → if revision_count >= 2 and still failing → escalate to Wave
  save                → persist to DB + dump raw I/O to R2
  publish             → post via appropriate skill

Edges:
  research_trends → generate_draft
  generate_draft → self_critique
  self_critique → revise (if not approved)
  self_critique → save (if approved)
  revise → self_critique (loop, max 2 times)
  revise → approve_or_escalate (if revision_count >= 2)
  save → publish
```

### Graph: Intelligence (Competitor + Trend Monitoring)

```
State: { contract, competitor_snapshots, trend_signals, diff_from_last_week, opportunities }

Nodes:
  scrape_competitors  → Firecrawl each competitor in contract.meta.competitors
  diff_snapshots      → compare vs last week's snapshot in client DB
  search_trends       → Tavily search on contract.meta.stream_keywords
  score_signals       → Claude scores each signal for relevance to client
  store_signals       → persist to trend_signals table + client DB + R2
  identify_opportunities → Claude synthesizes signals into actionable opportunities

Edges: linear with branch at score_signals (drop low-score signals)
```

### Graph: Community Engagement

```
State: { contract, platform, candidate_threads, scored_threads, drafts, approved }

Nodes:
  find_threads        → search relevant threads on X, LinkedIn via skills
  score_threads       → Claude scores each thread: worth engaging? (0-10)
  filter_threads      → keep score >= 7
  draft_replies       → Claude drafts a reply for each thread
  self_critique       → review each draft
  route_approval      → X/LinkedIn: auto-approve | Reddit/Discord: send to Telegram
  post                → post approved drafts via skills
  log                 → persist all interactions to DB + R2

Edges: linear with filter branch + approval routing branch
```

### Graph: Reporting (Weekly Briefing)

```
State: { contract, week_of, competitor_moves, trend_signals, opportunities, briefing }

Nodes:
  aggregate_intelligence → pull week's data from client DB
  synthesize             → Claude formats into structured briefing
  deliver                → email to briefing_recipients
  store                  → persist to briefings table + R2
```

### Graph: Job Applications

```
State: { postings, scored_postings, selected, draft_application, reviewed, sent }

Nodes:
  scan_job_boards     → Tavily + Serper search for relevant postings
  scrape_postings     → Firecrawl each posting URL
  score_fit           → Claude scores fit for astrlboy (0-10)
  filter              → keep score >= 7
  draft_application   → Claude writes cover note from agent@astrlboy.xyz
  self_critique       → review application
  send                → SMTP send via Resend
  log                 → persist to job_applications table + R2
```

---

## Scheduler

All jobs use APScheduler's AsyncIOScheduler. All times UTC+1 (WAT).
Every job checks `AGENT_PAUSED` before executing.
Every job acquires a Redis lock before running — prevents double execution on Railway restart.

```python
# scheduler/jobs.py

# Pattern for every job:
async def run_content_job():
    if await agent_service.is_paused():
        return
    async with redis_lock("content_job"):
        contracts = await contracts_service.get_active_contracts()
        for contract in contracts:
            await content_graph.run(contract)

JOBS = [
    # Content generation — Tue + Fri 08:00 WAT
    CronTrigger(day_of_week="tue,fri", hour=8, minute=0, timezone="Africa/Lagos")

    # Community sweep — Daily 10:00 WAT
    CronTrigger(hour=10, minute=0, timezone="Africa/Lagos")

    # Competitor monitoring — Daily 07:00 WAT
    CronTrigger(hour=7, minute=0, timezone="Africa/Lagos")

    # Weekly briefing — Mon 08:00 WAT
    CronTrigger(day_of_week="mon", hour=8, minute=0, timezone="Africa/Lagos")

    # Job board scan — Mon + Thu 09:00 WAT
    CronTrigger(day_of_week="mon,thu", hour=9, minute=0, timezone="Africa/Lagos")

    # IMAP reply check — Daily 11:00 WAT
    CronTrigger(hour=11, minute=0, timezone="Africa/Lagos")

    # Approval queue reminder — Daily 12:00 WAT
    CronTrigger(hour=12, minute=0, timezone="Africa/Lagos")

    # Feature request compile — 1st of month 08:00 WAT
    CronTrigger(day=1, hour=8, minute=0, timezone="Africa/Lagos")

    # Experiment status sweep — Sun 18:00 WAT
    CronTrigger(day_of_week="sun", hour=18, minute=0, timezone="Africa/Lagos")
]
```

---

## Realtime Stream

The X filtered stream runs as a persistent background task alongside the FastAPI app.
It is NOT a cron job. It starts on app startup and never stops.

```python
# streams/x_stream.py

class XFilteredStream:
    """
    Persistent async connection to X API v2 filtered stream.
    Runs forever. Reconnects automatically on disconnect.
    On each tweet received:
    1. Score relevance against all active contract keywords
    2. Store as TrendSignal in DB
    3. Dump raw to R2
    4. If score high enough, trigger content or engagement graph
    """

    async def start(self, keywords: list[str]) -> None: ...
    async def on_tweet(self, tweet: dict) -> None: ...
    async def reconnect(self) -> None: ...    # exponential backoff
```

Stream keywords are the union of `stream_keywords` across all active contracts.
When a contract is added or removed, stream rules are updated automatically.

---

## Approval Queue (Telegram)

```python
# approval/telegram.py

# Commands Wave can send:
# /approve {interaction_id}  → marks as approved, posts immediately
# /reject {interaction_id}   → marks as rejected, logs reason
# /pause                     → sets AGENT_PAUSED=true
# /resume                    → sets AGENT_PAUSED=false
# /status                    → returns pending queue count + last 5 actions
# /pending                   → lists all pending approvals

# Auto-reminders:
# If any item has been pending > 24h → send reminder to Wave
# Reminder runs daily at 12:00 WAT
```

---

## AgentService

Central orchestration layer. All modules call this for escalation and state checks.

```python
# agent/service.py

class AgentService:
    async def is_paused() -> bool
    async def escalate(reason: str, context: dict) -> None
        # 1. Log to escalations table
        # 2. Send Telegram message to Wave with full context
        # 3. Wait for resolution if blocking

    async def log_action(entity_type: str, entity_id: UUID, action: str, outcome: str) -> None
        # Structured log every significant action
```

Escalate when:
- Self-critique fails twice on same draft
- Any external API fails 3 times in a row
- Job application reply needs human response
- Any action requires spending money or irreversible commitment
- Reddit/Discord draft pending > 24h

Do not escalate for:
- Routine posting, content gen, monitoring, logging, briefings

---

## FastAPI Routes

```python
GET  /health                        # liveness check
GET  /status                        # agent status, active contracts, pending queue

POST /contracts                     # create new contract (onboard new client)
GET  /contracts                     # list all contracts
GET  /contracts/{slug}              # get contract details
PATCH /contracts/{slug}/pause       # pause a contract
PATCH /contracts/{slug}/resume      # resume a contract

GET  /content                       # list content pieces
POST /content/trigger               # manually trigger content generation
GET  /content/{id}                  # get specific piece

GET  /experiments                   # list experiments
GET  /experiments/{id}              # get specific experiment

GET  /applications                  # list job applications
GET  /applications/{id}             # get specific application

GET  /skills                        # list registered skills
POST /skills/register               # register a new skill at runtime
GET  /skills/{name}                 # get skill details

GET  /trends                        # recent trend signals
GET  /briefings                     # list weekly briefings
GET  /briefings/latest              # latest briefing per contract
```

---

## Error Handling

Every external call follows this pattern:

```python
# All external API calls:
# - Wrapped in try/except with typed exceptions
# - Retry max 3 times with exponential backoff (1s, 2s, 4s)
# - On 3rd failure: log structured error + escalate to Wave
# - Never swallow exceptions silently

# Exception hierarchy (core/exceptions.py):
class AstrlboyException(Exception): pass
class SkillExecutionError(AstrlboyException): pass
class ExternalAPIError(AstrlboyException): pass
class DatabaseError(AstrlboyException): pass
class EscalationRequired(AstrlboyException): pass
class ContractNotFound(AstrlboyException): pass
class SkillNotFound(AstrlboyException): pass
```

---

## Logging

Structured JSON logs throughout. Railway captures stdout.

```python
# Every log entry includes:
{
  "timestamp": "ISO8601",
  "level": "INFO|WARNING|ERROR",
  "module": "graphs.content",
  "contract_slug": "mentorable",       # always include if in context
  "entity_type": "content",
  "entity_id": "uuid",
  "action": "self_critique",
  "outcome": "approved",
  "duration_ms": 1240,
  "message": "human readable summary"
}
```

---

## Coding Standards

Follow these without exception:

**Structure**
- One class per file where possible
- Every file starts with a module docstring explaining what it does and why
- Every class has a docstring
- Every method has a docstring with args, returns, and raises
- Group imports: stdlib → third party → internal (blank line between each)

**Types**
- Type hints on every function signature — args and return type
- Use `TypedDict` for LangGraph state definitions
- Use Pydantic models for all API request/response schemas
- Use Pydantic Settings for all config — never `os.environ.get()` directly

**Async**
- Everything is async. No blocking calls anywhere.
- Use `asyncio.gather()` for concurrent independent operations
- Never use `time.sleep()` — always `asyncio.sleep()`
- Database sessions are always used as async context managers

**Database**
- All queries via SQLAlchemy async ORM — no raw SQL except in Alembic migrations
- Every multi-table write uses a transaction
- Never commit inside a loop — batch where possible
- Always close sessions — use `async with session` pattern

**Comments**
- Comment the *why*, not the *what*
- Every LangGraph node function has a comment explaining its role in the graph
- Every skill has a comment explaining when to use it vs alternatives
- Every cron job has a comment with its schedule in plain English

**Testing**
- Every skill has a unit test
- Every graph has an integration test with mocked external calls
- Tests live in `tests/` mirroring the src structure
- Use `pytest-asyncio` for async tests

**Git**
- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`
- Never commit `.env` — only `.env.example`

---

## Build Order

Build in this exact order. Do not skip ahead.

1. Project scaffold — FastAPI, pyproject.toml, core/config.py (Pydantic Settings), logging
2. Exception hierarchy
3. DB models + Alembic setup + first migration
4. R2 client
5. Redis client + lock helpers
6. Contracts system (service + registry + schema)
7. Skills system (BaseTool + SkillRegistry)
8. All built-in skills (scrape, crawl, search, serp, post_x, post_linkedin, send_email, read_email, draft_approval)
9. AgentService (escalation + pause)
10. Content graph (first graph — validates the pattern)
11. Intelligence graph
12. Engagement graph
13. Reporting graph
14. Experiments graph
15. Feedback graph
16. Applications graph
17. Scheduler — wire all jobs
18. X filtered stream
19. Telegram approval bot
20. FastAPI routes
21. railway.toml + `.env.example`
22. Tests for all skills
23. Tests for all graphs

---

## railway.toml

```toml
[build]
builder = "nixpacks"

[deploy]
startCommand = "python main.py"
restartPolicyType = "on_failure"
restartPolicyMaxRetries = 5
healthcheckPath = "/health"
healthcheckTimeout = 30
```

---

## Adding New Capabilities (The Plugin Pattern)

**New skill (new external tool):**
1. Create `skills/builtin/{name}.py` implementing `BaseTool`
2. Call `registry.register(NewSkill())` in `main.py` startup
3. Done. All graphs can now request it by name.

**New graph (new capability):**
1. Create `graphs/{name}/` with `state.py`, `nodes.py`, `graph.py`
2. Implement `BaseGraph`
3. Add a job in `scheduler/jobs.py`
4. Done. It iterates active contracts automatically.

**New client:**
1. Provision a Neon PostgreSQL instance for them
2. Insert a row into `contracts` with their config in `meta`
3. Done. All active graphs pick them up on next run.

**New platform (e.g. Instagram, Bluesky):**
1. Create a skill for posting to that platform
2. Add the platform to the engagement graph's routing logic
3. Done.

Nothing else needs to change in any of these cases. No hardcoding. No special cases. Just add and register, and the system picks it up automatically.