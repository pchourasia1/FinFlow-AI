# Running FinFlow-AI on Windows

A step-by-step guide to setting up and running the backend on Windows 10/11. Commands are for **PowerShell**, run from the project root (the `FinFlow-AI` folder) unless a step says otherwise.

For how the pieces fit together, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Overview

| Step | What you do | How often |
|---|---|---|
| 1. [Prerequisites](#1-prerequisites) | Install Python, get a Supabase project and a Gemini key | Once |
| 2. [Python environment](#2-create-the-python-environment) | Create `venv` and install packages | Once |
| 3. [Configuration](#3-configure-secrets) | Fill in `backend\.env` and `supabase_config.yaml` | Once |
| 4. [Database](#4-set-up-the-database) | Create tables, generate and load data | Once (or to reset data) |
| 5. [Run](#5-run-the-services) | `.\run_services.ps1` | Every session |
| 6. [Verify](#6-verify) | Hit the health checks and send a chat message | Every session |
| 7. [Stop](#7-stop-the-services) | `Ctrl+C` or `.\run_services.ps1 -Stop` | Every session |

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.11 or 3.12** from [python.org](https://www.python.org/downloads/windows/) or `uv` | Avoid creating the venv from **Anaconda**: its bundled Visual C++ runtime is too old for current PyTorch (see [Troubleshooting](#pytorch-dll-error-winerror-1114)) |
| **Microsoft Visual C++ Redistributable (x64)** | Needed by PyTorch. Install the latest from [Microsoft](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist) |
| **Supabase project** | Free tier is fine. Enable the `vector` extension (Database → Extensions) |
| **Google Gemini API key** | From [Google AI Studio](https://aistudio.google.com/apikey) |
| **psql** (optional) | Only if you want to load CSVs with `\copy` instead of the Supabase table editor |

Check which Pythons you have:

```powershell
py -0p
```

## 2. Create the Python environment

```powershell
py -3.12 -m venv venv
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\python -m pip install -r requirements.txt
```

The install downloads PyTorch (about 200 MB) and takes a few minutes.

If you use [uv](https://docs.astral.sh/uv/) instead:

```powershell
uv venv --python 3.12 venv
uv pip install --python venv\Scripts\python.exe -r requirements.txt
```

A uv-created venv has no `pip` inside it, so wherever this guide says `venv\Scripts\python -m pip install ...`, use `uv pip install --python venv\Scripts\python.exe ...` instead.

Confirm PyTorch loads:

```powershell
venv\Scripts\python -c "import torch; print(torch.__version__)"
```

You don't need to activate the venv. The launcher and the commands below call `venv\Scripts\python` directly. To activate it anyway, run `venv\Scripts\Activate.ps1` (PowerShell) or `source venv/Scripts/activate` (Git Bash).

## 3. Configure secrets

Fill in the placeholders in two files. Both are git-ignored.

**`backend\.env`** (read by all three services):

| Variable | Where to find it |
|---|---|
| `GOOGLE_API_KEY` | Google AI Studio |
| `PROJECT_ID` | Supabase → Project Settings → General → Project ID |
| `PSWD` | The database password you set when creating the project |
| `SUPABASE_DB_URL` | Optional. Set it if your project is **not** in AWS `us-east-2`: Supabase → **Connect** → "Transaction pooler" URI |

**`data\scripts\supabase_config.yaml`** (read by the data scripts): the same `project_id` and `pwd`.

## 4. Set up the database

Do these in order.

| # | Step | Command |
|---|---|---|
| 1 | Create the tables | Open the Supabase **SQL editor**, paste `data\sql\schema.sql`, and run it |
| 2 | Generate synthetic CSVs | `venv\Scripts\python data\scripts\setup_data.py` |
| 3 | Import users, then accounts | Supabase **Table editor** → `users` → Insert → Import data from CSV → `data\synthetic\users.csv`. Repeat for `accounts` with `accounts.csv` |
| 4 | Load transactions | `venv\Scripts\python data\scripts\load_transactions.py` |
| 5 | Embed the policy document | `venv\Scripts\python data\scripts\vectorize.py` |

`setup_data.py` accepts `--users 1000` for a smaller dataset and `--seed 7` for different data.

If your project is outside `us-east-2`, the data scripts need the connection string too, because they don't read `.env`:

```powershell
$env:SUPABASE_DB_URL = "postgresql://postgres.<project-id>:<password>@aws-1-<region>.pooler.supabase.com:6543/postgres"
```

## 5. Run the services

### Option A: launcher script (recommended)

```powershell
.\run_services.ps1
```

Or double-click **`run_services.bat`** in File Explorer.

The launcher:

1. Checks that `venv` exists, warns if `backend\.env` is missing, and refuses to start if ports 8000-8002 are taken.
2. Starts the three services from the venv on `127.0.0.1`, with auto-reload on code changes.
3. Waits for each `/health` endpoint and prints `[ OK ]` or `[FAIL]` per service.
4. Keeps running until you press **Ctrl+C**, then stops every service and its child processes.

```text
  FinFlow AI

  [ OK ]  agent        http://localhost:8000   docs: http://localhost:8000/docs
  [ OK ]  vectorstore  http://localhost:8001   docs: http://localhost:8001/docs
  [ OK ]  database     http://localhost:8002   docs: http://localhost:8002/docs

  Logs: logs\<service>.log (server) and logs\<service>.out.log (debug prints)
  Press Ctrl+C to stop all services
```

The first start of the vectorstore service downloads the embedding model (about 90 MB), so it can take a minute.

| Option | Effect |
|---|---|
| `-NewWindows` | Open each service in its own console window so you can watch its output live, instead of writing to `logs\` |
| `-NoReload` | Turn off auto-reload (faster startup, fewer processes) |
| `-TimeoutSec 300` | Wait longer for services to become healthy (default 180) |
| `-Stop` | Stop any FinFlow services still running, then exit |

If PowerShell says running scripts is disabled, use `run_services.bat`, or allow local scripts once for your account:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### Option B: one terminal per service

Open three PowerShell windows in the project root:

```powershell
# Terminal 1: agent
cd backend\agent-service
..\..\venv\Scripts\python -m uvicorn app.main:app --port 8000 --reload

# Terminal 2: vectorstore
cd backend\vectorstore-service
..\..\venv\Scripts\python -m uvicorn app.main:app --port 8001 --reload

# Terminal 3: database
cd backend\database-service
..\..\venv\Scripts\python -m uvicorn app.main:app --port 8002 --reload
```

Each service must start from its own folder. The agent in particular loads its prompt file by relative path.

## 6. Verify

```powershell
# Health checks
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8001/health
Invoke-RestMethod http://localhost:8002/health

# Send a chat message (streams Server-Sent Events)
curl.exe -N -X POST http://localhost:8000/chat/stream `
  -H "Content-Type: application/json" `
  -d '{\"query\": \"What is my account balance?\", \"user_id\": 6}'
```

Use `curl.exe`, not `curl`: in Windows PowerShell 5.1, `curl` is an alias for `Invoke-WebRequest`, which doesn't stream.

Each service also has interactive API docs at `/docs`, for example http://localhost:8000/docs.

## 7. Stop the services

- In the launcher window: press **Ctrl+C**.
- If you closed that window, or a service is stuck: `.\run_services.ps1 -Stop`.

## Troubleshooting

### PyTorch DLL error (WinError 1114)

```text
OSError: [WinError 1114] A dynamic link library (DLL) initialization routine failed.
Error loading "...\torch\lib\c10.dll" or one of its dependencies.
```

Only the vectorstore service fails; the agent and database services start normally.

**Cause:** the venv was created from Anaconda's Python. A venv uses its base Python's folder for DLLs, and Anaconda ships an old Visual C++ runtime (`vcruntime140.dll` 14.27) that current PyTorch can't use.

**Fix:** recreate the venv from a non-Anaconda Python:

```powershell
.\run_services.ps1 -Stop
Remove-Item -Recurse -Force venv
uv venv --python 3.12 venv
uv pip install --python venv\Scripts\python.exe -r requirements.txt
```

Without uv, install Python 3.12 from python.org and use `py -3.12 -m venv venv` then `venv\Scripts\python -m pip install -r requirements.txt`.

Check the base Python with `venv\Scripts\python -c "import sys; print(sys.base_prefix)"`: it should not point to `anaconda3`.

### A service shows `[FAIL]`

Open `logs\<service>.log` and read the last traceback. Common causes:

| Error in log | Fix |
|---|---|
| `could not translate host name` / `connection refused` / `password authentication failed` | Check `PROJECT_ID`, `PSWD`, or set `SUPABASE_DB_URL` in `backend\.env` |
| `FileNotFoundError: app/llm/prompts/orchestration.json` | The agent was started outside `backend\agent-service`; use the launcher |
| `API key not valid` | Check `GOOGLE_API_KEY` in `backend\.env` |
| `ModuleNotFoundError` | Re-run `venv\Scripts\python -m pip install -r requirements.txt` |

### Port already in use

```powershell
.\run_services.ps1 -Stop
```

If something other than FinFlow holds the port, find it with:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object OwningProcess
```

### `UnicodeEncodeError` in service output

The services print emoji in their debug output. The launcher sets `PYTHONUTF8=1` to handle this; if you start services manually (Option B), run `$env:PYTHONUTF8 = '1'` in each terminal first.

### Windows Firewall prompt

The launcher binds to `127.0.0.1`, so the services are reachable only from this computer and Windows shouldn't prompt. If you start services manually with `--host 0.0.0.0`, Windows will ask whether to allow network access.
