# FinFlow: AI-Powered Digital Banking Platform (In-Progress)

A microservices-based digital banking platform with a conversational AI assistant. Users ask questions in plain English, such as "How much did I spend on groceries last month?", "What's my savings balance?" or "What's the international transfer fee?", and the assistant answers from their own account data or from the bank's policy documents.

Built with FastAPI, LangGraph, Google Gemini, and RAG over pgvector. All data is synthetic, and the bank is fictional.

## Architecture

<p align="center">
    <img src="docs/images/Architecture.png" alt="FinFlow-AI architecture" width="500"/>
</p>

| Component | Port | Role |
|---|---|---|
| **Chat UI** | 8000 | Browser chat page served by the Agent Service (a Next.js frontend on port 3000 is planned) |
| **Agent Service** | 8000 | LangGraph orchestrator: Gemini picks a tool, calls it, and writes the answer |
| **Vectorstore Service** | 8001 | Semantic search over policy documents (RAG) |
| **Database Service** | 8002 | Spending analytics, balances, and admin statistics |
| **Supabase PostgreSQL** | n/a | Users, accounts, transactions, and policy embeddings |

The orchestrator has three tools: **SQL Analytics** (spending by category, date, merchant, or account), **Balance** (account balances), and **RAG** (policy search).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the request flow, API endpoints, stream events, data model, and known limitations.

## Features

- **Conversational banking assistant** powered by Google Gemini 2.5 Flash Lite
- **Intelligent query routing** with a LangGraph state machine that can call several tools in parallel
- **Spending analytics** filtered by category, date range, account type, and merchant
- **Balance queries** across checking and savings accounts
- **RAG policy search** using pgvector and `all-MiniLM-L6-v2` embeddings
- **Streaming responses** over Server-Sent Events
- **Synthetic data generation** for 10,000+ users with realistic transaction patterns

## Data Model

<p align="center">
    <img src="docs/images/Tables_Schema.png" alt="Database tables" width="700"/>
</p>

`users` have many `accounts`, which have many `transactions`. `policy_document` holds the embedded policy chunks. The full schema is in [`data/sql/schema.sql`](data/sql/schema.sql).

## Tech Stack

| Area | Technologies |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn, httpx |
| AI / ML | LangChain, LangGraph, Google Gemini 2.5 Flash Lite, Hugging Face Transformers, PyTorch, sentence-transformers (all-MiniLM-L6-v2) |
| Database | PostgreSQL (Supabase), pgvector, psycopg2 |
| Frontend | Next.js, TypeScript |
| Data | Faker, python-dotenv, PyYAML |

## Getting Started

### Prerequisites

- Python 3.12 (not Anaconda on Windows)
- A [Supabase](https://supabase.com) project (or any PostgreSQL with pgvector)
- A [Google Gemini API key](https://aistudio.google.com/apikey)

### 1. Install dependencies

```bash
python -m venv venv
source venv/Scripts/activate      # Windows (Git Bash); PowerShell: venv\Scripts\Activate.ps1
# source venv/bin/activate        # macOS / Linux
pip install -r requirements.txt
```

On Windows, create the venv from a python.org or `uv` Python rather than Anaconda; Anaconda's old Visual C++ runtime breaks PyTorch.

### 2. Configure

Copy the templates, then fill in your values:

```bash
cp backend/.env.example backend/.env
cp data/scripts/supabase_config.example.yaml data/scripts/supabase_config.yaml
```

- **`backend/.env`:** set `GOOGLE_API_KEY`, `PROJECT_ID`, and `PSWD`. If your Supabase project is not in `us-east-2`, also set `SUPABASE_DB_URL` to the "Transaction pooler" URI from the Supabase dashboard. Optional: `GEMINI_MODEL` and `FINFLOW_AS_OF_DATE` (see the comments in the file).
- **`data/scripts/supabase_config.yaml`:** set `project_id` and `pwd`.

Both copies are git-ignored; only the templates are committed.

### 3. Set up the database

Run these from the project root:

```bash
# Create tables and enable pgvector: paste data/sql/schema.sql into the Supabase SQL editor

python data/scripts/setup_data.py            # generate synthetic CSVs
# Import data/synthetic/users.csv, then accounts.csv (Supabase table editor or psql \copy)
python data/scripts/load_transactions.py     # load transactions
python data/scripts/vectorize.py             # embed the policy document
```

### 4. Run the services

| Platform | Start | Stop |
|---|---|---|
| **Windows** | `.\run_services.ps1` (or double-click `run_services.bat`) | `Ctrl+C`, or `.\run_services.ps1 -Stop` |
| **Linux** | `./run_services.sh` | `Ctrl+C` |

The Windows launcher starts all three services, waits for their health checks, and writes logs to `logs\`. See **[docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md)** for the full Windows walkthrough and troubleshooting.

To run a service by hand instead, start it from its own folder:

```bash
cd backend/agent-service && python -m uvicorn app.main:app --port 8000 --reload
# vectorstore-service: --port 8001   database-service: --port 8002
```

### 5. Try it

Open **http://localhost:8000** in your browser and ask a question. The chat page streams answers, keeps the conversation going across follow-up questions, and has a **How I got this** panel showing which tool was used and which policy sections matched. Change **User ID** in the header to chat as a different customer (1 to 10000).

<p align="center">
    <img src="docs/images/chat-ui.png" alt="FinFlow chat UI" width="700"/>
</p>

Or from the command line:

```bash
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "How much did I spend on coffee last month?", "user_id": 6}'
```

Interactive API docs are at `http://localhost:8000/docs`, `:8001/docs`, and `:8002/docs`.

## Project Structure

```text
FinFlow-AI/
├── backend/
│   ├── .env                     # Service configuration (git-ignored)
│   ├── agent-service/           # AI agent orchestration + chat UI (port 8000)
│   │   ├── app/
│   │   │   ├── api/routes/      # /chat/stream endpoint
│   │   │   ├── tools/           # Orchestrator + RAG, SQL analytics, balance tools
│   │   │   ├── llm/prompts/     # Tool descriptions for the LLM
│   │   │   └── utils/           # Category normalizer
│   │   └── tests/integration/   # Tool integration tests
│   ├── database-service/        # Analytics, balance, and admin APIs (port 8002)
│   │   └── app/
│   │       ├── api/routes/
│   │       ├── services/        # SQL query logic
│   │       └── config/          # Connection pool
│   └── vectorstore-service/     # Vector search (port 8001)
│       └── app/
│           ├── api/routes/
│           ├── vectorstore/     # Embeddings and similarity search
│           └── config/
├── data/
│   ├── policy/                  # Fictional bank policy document
│   ├── scripts/                 # Data generation, loading, and vectorization
│   ├── sql/                     # Database schema
│   └── synthetic/               # Generated CSVs (git-ignored)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── WINDOWS_SETUP.md
│   └── images/                  # Architecture and schema diagrams
├── frontend/                    # Next.js API client (planned full frontend)
├── requirements.txt
├── run_services.ps1             # Start/stop all services (Windows)
├── run_services.bat             # Double-click wrapper for run_services.ps1
└── run_services.sh              # Start all services (Linux)
```

## Roadmap

- Next.js frontend with dashboards (the chat page currently ships with the agent service)
- User authentication (currently `user_id` is passed by the client)
- Persistent conversation history
- API gateway and Docker deployment
