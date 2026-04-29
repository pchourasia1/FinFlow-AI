# FinFlow-AI Architecture

FinFlow-AI is a conversational assistant for a fictional digital bank. A user asks a question in plain English; an LLM agent decides whether it needs the user's transactions, their account balances, or the bank's policy text, calls the matching backend service, and streams back a natural-language answer.

The system is a browser chat UI (served by the agent service; a fuller Next.js frontend is planned) plus three independent FastAPI services that talk over HTTP and share one Supabase PostgreSQL database with the pgvector extension.

## System overview

![FinFlow-AI architecture](images/Architecture.png)

| Component | Port | Code | Responsibility |
|---|---|---|---|
| Chat UI | 8000 | `backend/agent-service/app/static/index.html` | Chat page served at `/` by the Agent Service; no build step or Node.js needed |
| Next.js frontend (planned) | 3000 | `frontend/` | Future dashboards; calls the agent for chat and the data services directly. Only the API client exists so far |
| Agent Service API | 8000 | `backend/agent-service` | Chat endpoint, LangGraph orchestrator, tool execution, response streaming |
| Vectorstore Service | 8001 | `backend/vectorstore-service` | Embeds queries and runs semantic search over policy chunks |
| Database Service | 8002 | `backend/database-service` | Spending analytics, balances, admin statistics |
| Supabase PostgreSQL | n/a | `data/sql/schema.sql` | `users`, `accounts`, `transactions`, `policy_document` |

There are two paths through the system:

- **Chat path:** frontend → Agent Service → Orchestrator → one or more tools → Database or Vectorstore Service → Supabase. The orchestrator also calls the Google Gemini API (`gemini-2.5-flash-lite` by default; set `GEMINI_MODEL` in `backend/.env` to change it) to choose tools and write answers.
- **Direct path:** the frontend calls the Database and Vectorstore services itself, for screens that need raw data rather than a chat answer (for example the `/admin` endpoints).

Each service is started with `uvicorn app.main:app --port <port>` from its own directory. `run_services.ps1` (Windows) or `run_services.sh` (Linux) starts all three.

## Agent service

### API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Chat UI (single HTML page, no external dependencies) |
| `POST` | `/chat/stream` | Body `{query, user_id, thread_id?}`; returns a Server-Sent Events stream |
| `GET` | `/info` | `{model, today}`: the Gemini model in use and the effective "today" date, shown in the chat UI header |
| `GET` | `/health` | Health check |

CORS allows `http://localhost:3000`.

### Orchestrator

`app/tools/orchestrator.py` builds a LangGraph state machine around Gemini (model from `GEMINI_MODEL`, default `gemini-2.5-flash-lite`; `temperature=0.25`) with three tools bound to it. The tool descriptions, which steer the LLM's tool choice, live in `app/llm/prompts/orchestration.json`.

```text
START ──► router ──(tool calls?)──► tools ──► respond ──► END
                         │                       ▲
                         └──────── no ───────────┘
```

1. **router** sends the conversation, plus a system message carrying the `user_id`, to Gemini. Gemini either answers directly or emits one or more tool calls.
2. **tools** runs all requested tool calls in parallel (`asyncio.gather`). It injects `user_id` from state into the account tools, so the LLM never has to supply it.
3. **respond** makes a second Gemini call to turn the tool output into a natural-language answer (skipped if no tools ran).

Conversation memory uses LangGraph's in-process `MemorySaver`, keyed by `thread_id`.

### Tools

| Tool (diagram name) | Name seen by the LLM | Wrapper | Calls |
|---|---|---|---|
| SQL Analytics Tool | `analyze_spending` | `sql_analytics_tool.py` | `POST :8002/analytics/spending/` |
| Balance Tool | `get_account_balances` | `balance_query_tool.py` | `POST :8002/analytics/balance/` |
| RAG Tool | `search_policy_documents` | `rag_tool.py` | `POST :8001/search/rag-context` |

`analyze_spending` first normalizes free-text categories (e.g. "coffee") to the dataset's category names via `app/utils/category_normalizer.py`.

### Chat UI

`app/static/index.html` is a self-contained page (inline CSS and JavaScript, no build step) served at `/`:

- Sends each question to `/chat/stream` and renders the SSE events as they arrive: a status line while tools run ("Checking spending analysis"), then the answer as Markdown.
- Keeps one `thread_id` per conversation, so follow-ups like "and the month before that?" use the earlier context. **New chat** starts a fresh thread.
- The **User ID** field picks which synthetic customer to chat as; changing it starts a new conversation.
- A **How I got this** panel under each answer lists the tools used, their arguments, and the matched policy sections from the `tool_execution` event.
- The header shows the model and "today" date from `/info`, and a dot that reflects `/health`.

### Stream events

Each SSE message is `data: <json>`. Event types, in typical order:

| `type` | Payload | Emitted by |
|---|---|---|
| `thinking` | `content`: any text the LLM produced while routing | router |
| `tool_call` | `tools`: names of the tools chosen | router |
| `tool_execution` | `metadata`: tool args, top RAG hits, balance rows | tools |
| `response` | `content`: a 50-character chunk of the final answer | respond |
| `done` | n/a | chat route |
| `error` | `content`: error message | chat route |

## Database service

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/analytics/spending/` | Spending totals filtered by category, date range, account, or account type; grouped by category, month, or merchant (top 50) |
| `POST` | `/analytics/balance/` | A user's account balances, optionally by account ID or type |
| `GET` | `/admin/stats` | Counts of users, accounts, and transactions; total balance |
| `GET` | `/admin/users` | Paginated user list |
| `GET` | `/admin/users/{user_id}/details` | One user's profile, accounts, and recent transactions |
| `GET` | `/admin/trends` | Transaction volume over time |
| `GET` | `/admin/geographic` | Users by state |
| `GET` | `/admin/risk-scores` | Risk-score distribution |

Business logic lives in `app/services/` (`spending_service.py`, `balance_service.py`, `admin_service.py`). Queries are parameterized SQL over a `psycopg2` `ThreadedConnectionPool` (1 to 10 connections) defined in `app/config/database.py`. CORS allows `http://localhost:3000`.

