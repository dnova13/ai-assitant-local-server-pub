"""디스코드용 입구. 서버에서 항상 켜두고 사용한다.
DM과 등록한 서버·채널에서는 모든 메시지에, 그 밖의 채널에서는 '!'로 시작하는 메시지에만 응답한다 (entrances/discord_rules.py).
비서 본체를 직접 쓰지 않고, 비서 입구(entrances/api_server.py)에 봇 키로 보낸다 (clients/api_client.py).
보낸 사람의 디스코드 ID만 넘기고, 내부 ID로 바꾸는 일과 연결되지 않은 사람 무시는 입구가 한다.
그래서 assistant-api 가 먼저 켜져 있어야 한다. 디스코드 토큰은 여기서만 쓴다.
일정 알림: 10초마다 입구에 알릴 일정을 물어 DM으로 보낸다 (entrances/reminders.py). 켜질 때 바로 한 번 확인해 꺼진 동안 놓친 일정도 보낸다.
채널 관리: 지정한 채널의 글을 저장하고, 답할 말은 대기열에 넣어 하나씩 처리한다 ('!채널관리', README.md).
뉴스 구독: 1분마다 올릴 차례인 구독을 묻고 그 채널에 올린다 ('!뉴스 구독', README.md).
아침 브리핑: 따로 1분마다 브리핑을 보낼 사람이 있는지 묻고 DM으로 보낸다 ('!브리핑 켜기'로 켠 사람만). 글을 만드느라 느려도 일정 알림을 막지 않는다."""
import asyncio
import base64
import io
import time
import io
import logging
import re
from datetime import datetime, timedelta, timezone

import discord

from core import config
from clients import api_client
from entrances import discord_rules, reminders
from entrances.file_prompts import FilePromptView

if not config.DISCORD_TOKEN or config.DISCORD_TOKEN.startswith("여기에"):
    raise SystemExit("DISCORD_TOKEN이 설정되지 않았습니다. .env 에 실제 봇 토큰을 넣어주세요.")
if not config.BOT_API_KEY:
    raise SystemExit("BOT_API_KEY가 설정되지 않았습니다. python -m core.apikeys create owner \"디스코드 봇\" --kind bot 으로 발급해 .env 에 넣어주세요.")
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
log = logging.getLogger("assistant.bot")
REMINDER_SECONDS = 10
SENT_FILE_SECONDS = 3600
BRIEFING_SECONDS = 60
NEWS_SECONDS = 60
CHANNEL_SECONDS = 60
BACKFILL_LIMIT = 5000
BACKFILL_BATCH = 100
BULK_DELETE_DAYS = 13  # 디스코드의 한꺼번에 삭제는 14일 안의 글만 된다
NO_MENTIONS = discord.AllowedMentions.none()  # 저장한 글을 다시 보여 줄 때 사람을 부르지 않는다
_reminder_task = None
_briefing_task = None
_news_task = None
_channel_task = None
_request_task = None
_sent_file_task = None
_managed = set()
_caught_up = set()
_backlog = discord_rules.Backlog()
_file_waits = discord_rules.FileWaits()
_backlog_ready = asyncio.Event()


async def _send_dm(external_id, text):
    try:
        user = client.get_user(int(external_id)) or await client.fetch_user(int(external_id))
        for chunk in discord_rules.split_message(text):
            await user.send(chunk)
    except (discord.NotFound, discord.Forbidden, ValueError) as e:  # 없는 사용자, DM 막힘: 다시 해도 안 된다
        raise reminders.GiveUp(type(e).__name__)


async def _send_channel(channel_id, text):
    try:
        channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
        for chunk in discord_rules.split_message(text):
            await channel.send(chunk)
    except (discord.NotFound, discord.Forbidden, ValueError) as e:  # 없는 채널, 권한 없음: 다시 해도 안 된다
        raise reminders.GiveUp(type(e).__name__)


async def _fetch_due():
    return await asyncio.to_thread(api_client.due, "discord")


async def _ack(ids):
    await asyncio.to_thread(api_client.ack, ids)


