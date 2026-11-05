"""Ollama 호환 주소 (/api/…). 앱이 서버 주소와 API 키만 바꾸면 Ollama 처럼 비서를 쓴다 (대화만).

  GET  /                  살아 있는지 (키 없이. Ollama 와 같은 글자를 돌려준다)
  GET  /api/version       서버 Ollama 버전
  GET  /api/tags          서버 Ollama 에 설치된 모델 목록 그대로 (앱의 모델 선택 목록)
  POST /api/chat          마지막 사용자 메시지를 비서가 처리. 앱이 고른 모델로 분류와 대화를 한다

- 비서는 사용자별 대화 기억을 스스로 갖고 있어서, 앱이 보낸 이전 메시지와 시스템 프롬프트는 쓰지 않는다.
"""
import json
from datetime import datetime, timezone

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from core import config, users
from entrances.api.common import bot, json_body, require_key

router = APIRouter()


def _ollama_get(path):
    try:
        r = requests.get(f"{config.OLLAMA_URL}{path}", headers=config.ollama_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        raise HTTPException(502, f"서버의 Ollama 에서 정보를 가져오지 못했습니다 ({type(e).__name__}).")


def _last_user_text(body):
    for m in reversed(body.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


@router.get("/", response_class=PlainTextResponse)
def root():
    return "Ollama is running"  # 앱의 자동 인식이 이 글자를 볼 수 있어서 Ollama 와 같게 둔다


@router.get("/api/version")
def version(key=Depends(require_key)):
    return _ollama_get("/api/version")


@router.get("/api/tags")
def tags(key=Depends(require_key)):
    return _ollama_get("/api/tags")


@router.post("/api/chat")
async def chat(request: Request, key=Depends(require_key)):
    if key["kind"] != "app":
        raise HTTPException(403, "앱 키가 아닙니다.")
    body = await json_body(request)
    model = body.get("model") or config.MODEL
    user = key["user"]
    # 비서는 모델 응답을 기다리며 멈추는 코드라 별도 스레드에서 돌린다 (서버 전체가 멈추지 않게)
    reply = await run_in_threadpool(bot.handle, user, _last_user_text(body), model=model,
                                    allow_admin=users.is_admin(user))
    now = datetime.now(timezone.utc).isoformat()

    def msg(content, done):
        out = {"model": model, "created_at": now, "message": {"role": "assistant", "content": content}, "done": done}
        if done:
            out["done_reason"] = "stop"
        return out

    if body.get("stream", True) is False:  # Ollama 처럼 stream 을 안 보내면 스트림으로 답한다
        return msg(reply, True)
    lines = (json.dumps(m, ensure_ascii=False) + "\n" for m in (msg(reply, False), msg("", True)))
    return StreamingResponse(lines, media_type="application/x-ndjson")
