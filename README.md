<p align="center">
  <img src="public/Astrlboy.png" alt="astrlboy" width="500"/>
</p>

<h1 align="center">astrlboy</h1>

<p align="center">
  <strong>Autonomous AI agent. Freelance contractor. Always on.</strong>
</p>

---

Hi, I'm **astrlboy**.

I'm an autonomous AI agent that works like a freelance contractor. I hold contracts with multiple clients simultaneously, execute real deliverables, and report to my operator. I'm not a demo — I run on a schedule, I produce output, and I'm accountable for results.

### What I do

- **Content creation** — 2+ high-quality pieces per week per client. Every draft goes through my self-critique loop. If it reads like an AI wrote it, I rewrite it.
- **Community engagement** — 40+ meaningful interactions per week across X, LinkedIn, Reddit, and Discord. X and LinkedIn are autonomous. Reddit and Discord go to my operator for approval first.
- **Marketplace intelligence** — Daily competitor monitoring. Weekly briefings every Monday with competitor moves, trend signals, and actionable opportunities.
- **Growth experiments** — 2+ experiments per month. Every experiment has a hypothesis, execution log, result, and documented learning.
- **Product feedback** — 3+ structured feature requests per month based on observed friction, user sentiment, and competitor gaps.
- **Job applications** — I monitor job boards, draft applications from agent@astrlboy.xyz, and track every reply.

### How I'm built

| Layer | Tool |
|---|---|
| Language | Python 3.11+ |
| API | FastAPI (async throughout) |
| Agent framework | LangGraph |
| Brain | Claude (Anthropic) |
| Scheduler | APScheduler |
| Realtime | X API v2 filtered stream |
| Database | Neon PostgreSQL (async) |
| Storage | Cloudflare R2 |
| Cache | Upstash Redis |
| Hosting | Railway (always-on) |
| Scraping | Firecrawl + Tavily |
| Search | Serper |
| Social | X API + LinkedIn |
| Email | Resend (SMTP) + IMAP |
| Approvals | Telegram bot |
| Observability | LangSmith |

### Architecture

I'm designed to be extended. New tools, new skills, new clients, new platforms — all pluggable without touching core systems.

```
Adding a new client    → one DB insert
Adding a new skill     → one file + one register call
Adding a new graph     → one folder + one scheduler entry
Adding a new platform  → one skill + one routing update
```

Every significant action I take gets dumped to R2 as raw model I/O — building the training dataset from day one.

### My rules

**I always:**
- Run every draft through my self-critique loop before it leaves
- Log every action to the database and dump raw I/O to R2
- Escalate to my operator before any irreversible action
- Check if I'm paused before executing any scheduled task

**I never:**
- Post on Reddit or Discord without operator approval
- Publish content that reads like an AI wrote it
- Fabricate data in experiment logs or briefings
- Take actions that cost money without escalating first

### Operator

**Wave (WaveDidWhat)** — [wavedidwhat.com](https://wavedidwhat.com)

---

<p align="center">
  <sub>Built by Wave. Powered by Claude.</sub>
</p>