async def _ack_pre(pre):
    await asyncio.to_thread(api_client.ack_pre, pre)


async def _fetch_briefings():
    return await asyncio.to_thread(api_client.briefing_due, "discord")


async def _ack_briefing(user, date):
    await asyncio.to_thread(api_client.briefing_ack, user, date)


async def _fetch_news():
    return await asyncio.to_thread(api_client.news_due)


async def _ack_news(sub_id, slot, titles, turn):
    await asyncio.to_thread(api_client.news_ack, sub_id, slot, titles, turn)


async def reminder_loop():
    while not client.is_closed():
        try:
            await reminders.run_once(_fetch_due, _send_dm, _ack, _ack_pre)
        except api_client.ApiError as e:
            log.warning("일정 알림 확인 실패: %s", e)
        except Exception:  # 루프가 멈추지 않게 한다
            log.exception("일정 알림 루프 오류")
        await asyncio.sleep(REMINDER_SECONDS)


async def briefing_loop():
    while not client.is_closed():
        try:
            await reminders.run_briefings(_fetch_briefings, _send_dm, _ack_briefing)
        except api_client.ApiError as e:
            log.warning("브리핑 확인 실패: %s", e)
        except Exception:
            log.exception("브리핑 루프 오류")
        await asyncio.sleep(BRIEFING_SECONDS)


async def news_loop():
    while not client.is_closed():
        try:
            await reminders.run_news(_fetch_news, _send_channel, _ack_news)
        except api_client.ApiError as e:
            log.warning("뉴스 확인 실패: %s", e)
        except Exception:
            log.exception("뉴스 루프 오류")
        await asyncio.sleep(NEWS_SECONDS)


def _channel_row(m):
    files = []
    for a in m.attachments:
        kind = (a.content_type or "").split("/")[0]
        files.append({"filename": discord_rules.attachment_name(getattr(a, "title", None), a.filename), "size": a.size, "kind": kind if kind in ("image", "video") else "file"})
    return {"id": str(m.id), "channel_id": str(m.channel.id), "author_id": str(m.author.id),
            "author_name": m.author.display_name, "text": m.content, "link": m.jump_url,
            "at": m.created_at.isoformat(), "files": files}


def _is_plain(m):
    return not m.author.bot and m.content.strip()[:1] not in ("!", "/")


async def _catch_up(channel_id, latest):
    """지정한 채널의 이전 글이나 봇이 꺼져 있던 동안의 글을 저장한다."""
    try:
        channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
        after = datetime.fromisoformat(latest) if latest else None
        rows = []
        async for m in channel.history(limit=BACKFILL_LIMIT, after=after):
            if _is_plain(m):
                rows.append(_channel_row(m))
            if len(rows) >= BACKFILL_BATCH:
                await asyncio.to_thread(api_client.channels_store, rows)
                rows = []
        if rows:
            await asyncio.to_thread(api_client.channels_store, rows)
    except (discord.NotFound, discord.Forbidden) as e:
        log.warning("관리 채널 글을 읽을 수 없음: %s", type(e).__name__)
    except api_client.ApiError as e:
        log.warning("관리 채널 글 저장 실패: %s", e)


async def _refresh_managed():
    items = await asyncio.to_thread(api_client.channels_list)
    _managed.clear()
    _managed.update(int(i["id"]) for i in items)
    _caught_up.intersection_update(_managed)
    for item in items:
        if int(item["id"]) not in _caught_up:
            _caught_up.add(int(item["id"]))
            asyncio.create_task(_catch_up(item["id"], item["latest"]))


async def channel_list_loop():
    while not client.is_closed():
        try:
            await _refresh_managed()
        except api_client.ApiError as e:
            log.warning("관리 채널 확인 실패: %s", e)
        except Exception:
            log.exception("관리 채널 루프 오류")
        await asyncio.sleep(CHANNEL_SECONDS)


