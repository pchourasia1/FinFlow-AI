"""
    Main FastAPI application for the Agent Service.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from app.api.routes.chat import router as chat_router
from app.tools.orchestrator import orchestrator, _today

# Chat UI page (served at "/")
STATIC_DIR = Path(__file__).parent / "static"

# FastAPI app instance
app = FastAPI(
    title = "Agent Service",
    description = "AI Agent Orchestration service for FinFlow-AI",
    version = "1.0.0"
)

# CORS middleware to allow frontend to make requests to this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins = ["http://localhost:3000"],
    allow_credentials = True,
    allow_methods = ["*"],  # Allow all HTTP methods (GET, POST, PUT, DELETE, etc.)
    allow_headers = ["*"],  # Allow all headers
)

# Include the chat router
app.include_router(chat_router)

# Chat UI
@app.get("/", include_in_schema = False)
async def chat_ui():
    return FileResponse(STATIC_DIR / "index.html")

# Context shown in the chat UI header
@app.get("/info")
async def info():
    return {"model": orchestrator.model, "today": _today()}

@app.get("/health")
async def health_check():
    return {"status": "ok"}
