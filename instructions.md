# instructions.md — Local, Open-Source Agentic Job Finder (Daily Email)

## 1) Objective

Build a **fully local, open-source agentic AI system** that:

1. Reads my job criteria from a local file (`criteria.md` or `criteria.txt`).
2. Finds **new job listings posted within the last 24 hours** (daily mode) and optionally **last 7 days** (weekly mode).
3. Filters jobs by **hard constraints** (remote, full-time, salary range, etc.).
4. Uses a **local LLM (Ollama)** for semantic evaluation and scoring (e.g., “agentic AI fit”, “reputed company”, seniority match).
5. Deduplicates against prior runs and maintains a local history.
6. Produces a curated report with **titles, companies, dates, salary (if available), match rationale, and links**.
7. Sends the report to my **Gmail** every morning automatically.
8. Runs locally on my **Mac** and costs **$0** (no paid APIs, no paid SaaS dependencies).

The system must be reliable, deterministic where possible, and easy to run/maintain.

---

## 2) Non-Negotiable Requirements (Hard Constraints)

### 2.1 Cost & Licensing
- Use **only open-source / free** components.
- Do **not** use paid APIs (no SerpAPI, no LinkedIn API, no paid email services).
- Use **legal sources** and avoid ToS-violating scraping (especially LinkedIn scraping).

### 2.2 Runtime Environment
- Must run locally on macOS (Apple Silicon compatible).
- Orchestrate all tasks locally; can use Docker locally for supporting services.

### 2.3 LLM
- Must use **Ollama** for LLM inference (local).
- Default model: `llama3` (configurable).

### 2.4 Scheduling
- Must run automatically **every morning** (default 7:00 AM local time).
- Use `cron` or `launchd` (choose simplest; default `cron`).

### 2.5 Email Delivery
- Send email to Gmail using **Gmail SMTP** (App Password).
- No paid email services. No external hosted servers required.

### 2.6 Data Sources (Legal / Accessible)
- Prefer sources with:
  - RSS feeds
  - public JSON APIs
  - public company job board feeds

Minimum supported sources:
- RemoteOK API (`https://remoteok.com/api`)
- We Work Remotely RSS (`https://weworkremotely.com/remote-jobs.rss`)
- Company boards:
  - Greenhouse RSS: `https://boards.greenhouse.io/{company}.rss`
  - Lever RSS: `https://jobs.lever.co/{company}?format=rss`

Optional “discover more sources” mechanism:
- Local metasearch engine (SearXNG) used only to discover legitimate job board links (not to scrape LinkedIn).

### 2.7 Robustness
- Must not crash if:
  - salary field is missing
  - date parsing fails for some sources
  - job text contains malformed HTML
  - LLM output is not valid JSON (must recover and re-try or fall back)

---

## 3) Preferred Tech Stack (Finalize)

### 3.1 Orchestration / Agents
- Use **LangGraph** for workflow orchestration (stateful agent/pipeline).
- Use **LangChain** for tool calling, LLM wrappers, and retrieval utilities.

### 3.2 API / Service Layer (Optional but Recommended)
- Use **LangServe** to expose a local HTTP API:
  - Endpoint: `/run` (daily mode)
  - Endpoint: `/run_weekly` (weekly mode)
  - Endpoint: `/health`

### 3.3 Observability
- Must be fully local and free.
- Default: structured logging to local files + console.
- If adding tracing/evals, use an open-source alternative (e.g., Langfuse self-host) but **do not require it**.

### 3.4 Storage
- Use **SQLite** for:
  - job history
  - dedupe keys
  - run metadata (run time, counts, errors)
- Must store a “job signature” for dedupe (URL + normalized title/company).

### 3.5 Vector DB (Local)
Choose one (prefer easiest + Pythonic):
- Option A (recommended): **Chroma** embedded locally (no server needed).
- Option B: **Qdrant** in Docker (localhost).

Vector DB usage:
- Store job descriptions and embeddings for:
  - semantic dedupe (near-duplicates)
  - preference learning (future enhancement)
  - retrieval for better LLM scoring explanations

### 3.6 Web Search (Local)
- Use **SearXNG** running locally in Docker as a metasearch tool.
- It is used only for discovering additional job board URLs and verifying company signals (not scraping restricted sites).

### 3.7 Validation
- Use **Pydantic** for:
  - criteria schema
  - job schema
  - LLM output schema validation (strict)
- If LLM output fails validation, re-try with a “repair prompt” or fallback.

---

## 4) Inputs / Outputs

### 4.1 Inputs
1) `criteria.md` (human-written requirements)
2) `sources.yaml` (list of sources to fetch)
3) `.env` (secrets and config)

