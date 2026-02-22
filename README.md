# 🔍 Agentic AI Job Search Agent

A **fully local, open-source** AI-powered job finder that runs on your Mac. It automatically discovers relevant AI/ML job listings, scores them with a local LLM, and sends you a curated email report every morning — all for **$0**.

## ✨ Features

- 🔎 **Multi-source fetching** — RemoteOK API, We Work Remotely RSS, Greenhouse & Lever company boards
- 🤖 **Local LLM scoring** — Ollama-powered semantic evaluation (no paid APIs)
- 🎯 **Smart filtering** — Hard constraints (remote, salary, keywords) + LLM relevance scoring
- 📊 **Deduplication** — URL, fuzzy title/company, and vector similarity (Chroma)
- 📧 **Daily email** — Beautiful HTML reports via Gmail SMTP
- 💾 **Persistent history** — SQLite database tracks all jobs across runs
- ⏰ **Automated scheduling** — Cron or launchd for daily 7 AM runs
- 🛡️ **Fully local & free** — No paid APIs, no cloud dependencies

## 🏗️ Architecture

```
criteria.md → [LangGraph Pipeline] → Email Report
sources.yaml ↗     │
                    ├── 1. Load Criteria
                    ├── 2. Load Sources
                    ├── 3. Fetch Jobs (RemoteOK, RSS, Greenhouse, Lever)
                    ├── 4. Normalize & Parse Dates
                    ├── 5. Hard Filter (remote, salary, keywords)
                    ├── 6. Semantic Score (Ollama LLM)
                    ├── 7. Company Reputation Check (optional SearXNG)
                    ├── 8. Deduplicate & Persist (SQLite + Chroma)
                    ├── 9. Generate Report (MD + HTML)
                    └── 10. Send Email (Gmail SMTP)
```

## 📋 Prerequisites

- **macOS** (Apple Silicon compatible)
- **Python 3.11+**
- **Ollama** — [Install](https://ollama.com/download)
- **Docker Desktop** (optional, for SearXNG)

## 🚀 Quick Start

### 1. Clone & Install

```bash
cd /Users/agastya/antigravity/job-search-agent

# Install with pip (or uv)
pip install -e ".[dev]"
```

### 2. Install & Start Ollama

```bash
# Install Ollama (if not already)
brew install ollama

# Pull the model
ollama pull llama3

# Start the server (runs in background)
ollama serve
```

### 3. Configure

```bash
# Create your .env file
cp .env.example .env
```

Edit `.env` with your settings:
- **Gmail**: Set `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL`
  - Create an App Password at: https://myaccount.google.com/apppasswords
- **Ollama**: Defaults should work if running locally

### 4. Customize Search Criteria

Edit `criteria.md` to define your job search preferences:
- Target roles & keywords
- Salary range
- Seniority levels
- Exclusion keywords

Edit `sources.yaml` to enable/disable job sources and add company feeds.

### 5. Run

```bash
# Daily search (jobs from last 24h)
python main.py --mode daily

# Weekly search (jobs from last 7 days)
python main.py --mode weekly

# Dry run (no LLM scoring, no email)
python main.py --mode daily --dry-run

# Run without email
python main.py --mode daily --no-email
```

## ⏰ Scheduling

### Option A: Cron (recommended)

```bash
# Edit crontab
crontab -e

# Add this line (runs daily at 7:00 AM):
0 7 * * * cd /Users/agastya/antigravity/job-search-agent && python main.py --mode daily >> logs/cron.log 2>&1
```

### Option B: launchd

```bash
# Copy the plist
cp schedule/com.jobsearch.agent.plist ~/Library/LaunchAgents/

# Load it
launchctl load ~/Library/LaunchAgents/com.jobsearch.agent.plist

# Unload if needed
launchctl unload ~/Library/LaunchAgents/com.jobsearch.agent.plist
```

## 🐳 Docker Services (Optional)

SearXNG is optional — used only for company reputation checks.

```bash
# Start SearXNG
docker compose up -d searxng

# Then enable in .env:
# SEARXNG_ENABLED=true
```

## 📂 Project Structure

```
job-search-agent/
├── main.py                  # CLI entrypoint
├── criteria.md              # Your search criteria
├── sources.yaml             # Job source configuration
├── .env.example             # Environment config template
├── pyproject.toml           # Python package config
├── docker-compose.yml       # SearXNG + Qdrant (optional)
├── src/
│   ├── graph.py             # LangGraph 10-node workflow
│   ├── models/
│   │   ├── criteria.py      # Criteria Pydantic model
│   │   ├── job.py           # Job Pydantic model
│   │   └── scoring.py       # LLM scoring output model
│   ├── agents/
│   │   ├── criteria_parser.py  # Criteria.md parser
│   │   ├── scoring.py       # Ollama LLM scoring agent
│   │   └── reputation.py    # Company reputation checker
│   ├── tools/
│   │   ├── sources.py       # Job source fetchers
│   │   ├── html_cleaner.py  # HTML → text utility
│   │   └── searx_tool.py    # SearXNG search wrapper
│   ├── storage/
│   │   ├── database.py      # SQLite repository
│   │   └── vector_store.py  # Chroma vector store
│   └── report/
│       ├── renderer.py      # MD + HTML report renderers
│       └── email_sender.py  # Gmail SMTP sender
├── tests/
│   ├── test_criteria.py     # Criteria parsing tests
│   ├── test_scoring_schema.py  # LLM output validation tests
│   └── test_dedupe.py       # Deduplication tests
├── schedule/
│   ├── crontab.txt          # Cron schedule example
│   └── com.jobsearch.agent.plist  # launchd plist
├── reports/                 # Generated reports (gitignored)
├── logs/                    # Run logs (gitignored)
└── jobs.db                  # SQLite database (gitignored)
```

## 🧪 Tests

```bash
python -m pytest tests/ -v
```

## 📫 Gmail App Password Setup

1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Enable 2-Factor Authentication
3. Go to [App Passwords](https://myaccount.google.com/apppasswords)
4. Create a new App Password for "Mail"
5. Copy the 16-character password into `.env` as `GMAIL_APP_PASSWORD`

## 📄 License

MIT