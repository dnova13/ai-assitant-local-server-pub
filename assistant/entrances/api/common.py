"""세 라우터(ollama_compat, app_v1, internal)가 함께 쓰는 것: 비서 본체 하나, 키 확인, 본문 읽기."""
import json

from fastapi import Depends, HTTPException, Request

from core import apikeys
from core.assistant import Assistant

LOCAL_HOSTS = {"127.0.0.1", "::1"}  # /internal/… 을 받는 곳 (서버 자기 자신)

bot = Assistant()  # 입구가 몇 개든 비서 본체는 하나 (사용자별 대화 기억을 여기서 가진다)


def _key_text(request):
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def require_key(request: Request):
    key = apikeys.verify(_key_text(request))
    if key is None:
        raise HTTPException(401, "API 키가 없거나 맞지 않습니다.")
    return key


def require_app(key=Depends(require_key)):
    if key["kind"] != "app":
        raise HTTPException(403, "앱 키가 아닙니다.")
    return key


def require_bot(request: Request, key=Depends(require_key)):
    """봇 키이고 서버 안(같은 PC)에서 온 요청만."""
    if key["kind"] != "bot":
        raise HTTPException(403, "봇 키가 아닙니다.")
    if (request.client.host if request.client else "") not in LOCAL_HOSTS:
        raise HTTPException(403, "서버 안에서만 쓸 수 있는 주소입니다.")
    return key


async def json_body(request):
    """Ollama 처럼 Content-Type 이 없어도 본문을 JSON 으로 읽는다."""
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        raise HTTPException(400, "요청 본문이 JSON 이 아닙니다.")
    if not isinstance(body, dict):
        raise HTTPException(400, "요청 본문은 JSON 객체여야 합니다.")
    return body