#### 4.1.1 criteria.md format
The system must support a criteria file containing:
- Mandatory constraints:
  - fully remote only
  - full-time only
  - no hourly/contract/1099
  - salary range (base preferred) as min/max
  - target roles/keywords
  - seniority levels
  - exclusion keywords
  - posted-within window (1 day or 7 days)

It should be easy to edit without changing code.

#### 4.1.2 sources.yaml format
Provide a `sources.yaml` file with sources and type:
- remoteok_api
- rss
- greenhouse_company
- lever_company
- custom_rss_urls

Example fields:
- `name`
- `type`
- `url` (or `company_slug`)
- `enabled`

### 4.2 Outputs
1) Daily email report to Gmail (HTML + plain text fallback)
2) Saved report artifact:
   - `reports/YYYY-MM-DD.md` (and optional `.html`)
3) SQLite database:
   - `jobs.db`
4) Logs:
   - `logs/run_YYYY-MM-DD.log`

---

## 5) Required Functional Behavior

### 5.1 Workflow Overview (LangGraph)
Create a workflow with these nodes:

1) **LoadCriteria**
   - Read criteria.md
   - Parse into structured criteria (Pydantic model)
   - Maintain both raw text (for LLM prompts) and structured form (for hard filters)

2) **LoadSources**
   - Read sources.yaml
   - Build list of enabled sources to query

3) **FetchJobs**
   - For each enabled source:
     - fetch jobs
     - normalize into a common Job schema
   - Must capture source metadata and retrieval timestamp

4) **NormalizeAndParseDates**
   - Normalize fields:
     - title, company, url, location, remote flag, employment type, salary text, posted_date, description
   - Parse dates into ISO format; if date missing, mark as unknown

5) **HardFilter**
   - Apply deterministic filters:
     - must be fully remote (as best as can be inferred)
     - exclude hourly/contract/part-time/1099
     - exclude jobs older than `posted_within_days`
     - salary gating:
       - if salary is present: must be in range
       - if salary is absent: keep but add “missing_salary” flag
     - keyword gating:
       - must match at least N keywords in title/description (configurable)

6) **SemanticScore (LLM)**
   - For each remaining job:
     - call local LLM (Ollama) to score relevance and fit
     - return STRICT JSON in a predefined schema:
       - `is_match` bool
       - `score` int 1-10
       - `reasons` list of strings
       - `flags` list of strings (e.g., missing_salary, unknown_company)
   - Validate with Pydantic.
   - If invalid, run a repair prompt once; if still invalid, discard or mark uncertain.

7) **CompanyReputationCheck (Optional / Heuristic)**
   - Determine “reputed company” via heuristics:
     - if company appears in a known allowlist OR
     - if web check indicates established company (signals from search snippets)
   - Must be free and local:
     - use SearXNG to query “{company} funding”, “{company} public company”, “{company} series B”
   - Output a `reputation_score` and evidence snippets.
   - Do not over-rely on LLM; keep it heuristic + evidence-based.

8) **DeduplicateAndPersist**
   - Dedupe:
     - exact dedupe: URL
     - fuzzy dedupe: normalized (company+title) and/or embedding similarity threshold
   - Persist new matches to SQLite:
     - store job, score, flags, reasons, run timestamp, source

9) **GenerateReport**
   - Create a ranked list by:
     - match score (desc)
     - reputation score (desc)
     - posted date (desc)
   - Include:
     - title, company, location/remote, posted date
     - salary if available
     - link (clickable)
     - reasons (bullet list)
     - flags
   - Save to `reports/YYYY-MM-DD.md` and optional `reports/YYYY-MM-DD.html`.

10) **SendEmail**
   - Send Gmail email via SMTP
   - Subject: `Daily Agentic AI Job Matches — YYYY-MM-DD`
   - Body: HTML (and text fallback)

---

## 6) Strict Data Models (Pydantic)

### 6.1 Criteria Model
Must include:
- `fully_remote: bool`
- `full_time_only: bool`
- `avoid_hourly: bool`
- `avoid_contract: bool`
- `posted_within_days: int`
- `min_salary: int | None`
- `max_salary: int | None`
- `keywords: list[str]`
- `seniority: list[str]`
- `exclude_keywords: list[str]`
- `min_llm_score: int` (default 7)
- `max_results_per_email: int` (default 30)

### 6.2 Job Model
Must include:
- `job_id` (computed stable id)
- `title`
- `company`
- `url`
- `source`
- `posted_date` (ISO string or None)
- `employment_type` (best guess)
- `remote_type` (remote/hybrid/onsite/unknown)
- `salary_text` (raw)
- `salary_min`, `salary_max` (optional parsed)
- `location` (optional)
- `description` (clean text)
- `raw_description_html` (optional)
- `flags` (list)
- `hard_filter_passed` (bool)

