# FinFlow-AI Architecture

FinFlow-AI is a conversational assistant for a fictional digital bank. A user asks a question in plain English; an LLM agent decides whether it needs the user's transactions, their account balances, or the bank's policy text, calls the matching backend service, and streams back a natural-language answer.

The system is a browser chat UI plus three independent FastAPI services that talk over HTTP and share one Supabase PostgreSQL database with the pgvector extension.

## System overview

![FinFlow-AI architecture](images/Architecture.png)

| Component | Port | Code | Responsibility |
|---|---|---|---|
| Chat UI | 8000 | `backend/agent-service/app/static/index.html` | Single-page chat interface served at `/` by the Agent Service |
| Agent Service | 8000 | `backend/agent-service` | Chat API, LangGraph orchestrator, tool execution, response streaming |
| Vectorstore Service | 8001 | `backend/vectorstore-service` | Embeds queries and runs semantic search over policy sections |
| Database Service | 8002 | `backend/database-service` | Spending analytics, balances, admin statistics |
| Supabase PostgreSQL | n/a | `data/sql/schema.sql` | `users`, `accounts`, `transactions`, `policy_document` |
| Google Gemini API | n/a | external | `gemini-3.5-flash-lite` chooses tools and writes answers |

The diagram shows the target design, in which a Streamlit frontend on port 8501 calls the Agent Service for chat and the Database and Vectorstore services directly for dashboards. Today the chat page is served by the Agent Service itself. `frontend/` holds an older TypeScript API client that the Streamlit app will replace.

## Life of a question

What happens when a user types "How much did I spend on groceries last month?":

1. **Chat UI** posts `{query, user_id, thread_id}` to `POST /chat/stream` and starts reading the Server-Sent Events (SSE) response.
2. **router** (orchestrator): Gemini receives the conversation so far plus a system message with the user's ID and today's date. It replies with a tool call: `analyze_spending(category="Groceries", start_date="2026-03-01", end_date="2026-03-31")`. The UI shows "Checking spending analysis".
3. **tools**: the agent injects the user's ID, normalizes the category name, and calls `POST :8002/analytics/spending/`. The Database Service runs a parameterized SQL query and returns totals. The UI receives the tool's arguments for its **How I got this** panel.
4. **respond**: Gemini gets the tool output and writes a plain-English answer.
5. The answer streams back as `response` events, and the UI renders it as Markdown. The conversation is saved under `thread_id`, so a follow-up like "and the month before?" has context.

## Chat UI

`backend/agent-service/app/static/index.html` is one self-contained file (inline CSS and JavaScript, no libraries, no build step).

| Feature | How it works |
|---|---|
| Streaming answers | Reads the SSE stream with `fetch` and a `ReadableStream`; shows a status line while tools run, then renders the answer as Markdown |
| Follow-up questions | Sends one `thread_id` per conversation; **New chat** starts a new one |
| User selector | The **User ID** field (1 to 10000) sets `user_id`; changing it starts a new conversation. Remembered in `localStorage` |
| How I got this | Built from the `tool_execution` event: tool name, arguments (dates, category, grouping), and matched policy sections with a match percentage |
| Header context | Model and "today" date from `GET /info`; a status dot from `GET /health` |
| Suggestions | Example questions for balances, spending, and policies on an empty chat |
| Stop | Cancels the in-flight request with an `AbortController` |
| Themes | Follows the system light or dark setting |

## Agent service

### API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Chat UI |
| `POST` | `/chat/stream` | Body `{query, user_id, thread_id?}`; returns an SSE stream |
| `GET` | `/info` | `{model, today}`: the Gemini model in use and the effective "today" date |
| `GET` | `/health` | Health check |

CORS allows `http://localhost:3000`, left over from an earlier frontend plan. The planned Streamlit app calls the services from Python on the server, so it doesn't need CORS.

### Orchestrator

`app/tools/orchestrator.py` builds a LangGraph state machine around Gemini (`GEMINI_MODEL`, default `gemini-3.5-flash-lite`) with three tools bound to it. The tool descriptions, which steer Gemini's tool choice, live in `app/llm/prompts/orchestration.json`.

```text
START ──► router ──(tool calls?)──► tools ──► respond ──► END
                         │                       ▲
                         └──────── no ───────────┘
```

1. **router** sends the conversation to Gemini with a system prompt containing the user's ID and today's date (`FINFLOW_AS_OF_DATE` if set), so relative dates like "last month" resolve correctly.
2. **tools** runs all requested tool calls in parallel (`asyncio.gather`). It injects `user_id` from state into the account tools and records each call's details as `tool_metadata` for the UI.
3. **respond** makes a second Gemini call to turn the tool output into an answer (skipped if no tools ran).