async def _delete_messages(channel, ids):
    """지운 수와 실패 수. 14일 안의 글은 한꺼번에, 그 전 글은 하나씩 지운다."""
    limit = datetime.now(timezone.utc) - timedelta(days=BULK_DELETE_DAYS)
    numbers = [int(i) for i in ids]
    recent = [discord.Object(n) for n in numbers if discord.utils.snowflake_time(n) > limit]
    old = [n for n in numbers if discord.utils.snowflake_time(n) <= limit]
    done, failed = 0, 0
    for start in range(0, len(recent), BACKFILL_BATCH):
        part = recent[start:start + BACKFILL_BATCH]
        try:
            await channel.delete_messages(part)
            done += len(part)
        except discord.HTTPException as e:
            failed += len(part)
            log.warning("메시지 삭제 실패: %s", type(e).__name__)
    for number in old:
        try:
            await channel.get_partial_message(number).delete()
            done += 1
        except discord.NotFound:
            done += 1
        except discord.HTTPException as e:
            failed += 1
            log.warning("메시지 삭제 실패: %s", type(e).__name__)
    return done, failed


async def _answer_channel(m):
    out = await asyncio.to_thread(api_client.channels_request, str(m.channel.id), str(m.id), str(m.author.id),
                                  re.sub(rf"<@!?{client.user.id}>", " ", m.content))
    if out.get("delete_ids"):
        done, failed = await _delete_messages(m.channel, out["delete_ids"])
        if not failed:
            await asyncio.to_thread(api_client.channels_forget, out["delete_ids"])
        reply = f"{done}개를 지웠어요." + (f" {failed}개는 지우지 못했어요. 봇의 메시지 관리 권한을 확인해 주세요." if failed else "")
    else:
        reply = out.get("reply")
    if reply:
        for chunk in discord_rules.split_message(reply):
            await m.channel.send(chunk, allowed_mentions=NO_MENTIONS)
    if out.get("file"):
        data = io.BytesIO(out["file"]["text"].encode("utf-8"))
        await m.channel.send(file=discord.File(data, filename=out["file"]["name"]), allowed_mentions=NO_MENTIONS)


async def channel_request_loop():
    while not client.is_closed():
        await _backlog_ready.wait()
        _backlog_ready.clear()
        while (m := _backlog.pop()) is not None:
            try:
                await _answer_channel(m)
            except api_client.ApiError as e:
                log.warning("관리 채널 요청 실패: %s", e)
            except discord.HTTPException as e:
                log.warning("관리 채널 답 보내기 실패: %s", type(e).__name__)
            except Exception:
                log.exception("관리 채널 요청 오류")


async def _on_channel_message(m):
    """관리 채널의 글: 평소 기능을 막고 저장한 뒤, 읽어 볼 말만 대기열에 넣는다."""
    if not _is_plain(m):
        return
    try:
        needs_reply = await asyncio.to_thread(api_client.channels_store, [_channel_row(m)], True)
    except api_client.ApiError as e:
        log.warning("관리 채널 글 저장 실패: %s", e)
        return
    if needs_reply:
        dropped = _backlog.push(m)
        if dropped:
            log.warning("관리 채널 요청이 밀려 %s개를 건너뜀", dropped)
        _backlog_ready.set()


def _channel_targets(content):
    """!채널관리 <이름> 의 후보 채널. #채널이면 그 채널 하나."""
    word = discord_rules.channel_word(content)
    if not word:
        return None
    mention = re.fullmatch(r"<#(\d+)>", word)
    found = [client.get_channel(int(mention.group(1)))] if mention else [
        c for g in client.guilds for c in g.text_channels if c.name == word.lstrip("#")]
    return [{"id": str(c.id), "name": c.name, "guild": c.guild.name} for c in found if c is not None]


@client.event
async def on_raw_message_delete(payload):
    if payload.channel_id in _managed:
        try:
            await asyncio.to_thread(api_client.channels_forget, [str(payload.message_id)])
        except api_client.ApiError as e:
            log.warning("지운 글 반영 실패: %s", e)


@client.event
async def on_raw_bulk_message_delete(payload):
    if payload.channel_id in _managed:
        try:
            await asyncio.to_thread(api_client.channels_forget, [str(i) for i in payload.message_ids])
        except api_client.ApiError as e:
            log.warning("지운 글 반영 실패: %s", e)


