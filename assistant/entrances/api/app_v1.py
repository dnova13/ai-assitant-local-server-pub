"""앱 규약 v1 (/v1/…, 앱 규약은 별도 앱 저장소에서 관리). 앱 키만. 파일 문장을 client_action 으로, 절전 확인을 confirm 으로 보낸다.

  GET  /v1/health         연결 확인과 기능 목록
  POST /v1/chat           {"text": "..."} -> NDJSON (delta / client_action / confirm / done)
  POST /v1/files/match    기존 이름과 임시 번호 -> 표기·발음이 맞는 후보 번호 (앱 키)
  POST /v1/actions/{id}/result  파일 동작 결과 {"status": "done|rejected|failed|expired", "detail": "..."}
  GET/POST /v1/computers, DELETE /v1/computers/{id}  앱으로 등록한 컴퓨터 목록, 등록 {"name": "…"}, 삭제
  WS   /v1/computers/connect  앱이 먼저 여는 연결. 첫 메시지로 키와 컴퓨터 번호를 보낸다 (주소에 키를 넣지 않는다)
  POST /v1/confirm        절전 승인 {"id": "c_…", "approve": true}. 첫 승인의 답에는 다음 창 "confirm" 이 붙는다
"""
import asyncio
import json
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from core import apikeys, classifier, config, users
from entrances.api import file_remote
from entrances.api.common import bot, json_body, require_app
from features import app_computers, files, confirmation

VERSION = "0.4.0"
RESULT_STATUS = ("done", "rejected", "failed", "expired")
MAX_DETAIL = 500
HELLO_SECONDS = 10  # 연결한 뒤 인증 메시지를 기다리는 시간
IDLE_SECONDS = 75  # 앱은 30초마다 ping 을 보낸다. 이만큼 조용하면 끊긴 것으로 본다
MAX_LINK_MESSAGE = 32768  # 앱의 답 (긴 찾기 결과)
CLOSE_POLICY, CLOSE_BAD_KEY, CLOSE_UNKNOWN, CLOSE_REPLACED = 1008, 4401, 4404, 4409

router = APIRouter(prefix="/v1")


@router.get("/health")
def v1_health(key=Depends(require_app)):
    return {"api": "1", "server": "local-ai-server", "version": VERSION, "model": config.MODEL,
            "features": {"chat_stream": True, "stt": False, "tts": False, "server_confirm": True,
                         "sessions": False, "low_spec": False, "client_actions": list(files.ACTIONS), "computer_link": True,
                         "file_name_match": True}}