Conversation memory uses LangGraph's in-process `MemorySaver`, keyed by `thread_id`.

Gemini 3.x returns message content as a list of blocks rather than a string; `_message_text()` extracts the text before it is streamed.

The file can also be run directly for a quick test: `python app/tools/orchestrator.py "What is my balance?" --user 6`, from `backend/agent-service`.

### Tools

| Tool (diagram name) | Name seen by Gemini | Wrapper | Calls |
|---|---|---|---|
| SQL Analytics Tool | `analyze_spending` | `sql_analytics_tool.py` | `POST :8002/analytics/spending/` |
| Balance Tool | `get_account_balances` | `balance_query_tool.py` | `POST :8002/analytics/balance/` |
| RAG Tool | `search_policy_documents` | `rag_tool.py` | `POST :8001/search/rag-context` |

- `analyze_spending` normalizes free-text categories (e.g. "coffee" → "Coffee Shops") with `app/utils/category_normalizer.py`.
- Month breakdowns are sorted by date and labeled like "July 2026" before Gemini sees them; with raw timestamps sorted by amount, Gemini mislabeled months.

### Stream events

Each SSE message is `data: <json>`. Event types, in typical order:

| `type` | Payload | Emitted by |
|---|---|---|
| `thinking` | `content`: any text Gemini produced while routing | router |
| `tool_call` | `tools`: names of the tools chosen | router |
| `tool_execution` | `metadata`: per tool, `tool_name`, `tool_args`, and top RAG hits or balance rows | tools |
| `response` | `content`: a 50-character chunk of the final answer | respond |
| `done` | n/a | chat route |
| `error` | `content`: error message | chat route |

## Database service

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/analytics/spending/` | Spending totals filtered by category, date range, account, or account type; grouped by category, month, merchant, or account (top 50) |
| `POST` | `/analytics/balance/` | A user's account balances, optionally by account ID or type |
| `GET` | `/admin/stats` | Counts of users, accounts, and transactions; total balance |
| `GET` | `/admin/users` | Paginated user list |
| `GET` | `/admin/users/{user_id}/details` | One user's profile, accounts, and recent transactions |
| `GET` | `/admin/trends` | Transaction volume over time |
| `GET` | `/admin/geographic` | Users by state |
| `GET` | `/admin/risk-scores` | Risk-score distribution |

Business logic lives in `app/services/`. Queries are parameterized SQL over a `psycopg2` `ThreadedConnectionPool` (1 to 10 connections) in `app/config/database.py`. CORS allows `http://localhost:3000` (not needed by the planned Streamlit app, which calls the API server-side).

**Date handling in spending queries:**