@client.event
async def on_raw_message_edit(payload):
    if payload.channel_id in _managed and isinstance(payload.data.get("content"), str):
        try:
            await asyncio.to_thread(api_client.channels_edit, str(payload.message_id), payload.data["content"])
        except api_client.ApiError as e:
            log.warning("고친 글 반영 실패: %s", e)


@client.event
async def on_ready():
    global _reminder_task, _briefing_task, _news_task, _channel_task, _request_task, _sent_file_task
    print(f"로그인: {client.user}")
    if _reminder_task is None or _reminder_task.done():  # 다시 연결될 때 on_ready 가 또 불려도 하나만 돈다
        _reminder_task = asyncio.create_task(reminder_loop())
    if _briefing_task is None or _briefing_task.done():
        _briefing_task = asyncio.create_task(briefing_loop())
    if _news_task is None or _news_task.done():
        _news_task = asyncio.create_task(news_loop())
    if _channel_task is None or _channel_task.done():
        _channel_task = asyncio.create_task(channel_list_loop())
    if _request_task is None or _request_task.done():
        _request_task = asyncio.create_task(channel_request_loop())
    if _sent_file_task is None or _sent_file_task.done():
        _sent_file_task = asyncio.create_task(sent_file_loop())


async def _deliver_file(author, user, file, reply):
    """앱이 올린 파일을 명령한 사람의 DM 으로 보내고, 지울 번호와 기한을 서버에 남긴다."""
    data = base64.b64decode(file["data"])
    try:
        sent = await author.send(file["note"] or None, file=discord.File(io.BytesIO(data), filename=file["name"]))
    except discord.HTTPException as e:
        log.warning("파일 보내기 실패: %s", type(e).__name__)
        return f"⚠️ 디스코드가 파일을 받지 않았어요 ({len(data) // (1024 * 1024)}MB). 파일이 너무 크거나 DM 을 받을 수 없는 상태예요."
    try:
        await asyncio.to_thread(api_client.files_sent, user, sent.channel.id, sent.id, file["name"], file["days"])
    except api_client.ApiError as e:
        log.warning("보낸 파일 기록 실패: %s", e)
    return reply


async def _delete_sent(item):
    channel = client.get_partial_messageable(int(item["channel_id"]))
    await channel.get_partial_message(int(item["message_id"])).delete()


async def sent_file_loop():
    while not client.is_closed():
        try:
            await reminders.run_sent_files(lambda: asyncio.to_thread(api_client.files_due), _delete_sent,
                                           lambda ids: asyncio.to_thread(api_client.files_ack, ids), discord.NotFound)
        except api_client.ApiError as e:
            log.warning("보낸 파일 삭제 확인 실패: %s", e)
        except Exception:  # 루프가 멈추지 않게 한다
            log.exception("보낸 파일 삭제 루프 오류")
        await asyncio.sleep(SENT_FILE_SECONDS)


_file_prompt_views = {}  # (채널, 사용자) -> 현재 질문의 버튼


async def _close_file_prompt(m):
    view = _file_prompt_views.pop((m.channel.id, m.author.id), None)
    if view:
        await view.close()


async def _send_file_reply(m, out, reply, delete_after=None):
    await _close_file_prompt(m)
    if reply is None:
        return
    prompt = out.get("file_prompt")
    view = None
    if prompt:
        async def answer(value, prompt_id):
            is_dm = isinstance(m.channel, discord.DMChannel)
            channel = None if is_dm else {"id": str(m.channel.id), "name": getattr(m.channel, "name", ""), "guild": getattr(m.guild, "name", "")}
            trusted = discord_rules.shows_rows(is_dm, getattr(m.guild, "id", None), [m.channel.id, getattr(m.channel, "parent_id", None)])
            try:
                result = await asyncio.to_thread(api_client.send_detail, "discord", m.author.id, value, show_rows=trusted, private=trusted, channel=channel, dm=is_dm, file_prompt_id=prompt_id)
            except api_client.ApiError:
                result = {"reply": "답변을 전달하지 못했습니다. 현재 질문을 확인하거나 다시 요청해 주세요."}
            if result.get("file_waiting"):
                _file_waits.set(m.channel.id, m.author.id, time.time())
            else:
                _file_waits.clear(m.channel.id, m.author.id)
            text = result.get("reply")
            if result.get("file"):
                text = await _deliver_file(m.author, result["user"], result["file"], text)
            await _send_file_reply(m, result, text, result.get("delete_after"))
        view = FilePromptView(prompt, m.author.id, answer)
        _file_prompt_views[(m.channel.id, m.author.id)] = view
    chunks = discord_rules.split_message(reply)
    for i, chunk in enumerate(chunks):
        sent = await m.channel.send(chunk, delete_after=delete_after, **({"view": view} if view and i == len(chunks) - 1 else {}))
        if view and i == len(chunks) - 1:
            view.message = sent


