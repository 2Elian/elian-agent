"""
FastAPI server - HTTP API for the Claude Code backend.
Endpoints: POST /api/chat (SSE), GET /api/chat/{id}/history, DELETE /api/chat/{id}, GET /health.
"""
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from config import HOST, PORT
from engine import get_or_create_engine, get_engine, remove_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Claude Code Backend", version="0.2.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


class ChatRequest(BaseModel):
    query: str
    session_id: str | None = None
    model: str | None = None
    provider: str | None = None
    max_turns: int | None = None


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.2.0"}


@app.get("/api/models")
async def list_models():
    return {"models": [{"model_id": "mimo-v2-flash", "tier": "small", "provider": "openai"},
                       {"model_id": "mimo-v2.5-pro", "tier": "large", "provider": "openai"}]}


@app.post("/api/chat")
async def chat(request: ChatRequest):
    engine = get_or_create_engine(
        session_id=request.session_id,
        model=request.model,
        provider=request.provider,
        max_turns=request.max_turns,
    )

    async def generate():
        async for event in engine.submit_message(request.query):
            d = event.data
            if isinstance(d, (dict, list)):
                d = json.dumps(d, ensure_ascii=False)
            yield f"event: {event.type}\ndata: {d or ''}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


@app.get("/api/chat/{session_id}/history")
async def get_history(session_id: str):
    engine = get_engine(session_id)
    if not engine:
        raise HTTPException(404, "Session not found")
    return {"session_id": session_id, "messages": [{"role": m.role, "content": str(m.content)[:500]} for m in engine.get_messages()]}


@app.delete("/api/chat/{session_id}")
async def clear_chat(session_id: str):
    engine = get_engine(session_id)
    if not engine:
        raise HTTPException(404, "Session not found")
    engine.clear_history()
    return {"status": "cleared", "session_id": session_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=HOST, port=PORT, reload=True)