- With no dates, the range is the 30 days ending "now", from `app/config/clock.py` (see [Simulated "today"](#simulated-today)).
- A date-only `end_date` such as `2026-09-30` includes that whole day.
- Ranges are capped at one year.
- Only spending (negative amounts) is counted.

## Vectorstore service

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/search/` | Matching policy sections with similarity scores (up to 5) |
| `POST` | `/search/rag-context` | Top sections joined into one context string for Gemini (up to 3) |

A query is embedded with `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions, mean-pooled, L2-normalized; `app/vectorstore/embeddings.py`) and compared with stored sections using pgvector's cosine-distance operator `<=>`. Only sections with a distance below `0.7` are returned. The model is downloaded from Hugging Face on first start and runs on CPU.

## Data model

![Database tables](images/Tables_Schema.png)

```text
users 1 ──── * accounts 1 ──── * transactions          policy_document (standalone)
```

| Table | Columns | Notes |
|---|---|---|
| `users` | `user_id` int8 PK, `name`, `email`, `phone`, `address`, `city`, `state` (text), `risk_score` float8 | 10,000 rows; `risk_score` from 0.1 to 1.0 |
| `accounts` | `account_id` int8 PK, `user_id` int8 → `users`, `balance` float8, `type` text | About 20,000 rows; `type` is `checking` or `savings` |
| `transactions` | `transaction_id` int8 PK, `account_id` int4 → `accounts`, `amount` numeric, `category` varchar, `merchant` varchar, `timestamp`, `running_balance` numeric, `created_at` | About 2.09 million rows; indexed on `account_id`, `timestamp`, and both together |
| `policy_document` | `id` int4 PK, `title` varchar, `content` text, `embedding` vector(384), `metadata` jsonb, `created_at` | 23 rows, one per section of the policy document |

`data/sql/schema.sql` creates all four tables and enables pgvector; it is safe to re-run. The full dataset uses about 400 MB, inside Supabase's 500 MB free tier.

## Data pipeline

All data is synthetic. Scripts live in `data/scripts/` and are run from the project root.

```text
data/sql/schema.sql ──► empty tables + pgvector

setup_data.py --as-of ──► data/synthetic/{users,accounts,transactions}.csv
                              │              │              │
                              ▼              ▼              ▼
                        (CSV import or \copy)           load_transactions.py ──► transactions

data/policy/policy_doc.md ──► vectorize.py ──► policy_document
```

1. **`schema.sql`**: run once in the Supabase SQL editor.
2. **`setup_data.py`** generates users with Faker, 1 to 3 accounts each, and 10 to 200 transactions per account from fixed category and merchant lists. Options: `--users` (default 10,000), `--seed` (default 53), `--as-of YYYY-MM-DD` (generate the year ending on that date; default today).
3. **Import `users.csv`, then `accounts.csv`**, with Supabase's table editor or psql `\copy` (commands are at the end of `schema.sql`).
4. **`load_transactions.py`** checks that `accounts` has rows, then bulk-loads `transactions.csv` with `COPY`, falling back to batched inserts.
5. **`vectorize.py`** splits `policy_doc.md` into sections, embeds each with MiniLM, and inserts them into `policy_document`. Re-run it whenever the policy document changes.
6. **`query_test.py`** is a manual check that embeds a sample question and searches the stored sections.

To reset the data, empty the tables (`TRUNCATE transactions, accounts, users, policy_document RESTART IDENTITY`) and repeat steps 2 to 5.

## Simulated "today"

Synthetic data covers one fixed year, so questions like "last month" only work if the system agrees on what "today" is. One setting keeps everything consistent:

| Piece | Uses the date for |
|---|---|
| `setup_data.py --as-of 2026-04-01` | Generates transactions from 2025-04-02 to 2026-04-01 |
| `FINFLOW_AS_OF_DATE=2026-04-01` in `backend/.env` | Database Service: default date ranges (`app/config/clock.py`) |
| | Agent Service: today's date in Gemini's system prompt, and in `/info` |

Leave `FINFLOW_AS_OF_DATE` unset to use the real date.

## Configuration

| File | Used by | Contents |
|---|---|---|
| `backend/.env` (template `backend/.env.example`) | All three services | `GOOGLE_API_KEY`, `PROJECT_ID`, `PSWD`; optional `SUPABASE_DB_URL`, `GEMINI_MODEL`, `FINFLOW_AS_OF_DATE`; `DATABASE_SERVICE_URL`, `VECTORSTORE_SERVICE_URL` |
| `data/scripts/supabase_config.yaml` (template `supabase_config.example.yaml`) | `load_transactions.py`, `vectorize.py` | `project_id`, `pwd` |

- When `SUPABASE_DB_URL` is unset, the services build a connection string for the Supabase pooler in `aws-1-us-east-2`. Set it for projects in other regions, or when the password contains characters that must be URL-encoded (`@` → `%40`).
- The data scripts do not read `.env`, so for them `SUPABASE_DB_URL` must be exported in the shell.
- Both real config files hold secrets and are excluded by `.gitignore`; only the templates are committed.

## Running the services

| Launcher | Platform | Behavior |
|---|---|---|
| `run_services.ps1` / `run_services.bat` | Windows | Checks the venv, `.env`, and free ports; starts all three services on `127.0.0.1`; waits for `/health`; logs to `logs\`; `Ctrl+C` stops everything; `-Stop` cleans up leftovers. Options: `-NewWindows`, `-NoReload`, `-TimeoutSec` |
| `run_services.sh` | Linux | Starts all three services; needs `hostnamectl`, `boxes`, `lolcat`, and `pkill` |

Each service can also be started by hand with `python -m uvicorn app.main:app --port <port>` from its own directory. See [WINDOWS_SETUP.md](WINDOWS_SETUP.md) for the Windows guide.

## Current limitations

- **No authentication.** The client chooses `user_id`, so any caller can ask about any user.
- **In-memory chat history.** Conversations are lost when the Agent Service restarts and are not shared across instances.
- **Simulated streaming.** The final answer is generated in full, then sent in 50-character chunks rather than token by token.
- **Duplicate backend calls.** For the RAG and balance tools, the tools step calls the backend once for the UI's metadata and again for the result.
- **Working-directory dependency.** The agent loads `app/llm/prompts/orchestration.json` by relative path, so it must be started from `backend/agent-service`.
- **Floating-point money.** `accounts.balance` is `float8`, which can introduce rounding errors; `transactions` uses exact `numeric`.
- **Auto-reload in synced folders.** uvicorn's `--reload` may miss file changes in OneDrive-synced folders; restart the services after editing code.