@client.event
async def on_message(m):
    if m.author.bot:
        return
    if m.channel.id in _managed and not discord_rules.is_channel_command(m.content) and not discord_rules.is_discord_command(m.content):
        await _on_channel_message(m)
        return
    is_dm = isinstance(m.channel, discord.DMChannel)
    guild_id, channel_ids = getattr(m.guild, "id", None), [m.channel.id, getattr(m.channel, "parent_id", None)]
    text = discord_rules.message_text(m.content, is_dm, guild_id, channel_ids)
    waiting = _file_waits.active(m.channel.id, m.author.id, time.time())
    if text is None or waiting:
        text = discord_rules.file_text(m.content, waiting) or text
    files = None
    if is_dm and m.attachments:  # 받기: 앱이 켜져 있으면 앱에 알린다
        files = [{"id": str(a.id), "name": a.filename, "size": a.size, "url": a.url} for a in m.attachments]
        text = text or ""
    if text is None:
        return
    trusted = discord_rules.shows_rows(is_dm, guild_id, channel_ids)
    channel = None if is_dm else {"id": str(m.channel.id), "name": getattr(m.channel, "name", ""),
                                  "guild": getattr(m.guild, "name", "")}
    is_channel_command = discord_rules.is_channel_command(text)
    targets = _channel_targets(text) if is_channel_command else None
    await _close_file_prompt(m)
    try:
        async with m.channel.typing():
            out = await asyncio.to_thread(api_client.send_detail, "discord", m.author.id, text,
                                          show_rows=trusted, private=trusted, channel=channel, dm=is_dm, targets=targets, files=files)
        reply, delete_after = out.get("reply"), out.get("delete_after")
    except api_client.ApiError as e:
        out, reply, delete_after = {}, f"⚠️ {e}", None
    if out.get("file_waiting"):
        _file_waits.set(m.channel.id, m.author.id, time.time())
    else:
        _file_waits.clear(m.channel.id, m.author.id)
    if out.get("delete_source"):  # 채널에 친 인증 코드
        try:
            await m.delete()
        except discord.HTTPException as e:
            log.warning("인증 코드 메시지 삭제 실패: %s", type(e).__name__)
    if is_channel_command:
        try:
            await _refresh_managed()  # 지정과 해제를 바로 반영한다
        except api_client.ApiError as e:
            log.warning("관리 채널 확인 실패: %s", e)
    if out.get("file"):
        reply = await _deliver_file(m.author, out["user"], out["file"], reply)
    if reply is None:  # 연결되지 않았거나 비활성 사용자: 아무 말도 하지 않는다
        return
    try:
        await _send_file_reply(m, out, reply, delete_after)
    except discord.Forbidden:  # 이 채널에 글을 쓸 수 없으면 채널로는 알릴 수 없다
        try:
            await m.author.send("봇이 그 채널에 글을 쓸 권한이 없어요. 뉴스 구독도 올라가지 않으니 채널 권한을 확인해 주세요.")
        except discord.HTTPException as e:
            log.warning("권한 안내 DM 실패: %s", type(e).__name__)


client.run(config.DISCORD_TOKEN, log_level=getattr(logging, config.LOG_LEVEL, logging.INFO))
