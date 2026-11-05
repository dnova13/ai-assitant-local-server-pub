"""일정 알림과 아침 브리핑 한 번 돌리기 (디스코드 봇이 일정은 10초, 브리핑은 1분마다 부른다).
디스코드에 묶이지 않게 받아 오기·보내기·표시하기를 함수로 받는다 (시험에서 가짜로 바꿔 끼운다).
- 한 사람에게 보내기가 하나라도 성공하면 '보냄'으로 표시한다.
- 잠깐의 실패(연결 끊김 등)만 있으면 표시하지 않아 다음 차례에 다시 보낸다.
- 다시 해도 안 되는 실패(GiveUp: 없는 사용자, DM 막힘)뿐이면 포기하고 '보냄'으로 표시한다 (계속 다시 시도하지 않게).
- 봇이 꺼져 있던 동안 지난 일정은 서버가 '놓친 일정'으로 묶어 문구를 만든다."""
import logging

log = logging.getLogger("assistant.reminders")


class GiveUp(Exception):
    """다시 보내도 안 되는 실패 (send 가 이것을 내면 포기한다)."""


async def _deliver(item, send, what):
    """('sent' | 'give_up' | 'retry')"""
    ok, retry = False, False
    for external_id in item.get("external_ids") or []:
        try:
            await send(external_id, item["text"])
            ok = True
        except GiveUp as e:  # 내용은 기록하지 않는다
            log.warning("%s 보낼 수 없음, 포기 (%s): %s", what, item.get("user"), e)
        except Exception as e:  # 한 사람이 실패해도 다른 사람에게는 보낸다
            retry = True
            log.warning("%s 보내기 실패, 다음에 다시 (%s): %s", what, item.get("user"), type(e).__name__)
    return "sent" if ok else ("retry" if retry else "give_up")


async def run_once(fetch_due, send, ack, ack_pre=None):
    """fetch_due() -> [{"user", "external_ids", "ids", "pre", "text"}], send(external_id, text), ack(ids), ack_pre(pre).
    모두 async 함수. '보냄'으로 표시한 일정 번호 목록을 돌려준다 (포기한 것 포함). 미리 알림(pre)은 ack_pre 로 따로 표시한다."""
    marked, pre = [], []
    for item in await fetch_due():
        if await _deliver(item, send, "일정 알림") != "retry":
            marked += item["ids"]
            pre += item.get("pre") or []
    if marked:
        await ack(marked)
    if pre and ack_pre:
        await ack_pre(pre)
    return marked


async def run_briefings(fetch_due, send, ack):
    """아침 브리핑 한 번. fetch_due() -> [{"user", "external_ids", "date", "text"}], ack(user, date).
    '오늘 보냄'으로 표시한 사람 목록을 돌려준다 (포기한 사람 포함)."""
    done = []
    for item in await fetch_due():
        if await _deliver(item, send, "브리핑") != "retry":
            await ack(item["user"], item["date"])
            done.append(item["user"])
    return done


async def run_news(fetch_due, send, ack):
    """뉴스 구독 한 번. fetch_due() -> [{"id", "channel_id", "slot", "text", "titles", "turn"}], send(channel_id, text),
    ack(id, slot, titles, turn). 다 보낸 뒤에만 기사를 보낸 것으로 표시한다. 잠깐의 실패는 표시하지 않아 다음 차례에 다시 보내고,
    GiveUp(권한 없음, 없는 채널)이면 그 시각만 넘긴다. 올린 구독 번호 목록을 돌려준다."""
    done = []
    for item in await fetch_due():
        try:
            await send(item["channel_id"], item["text"])
        except GiveUp as e:
            log.warning("뉴스 올릴 수 없음, 이번 시각은 건너뜀 (구독 %s): %s", item["id"], e)
            await ack(item["id"], item["slot"], [], None)
        except Exception as e:
            log.warning("뉴스 올리기 실패, 다음에 다시 (구독 %s): %s", item["id"], type(e).__name__)
        else:
            await ack(item["id"], item["slot"], item["titles"], item["turn"])
            done.append(item["id"])
    return done


async def run_sent_files(fetch, delete, ack, gone_error):
    """기한이 지난 파일 메시지를 지운다. 이미 없어졌으면 기록만 지우고, 다른 오류면 다음에 다시 한다."""
    done = []
    for item in await fetch():
        try:
            await delete(item)
        except gone_error:
            pass
        except Exception as e:
            log.warning("보낸 파일 메시지 삭제 실패: %s", type(e).__name__)
            continue
        done.append(item["message_id"])
    if done:
        await ack(done)
    return done