### 6.3 LLM Scoring Output Model
Strict JSON fields:
- `is_match: bool`
- `score: int` (1-10)
- `reasons: list[str]` (<= 6 items)
- `flags: list[str]`
- `confidence: str` one of `["low","medium","high"]`

---

## 7) Email Formatting Requirements

Email must be readable and skimmable:
- Top summary:
  - total fetched
  - total hard-filter passed
  - total semantic matches
- Job list:
  - numbered items
  - clickable link
  - compact “why matched” bullets
- Separate section for “Borderline” jobs (score = min_llm_score - 1) if enabled.
- Include “Uncertain / Missing salary” note.

---

## 8) Scheduling Requirements

- Must provide a simple way to schedule:
  - `cron` entry
  - optional `launchd` plist
- Default schedule: every day at 7:00 AM local time.
- Must provide a manual run mode:
  - `python main.py --mode daily`
  - `python main.py --mode weekly`

---

## 9) Local Setup Requirements

The implementation must include end-to-end setup instructions:

1) Install dependencies:
   - Homebrew
   - Python 3.11+
   - uv (recommended)
   - Docker Desktop
   - Ollama

2) Start local services:
   - Ollama server
   - (optional) Docker compose for SearXNG + Qdrant

3) Configure `.env`:
   - OLLAMA
   - Gmail SMTP creds
   - DB path
   - SearXNG URL
   - Vector DB selection and config

4) Run:
   - `python main.py --mode daily`

---

## 10) Implementation Guidance (Important)

### 10.1 Avoid Over-Agenting
This is a deterministic pipeline with an LLM scoring step.
Use LangGraph for orchestration, but keep most steps deterministic.

### 10.2 Do Not Scrape Restricted Sites
Do not scrape LinkedIn pages or any site that blocks bots or forbids scraping in ToS.
Prefer RSS and official job boards.

### 10.3 Defensive Engineering
- Handle timeouts, retries, and partial failures.
- If a source fails, log it and continue.
- If LLM output fails, repair once then fall back.

### 10.4 Deduplication Strategy
- primary key: URL
- fallback key: normalized `company|title`
- optional: embedding similarity threshold (e.g., cosine >= 0.92)

---

## 11) Deliverables (What the code generator must output)

Generate a repo with:

1) `README.md` (setup + run + schedule instructions)
2) `instructions.md` (this file)
3) `criteria.md` (example)
4) `sources.yaml` (example)
5) `.env.example` (no secrets)
6) `docker-compose.yml` (SearXNG + optional Qdrant)
7) Python package:
   - `src/graph.py` (LangGraph workflow)
   - `src/agents/` (criteria parsing, scoring, reputation)
   - `src/tools/` (sources, searx tool, HTML cleaning)
   - `src/storage/` (sqlite schema + repo)
   - `src/report/` (renderers + gmail sender)
   - `main.py` CLI entrypoint
8) `tests/` minimal tests:
   - criteria parsing test
   - LLM output schema validation test (mock)
   - dedupe logic test

---

## 12) Acceptance Criteria (Definition of Done)

A run is considered successful if:

1) It fetches from at least 2 sources (RemoteOK + WWR) without crashing.
2) It correctly filters:
   - no hourly/contract/part-time jobs in the final “matches”
   - only jobs within time window appear in matches (when date is present)
3) It generates:
   - `reports/YYYY-MM-DD.md`
4) It sends an email to Gmail with the report.
5) It stores jobs in SQLite and does not resend the same job on subsequent runs.
6) It runs end-to-end locally on macOS with no paid dependencies.

---

## 13) Configuration Defaults (Set These)

- Daily run window: `posted_within_days = 1`
- Weekly window: `posted_within_days = 7`
- Minimum match threshold: `min_llm_score = 7`
- Max jobs per email: `30`
- Ollama model: `llama3`
- SearXNG: enabled (optional)
- Vector DB: Chroma embedded (default) or Qdrant Docker (optional)

---

## 14) Future Enhancements (Optional, Not Required Now)

- Preference learning:
  - store which links I clicked / applied
  - personalize ranking over time
- Add more sources:
  - curated list of AI companies’ Greenhouse/Lever feeds
- “Company quality” enrichment:
  - add evidence links from SearXNG
  - compute a reputation score with clear heuristics
- Add a lightweight local UI:
  - Streamlit dashboard to view matches and history

---

## 15) Final Instruction to the Code-Generating LLM

Generate the full repository described above, with code and step-by-step setup instructions.
Keep implementation:
- deterministic where possible
- robust to bad data
- strictly validated using Pydantic
- fully local and free
- compliant with data source constraints
