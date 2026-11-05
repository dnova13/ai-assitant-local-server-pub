"""봇 전용 주소 (/internal/…). bot 키 + 서버 안(127.0.0.1)에서 온 요청만 받는다.

  POST /internal/message  {"source": "discord", "external_id": "...", "text": "...", "show_rows": true}  (show_rows: DM·등록한 서버와 채널일 때만)
  POST /internal/due      알림 보낼 때가 된 일정을 사람별로 묶어 돌려준다 {"source": "discord"}
  POST /internal/due/ack  보낸 일정을 '보냄'으로 표시 {"ids": [1, 2], "pre": [[일정 번호, 몇 분 전]]}
  POST /internal/briefing/due  아침 브리핑을 보낼 사람과 글 {"source": "discord"}
  POST /internal/briefing/ack  보냈다고 표시 {"user": "owner", "date": "2026-10-22"}
  POST /internal/channels/list | store | request | edit | forget  디스코드 관리 채널 (README.md)
  POST /internal/news/due  올릴 차례인 뉴스 구독과 글 {}
  POST /internal/news/ack  올렸다고 표시 {"id": 1, "slot": "2026-11-03 07:00", "titles": ["제목"], "turn": 2}
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from core import users
from entrances.api import file_remote
from entrances.api.common import bot, json_body, require_bot
from features import app_computers, briefing, channel_manage, news, schedule

router = APIRouter(prefix="/internal")


@router.post("/message")
async def internal_message(request: Request, key=Depends(require_bot)):
    """디스코드 봇 같은 서버 안 입구가 사용자 대신 보내는 메시지. 연결되지 않은 사용자면 reply 가 null."""
    body = await json_body(request)
    source, external_id = body.get("source"), body.get("external_id")
    if source not in users.SOURCES or external_id in (None, ""):
        raise HTTPException(400, "source 와 external_id 가 필요합니다.")
    user = users.resolve(source, external_id)
    if user is None:
        return {"user": None, "reply": None}
    if source == "discord":  # 파일·!파일: 앱으로 전달한다 (정형 해석·자연어 분기는 앱이 맡는다)
        place = _channel(body) or {}
        out = await file_remote.handle(user, str(body.get("text") or ""), body.get("dm") is True, place.get("id"), place.get("name", ""), body.get("files"), body.get("file_prompt_id"))
        if out is not None:
            return out
    reply = await run_in_threadpool(bot.handle, user, str(body.get("text") or ""), model=body.get("model"),
                                    allow_admin=users.is_admin(user), show_rows=body.get("show_rows") is True,
                                    private=body.get("private") is True, channel=_channel(body), targets=_targets(body))
    return {"user": user, "reply": reply}


@router.post("/files/sent")
async def internal_files_sent(request: Request, key=Depends(require_bot)):
    body = await json_body(request)
    await run_in_threadpool(app_computers.record_sent, str(body.get("user") or ""), str(body.get("channel_id") or ""),
                            str(body.get("message_id") or ""), str(body.get("name") or ""), int(body.get("days") or 7))
    return {"ok": True}


@router.post("/files/due")
async def internal_files_due(request: Request, key=Depends(require_bot)):
    return {"items": await run_in_threadpool(app_computers.due_sent)}


@router.post("/files/ack")
async def internal_files_ack(request: Request, key=Depends(require_bot)):
    body = await json_body(request)
    await run_in_threadpool(app_computers.forget_sent, [str(i) for i in body.get("ids") or []])
    return {"ok": True}


@router.post("/due")
async def internal_due(request: Request, key=Depends(require_bot)):
    """알림 보낼 때가 된 일정과 미리 알림. 그 입구에 연결된 사람만 돌려준다 (연결이 없으면 보낼 곳이 없어 남겨 둔다).
    [{"user", "external_ids", "ids", "pre", "text"}] — text 는 보낼 문구. 보낸 뒤 /internal/due/ack 로 ids 와 pre 를 알린다."""
    source = (await json_body(request)).get("source") or "discord"
    if source not in users.SOURCES:
        raise HTTPException(400, "모르는 source 입니다.")
    current = schedule.now()
    by_user = {}
    for eid, user, at, text in schedule.due(current):
        by_user.setdefault(user, []).append((eid, at, text))
    pre_by_user = {}
    for eid, user, at, text, offset in schedule.pre_due(current):
        pre_by_user.setdefault(user, []).append((eid, at, text, offset))
    out = []
    for user in {**by_user, **pre_by_user}:
        ids = users.external_ids(user, source)
        if ids:
            rows, pre = by_user.get(user, []), pre_by_user.get(user, [])
            parts = [schedule.pre_text([p[:3] for p in pre], current) if pre else "",
                     schedule.reminder_text(rows, current) if rows else ""]
            out.append({"user": user, "external_ids": ids, "ids": [r[0] for r in rows],
                        "pre": [[p[0], p[3]] for p in pre], "text": "\n".join(x for x in parts if x)})
    return {"items": out}


@router.post("/briefing/due")
async def internal_briefing_due(request: Request, key=Depends(require_bot)):
    """아침 브리핑을 켰고, 보낼 시각이 되었고, 오늘 아직 안 보낸 사람. 글을 만드는 데 모델을 써서 느릴 수 있다."""
    source = (await json_body(request)).get("source") or "discord"
    if source not in users.SOURCES:
        raise HTTPException(400, "모르는 source 입니다.")
    today = schedule.now().date().isoformat()
    out = []
    for p in users.list_people():
        if not p["active"] or not briefing.should_send(p["id"]):
            continue
        ids = users.external_ids(p["id"], source)
        if ids:
            text = await run_in_threadpool(bot.briefing_text, p["id"])
            out.append({"user": p["id"], "external_ids": ids, "date": today, "text": text})
    return {"items": out}


@router.post("/briefing/ack")
async def internal_briefing_ack(request: Request, key=Depends(require_bot)):
    body = await json_body(request)
    user, day = body.get("user"), body.get("date")
    if not isinstance(user, str) or users.get(user) is None or not isinstance(day, str):
        raise HTTPException(400, "user 와 date 가 필요합니다.")
    try:
        briefing.mark_sent(user, datetime.strptime(day, "%Y-%m-%d").date())
    except ValueError:
        raise HTTPException(400, "date 는 YYYY-MM-DD 입니다.")
    return {"ok": True}


@router.post("/due/ack")
async def internal_due_ack(request: Request, key=Depends(require_bot)):
    body = await json_body(request)
    ids, pre = body.get("ids", []), body.get("pre", [])
    if not isinstance(ids, list) or not all(_is_int(i) for i in ids):
        raise HTTPException(400, "ids 는 숫자 목록이어야 합니다.")
    if not isinstance(pre, list) or not all(isinstance(p, list) and len(p) == 2 and all(_is_int(x) for x in p) for p in pre):
        raise HTTPException(400, "pre 는 [일정 번호, 몇 분 전] 목록이어야 합니다.")
    schedule.mark_pre(pre)
    return {"marked": schedule.mark_notified(ids)}


@router.post("/channels/list")
async def internal_channels_list(request: Request, key=Depends(require_bot)):
    return {"items": await run_in_threadpool(channel_manage.managed)}


@router.post("/channels/store")
async def internal_channels_store(request: Request, key=Depends(require_bot)):
    body = await json_body(request)
    rows = body.get("rows")
    if not isinstance(rows, list) or not all(_is_row(r) for r in rows):
        raise HTTPException(400, "rows 형식이 틀렸습니다.")
    for row in rows:
        row["at"] = datetime.fromisoformat(row["at"])
    return {"needs_reply": await run_in_threadpool(channel_manage.store, rows, body.get("live") is True)}


@router.post("/channels/request")
async def internal_channels_request(request: Request, key=Depends(require_bot)):
    body = await json_body(request)
    values = [body.get(k) for k in ("channel_id", "message_id", "author_id", "text")]
    if not all(isinstance(v, str) for v in values):
        raise HTTPException(400, "channel_id, message_id, author_id, text 가 필요합니다.")
    return await run_in_threadpool(bot.channel_request, *values)


@router.post("/channels/edit")
async def internal_channels_edit(request: Request, key=Depends(require_bot)):
    body = await json_body(request)
    if not isinstance(body.get("id"), str) or not isinstance(body.get("text"), str):
        raise HTTPException(400, "id 와 text 가 필요합니다.")
    await run_in_threadpool(channel_manage.edit, body["id"], body["text"])
    return {"ok": True}


@router.post("/channels/forget")
async def internal_channels_forget(request: Request, key=Depends(require_bot)):
    ids = (await json_body(request)).get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(400, "ids 는 글자 목록이어야 합니다.")
    await run_in_threadpool(channel_manage.forget, ids)
    return {"ok": True}


def _is_row(row):
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("channel_id"), str):
        return False
    try:
        datetime.fromisoformat(row.get("at"))
    except (TypeError, ValueError):
        return False
    return True


@router.post("/news/due")
async def internal_news_due(request: Request, key=Depends(require_bot)):
    """올릴 차례인 뉴스 구독. 올린 뒤 /internal/news/ack 로 알려 준다."""
    return {"items": await run_in_threadpool(news.due_posts)}


@router.post("/news/ack")
async def internal_news_ack(request: Request, key=Depends(require_bot)):
    body = await json_body(request)
    sid, slot, titles, turn = body.get("id"), body.get("slot"), body.get("titles", []), body.get("turn")
    if not _is_int(sid) or not isinstance(slot, str) or not _is_slot(slot):
        raise HTTPException(400, "id 와 slot 이 필요합니다.")
    if not isinstance(titles, list) or not all(isinstance(t, str) for t in titles):
        raise HTTPException(400, "titles 는 글자 목록이어야 합니다.")
    if turn is not None and not _is_int(turn):
        raise HTTPException(400, "turn 은 숫자여야 합니다.")
    return {"ok": news.mark_posted(sid, slot, titles, turn)}


def _channel(body):
    """디스코드 채널 정보 {"id", "name"} 또는 None (DM, 다른 입구)."""
    channel = body.get("channel")
    if isinstance(channel, dict) and isinstance(channel.get("id"), str) and channel["id"]:
        return {"id": channel["id"], "name": str(channel.get("name") or ""), "guild": str(channel.get("guild") or "")}
    return None


def _targets(body):
    """봇이 이름으로 찾은 채널들 [{"id", "name", "guild"}]."""
    targets = body.get("targets")
    if not isinstance(targets, list):
        return None
    return [t for t in (_channel({"channel": t}) for t in targets) if t]


def _is_slot(value):
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    return True


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)
