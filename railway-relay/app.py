from __future__ import annotations

import os
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="telegram-relay", version="1.0.0")


class NotifyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    chat_id: Optional[str] = None
    relay_token: str


class NotifyResponse(BaseModel):
    ok: bool
    detail: str


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/notify", response_model=NotifyResponse)
def notify(payload: NotifyRequest) -> NotifyResponse:
    expected_relay_token = _env("RELAY_TOKEN")
    if not expected_relay_token:
        raise HTTPException(status_code=500, detail="RELAY_TOKEN is not set")
    if payload.relay_token != expected_relay_token:
        raise HTTPException(status_code=401, detail="invalid relay token")

    bot_token = _env("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN is not set")

    default_chat_id = _env("TELEGRAM_DEFAULT_CHAT_ID")
    chat_id = (payload.chat_id or default_chat_id).strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = {"chat_id": chat_id, "text": payload.text}

    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"telegram network error: {exc}") from exc

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"telegram http {r.status_code}: {r.text[:500]}")

    return NotifyResponse(ok=True, detail="sent")