@router.post("/files/match")
async def v1_file_names(request: Request, key=Depends(require_app)):
    """앱이 열거한 기존 이름만 비교한다. 경로·파일 내용·새 이름은 받지 않는다."""
    body = await json_body(request)
    try:
        query, candidates = files.validate_name_match(body.get("query"), body.get("candidates"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        ids = await run_in_threadpool(bot.match_file_names, query, candidates)
    except (files.NameMatchError, classifier.ClassifierError) as e:
        raise HTTPException(502, str(e))
    return {"ids": ids}


@router.post("/confirmation/interpret")
async def v1_confirmation_answer(request: Request, key=Depends(require_app)):
    body = await json_body(request)
    try:
        decision = await run_in_threadpool(confirmation.interpret, body.get("question"), body.get("answer"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except classifier.ClassifierError:
        raise HTTPException(502, "답의 뜻을 확인하지 못했습니다. 예 / 아니오로 다시 답해 주세요.")
    return {"decision": decision}


@router.post("/chat")
async def v1_chat(request: Request, key=Depends(require_app)):
    """한 번에 답을 만들어 delta 한 줄로 보낸다 (스트리밍 없음, 규약 3절 허용). session·options·client 는 아직 쓰지 않는다."""
    body = await json_body(request)
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(400, "text 가 필요합니다.")
    user = key["user"]
    out = await run_in_threadpool(bot.handle_ex, user, text, allow_admin=users.is_admin(user), client_actions=True)
    if out["confirm"]:
        events = [out["confirm"], {"type": "done"}]
    else:
        events = [{"type": "delta", "text": out["text"]}]
        if out["action"]:
            events.append(out["action"])
        events.append({"type": "done", "text": out["text"]})
    lines = (json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    return StreamingResponse(lines, media_type="application/x-ndjson")


@router.post("/actions/{action_id}/result", status_code=204)
async def v1_action_result(action_id: str, request: Request, key=Depends(require_app)):
    body = await json_body(request)
    status, detail = body.get("status"), body.get("detail") or ""
    if status not in RESULT_STATUS or not isinstance(detail, str):
        raise HTTPException(400, "status 는 done/rejected/failed/expired 중 하나입니다.")
    if not bot.record_result(key["user"], action_id, status, detail[:MAX_DETAIL]):
        raise HTTPException(404, "모르는 동작 id 입니다.")
    return Response(status_code=204)


@router.post("/confirm")
async def v1_confirm(request: Request, key=Depends(require_app)):
    body = await json_body(request)
    cid, approve = body.get("id"), body.get("approve")
    if not isinstance(cid, str) or not isinstance(approve, bool):
        raise HTTPException(400, "id 와 approve(true/false) 가 필요합니다.")
    if not users.is_admin(key["user"]):
        raise HTTPException(403, "이 명령은 관리자만 쓸 수 있어요.")
    ok, text, next_confirm = await run_in_threadpool(bot.confirm_v1, key["user"], cid, approve)
    if not ok:
        raise HTTPException(410, text)
    return {"text": text, "confirm": next_confirm} if next_confirm else {"text": text}


@router.get("/computers")
async def v1_computers(key=Depends(require_app)):
    return {"computers": await run_in_threadpool(app_computers.list_for, key["user"])}


@router.post("/computers", status_code=201)
async def v1_computer_add(request: Request, key=Depends(require_app)):
    body = await json_body(request)
    try:
        return await run_in_threadpool(app_computers.register, key["user"], body.get("name"), body.get("computer_id"))
    except app_computers.ComputerError as e:
        raise HTTPException(e.status, str(e))


@router.post("/computers/{computer_id}/files", status_code=204)
async def v1_computer_file(computer_id: str, request: Request, turn: str, key=Depends(require_app)):
    """앱이 보내는 파일. 진행 중인 요청의 답에 붙어 비서 봇으로 넘어가고, 서버에는 남지 않는다."""
    computer = await run_in_threadpool(app_computers.get, computer_id)
    if computer is None or computer["user"] != key["user"]:
        raise HTTPException(404, "등록되지 않은 컴퓨터입니다.")
    if int(request.headers.get("content-length") or 0) > file_remote.MAX_FILE_BYTES:
        raise HTTPException(413, "파일이 너무 큽니다 (20MB까지).")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > file_remote.MAX_FILE_BYTES:
            raise HTTPException(413, "파일이 너무 큽니다 (20MB까지).")
    name = unquote(request.headers.get("x-file-name") or "")
    note = unquote(request.headers.get("x-file-note") or "")
    try:
        days = int(request.headers.get("x-delete-days") or 7)
    except ValueError:
        raise HTTPException(400, "x-delete-days 는 숫자입니다.")
    if not name or "/" in name or "\\" in name or not file_remote.attach(computer_id, turn, name, bytes(data), note, days):
        raise HTTPException(404, "진행 중인 요청이 아닙니다.")
    return Response(status_code=204)


@router.delete("/computers/{computer_id}", status_code=204)
async def v1_computer_remove(computer_id: str, key=Depends(require_app)):
    if not await run_in_threadpool(app_computers.remove, key["user"], computer_id):
        raise HTTPException(404, "등록되지 않은 컴퓨터입니다.")
    link = app_computers.link_of(computer_id)
    if link is not None:
        await _reject(link, "unknown_computer", CLOSE_UNKNOWN)
    return Response(status_code=204)


def _check_link(key_text, computer_id):
    """키와 컴퓨터가 맞으면 (컴퓨터, None), 아니면 (None, 거절 이유). DB 오류는 그대로 올려 앱이 다시 연결하게 한다."""
    key = apikeys.verify(key_text) if isinstance(key_text, str) else None
    if key is None or key["kind"] != "app":
        return None, "bad_key"
    computer = app_computers.get(computer_id) if isinstance(computer_id, str) else None
    if computer is None or computer["user"] != key["user"]:
        return None, "unknown_computer"
    return computer, None


async def _reject(ws, reason, code):
    try:
        await ws.send_json({"type": "rejected", "reason": reason})
        await ws.close(code)
    except (RuntimeError, WebSocketDisconnect):  # 이미 끊긴 연결
        pass


async def _close_quietly(ws):
    try:
        await ws.close(CLOSE_POLICY)
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _receive(ws, seconds):
    """JSON 객체 하나. 형식이 틀리면 None."""
    try:
        text = await asyncio.wait_for(ws.receive_text(), seconds)
        message = json.loads(text) if len(text) <= MAX_LINK_MESSAGE else None
    except (ValueError, KeyError, TypeError):  # 글자가 아닌 메시지 포함
        return None
    return message if isinstance(message, dict) else None


@router.websocket("/computers/connect")
async def v1_computer_connect(ws: WebSocket):
    await ws.accept()
    computer_id = None
    try:
        hello = await _receive(ws, HELLO_SECONDS)
        if not hello or hello.get("type") != "hello":
            return await ws.close(CLOSE_POLICY)
        key_text, computer_id = hello.get("key"), hello.get("computer_id")
        computer, reason = await run_in_threadpool(_check_link, key_text, computer_id)
        if computer is None:
            return await _reject(ws, reason, CLOSE_BAD_KEY if reason == "bad_key" else CLOSE_UNKNOWN)
        old = app_computers.attach(computer_id, ws)
        if old is not None:
            await _reject(old, "replaced", CLOSE_REPLACED)
        await run_in_threadpool(app_computers.touch, computer_id)
        await ws.send_json({"type": "welcome", "name": computer["name"]})
        while True:
            message = await _receive(ws, IDLE_SECONDS)
            if message is None:
                return await ws.close(CLOSE_POLICY)
            if message.get("type") == "ping":
                # 연결 중에 키를 폐기하거나 등록을 지웠으면 여기서 끊는다
                computer, reason = await run_in_threadpool(_check_link, key_text, computer_id)
                if computer is None:
                    return await _reject(ws, reason, CLOSE_BAD_KEY if reason == "bad_key" else CLOSE_UNKNOWN)
                await ws.send_json({"type": "pong"})
            elif message.get("type") == "reply":
                file_remote.on_reply(computer_id, message)
    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        await _close_quietly(ws)
    finally:
        if computer_id and app_computers.detach(computer_id, ws):
            file_remote.on_disconnect(computer_id)
            await run_in_threadpool(app_computers.touch, computer_id)