## Vectorstore service

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/search/` | Matching policy chunks with similarity scores (up to 5) |
| `POST` | `/search/rag-context` | Top chunks joined into one context string for the LLM (up to 3) |

A query is embedded with `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions, mean-pooled, L2-normalized; `app/vectorstore/embeddings.py`) and compared with stored chunks using pgvector's cosine-distance operator `<=>`. Only chunks with a distance below `0.7` are returned.

The model is downloaded from Hugging Face on first start and runs on CPU.

## Data model

![Database tables](images/Tables_Schema.png)

```text
users 1 ──── * accounts 1 ──── * transactions          policy_document (standalone)
```

| Table | Columns | Notes |
|---|---|---|
| `users` | `user_id` int8 PK, `name`, `email`, `phone`, `address`, `city`, `state` (text), `risk_score` float8 | `risk_score` ranges 0.1 to 1.0 in the synthetic data |
| `accounts` | `account_id` int8 PK, `user_id` int8 → `users`, `balance` float8, `type` text | `type` is `checking` or `savings` |
| `transactions` | `transaction_id` int8 PK, `account_id` int4 → `accounts`, `amount` numeric, `category` varchar, `merchant` varchar, `timestamp`, `running_balance` numeric, `created_at` | Indexed on `account_id`, `timestamp`, and both together |
| `policy_document` | `id` int4 PK, `title` varchar, `content` text, `embedding` vector(384), `metadata` jsonb, `created_at` | One row per section of the policy document |

`data/sql/schema.sql` creates all four tables and enables pgvector. It is safe to re-run.

## Data pipeline

All data is synthetic. Scripts live in `data/scripts/` and are run from the project root.

```text
data/sql/schema.sql ──► empty tables + pgvector

setup_data.py ──► data/synthetic/{users,accounts,transactions}.csv
                        │              │              │
                        ▼              ▼              ▼
                  (CSV import or \copy)           load_transactions.py ──► transactions

data/policy/policy_doc.md ──► vectorize.py ──► policy_document
```

1. **`schema.sql`**: run once in the Supabase SQL editor.
2. **`setup_data.py`** (`--users`, default 10,000; `--seed`, default 53; `--as-of YYYY-MM-DD` to generate the year ending on that date, matched by `FINFLOW_AS_OF_DATE` in `backend/.env`) generates users with Faker, 1 to 3 accounts each, and transactions drawn from fixed category and merchant lists.
3. **Import `users.csv`, then `accounts.csv`**, through Supabase's table editor or psql `\copy` (commands are at the end of `schema.sql`).
4. **`load_transactions.py`** checks that `accounts` has rows, then bulk-loads `transactions.csv` with `COPY`, falling back to batched inserts of 1,000.
5. **`vectorize.py`** splits `policy_doc.md` into chunks by Markdown section, embeds each with MiniLM, and inserts them into `policy_document`. Re-run it whenever the policy document changes.
6. **`query_test.py`** is a manual check that embeds a sample question and searches the stored chunks.

## Configuration

| File | Used by | Contents |
|---|---|---|
| `backend/.env` (template: `backend/.env.example`) | All three services | `GOOGLE_API_KEY`, `PROJECT_ID`, `PSWD`, optional `SUPABASE_DB_URL`, `GEMINI_MODEL`, `FINFLOW_AS_OF_DATE`, `DATABASE_SERVICE_URL`, `VECTORSTORE_SERVICE_URL` |
| `data/scripts/supabase_config.yaml` (template: `supabase_config.example.yaml`) | `load_transactions.py`, `vectorize.py` | `project_id`, `pwd` |
| `NEXT_PUBLIC_AGENT_SERVICE_URL` | Frontend | Agent service URL (default `http://localhost:8000`) |

When `SUPABASE_DB_URL` is unset, the services build a connection string for the Supabase pooler in `aws-1-us-east-2`; set it explicitly for projects in other regions. The data scripts do not read `.env`, so for them `SUPABASE_DB_URL` must be exported in the shell.

Both config files hold secrets and are excluded by `.gitignore`.

## Current state and limitations

- **Next.js frontend not built.** Chat works through the page at `http://localhost:8000`. `frontend/` holds only the API client (`lib/api/agent.ts`) and shared types (`lib/types.ts`) for a future Next.js app.
- **Vectorstore has no CORS.** The diagram shows the frontend calling the Vectorstore Service directly, but that service does not allow browser requests from `localhost:3000` yet. The Database Service does.
- **No authentication.** `user_id` is supplied by the client, so any caller can ask about any user.
- **In-memory chat history.** Conversations are lost when the agent service restarts and are not shared across instances.
- **Working-directory dependency.** The agent loads `app/llm/prompts/orchestration.json` by relative path, so it must be started from `backend/agent-service`.
- **Simulated streaming.** The final answer is generated in full, then sent in 50-character chunks rather than streamed token by token.
- **Duplicate backend calls.** For the RAG and balance tools, the tools node calls the backend once to capture metadata for the stream and again to get the result.
- **Floating-point money.** `accounts.balance` is `float8`, which can introduce rounding errors; `transactions` uses exact `numeric`.
- **Two launchers.** `run_services.ps1` (Windows) and `run_services.sh` (Linux; needs `hostnamectl`, `boxes`, `lolcat`, `pkill`). See [WINDOWS_SETUP.md](WINDOWS_SETUP.md).
