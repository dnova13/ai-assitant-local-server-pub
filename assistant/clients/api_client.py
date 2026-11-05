"""서버 안 입구(디스코드 봇 등)가 비서 입구(entrances/api_server.py)를 부르는 부분.
비서 본체를 직접 쓰지 않고 /internal/message 로 보낸다. 일정 알림은 /internal/due 로 받아 간다. 주소와 봇 키는 .env 의 API_URL, BOT_API_KEY.
오류 문구에는 주소나 키를 넣지 않는다."""
import requests

from core import config


class ApiError(Exception):
    pass


def _post(path, body, timeout):
    if not config.BOT_API_KEY:
        raise ApiError("BOT_API_KEY 가 설정되지 않았어요. .env 에 봇 키를 넣어 주세요.")
    try:
        r = requests.post(f"{config.API_URL}{path}", json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {config.BOT_API_KEY}"})
    except requests.ConnectionError:
        raise ApiError("비서 입구(assistant-api)에 연결할 수 없어요. 서비스가 켜져 있는지 확인해 주세요.")
    except requests.Timeout:
        raise ApiError("비서 입구 응답이 너무 늦어요.")
    except UnicodeEncodeError:  # 키에 한글 등이 섞이면 헤더에 넣을 수 없다
        raise ApiError("봇 키에 영문·숫자가 아닌 글자가 있어요. .env 의 BOT_API_KEY 를 확인해 주세요.")
    except requests.RequestException as e:  # 잘못된 API_URL 등. 주소는 문구에 넣지 않는다
        raise ApiError(f"비서 입구 요청 오류 ({type(e).__name__}). .env 의 API_URL 을 확인해 주세요.")
    if r.status_code == 401:
        raise ApiError("봇 키가 맞지 않아요. .env 의 BOT_API_KEY 를 확인해 주세요.")
    if r.status_code == 403:
        raise ApiError("봇 키가 아니거나 서버 안에서 보낸 요청이 아니에요.")
    if not r.ok:
        raise ApiError(f"비서 입구 오류 ({r.status_code})")
    try:
        return r.json()
    except ValueError:
        raise ApiError("비서 입구 응답을 읽을 수 없어요.")


def send(source, external_id, text, model=None, timeout=300, show_rows=False, private=False, channel=None):
    """사용자 대신 메시지를 보내고 답을 돌려준다. 연결되지 않은 사용자면 None."""
    return send_detail(source, external_id, text, model, timeout, show_rows, private, channel).get("reply")


def files_sent(user, channel_id, message_id, name, days, timeout=30):
    return _post("/internal/files/sent", {"user": user, "channel_id": str(channel_id), "message_id": str(message_id), "name": name, "days": days}, timeout)


def files_due(timeout=30):
    return _post("/internal/files/due", {}, timeout).get("items") or []


def files_ack(ids, timeout=30):
    return _post("/internal/files/ack", {"ids": ids}, timeout)


def send_detail(source, external_id, text, model=None, timeout=300, show_rows=False, private=False, channel=None, dm=False, targets=None, files=None, file_prompt_id=None):
    """send 와 같고 응답 전체를 돌려준다 ({"reply", "delete_after"} 등). dm: 디스코드 DM 에서 온 말. show_rows: 행 내용이 보이는 DB 기능을 써도 되는 곳인가."""
    body = {"source": source, "external_id": str(external_id), "text": text}
    if show_rows:
        body["show_rows"] = True
    if private:
        body["private"] = True
    if channel:
        body["channel"] = channel
    if dm:
        body["dm"] = True
    if targets:
        body["targets"] = targets
    if files:
        body["files"] = files
    if file_prompt_id is not None:
        body["file_prompt_id"] = file_prompt_id
    if model:
        body["model"] = model
    return _post("/internal/message", body, timeout)


def due(source, timeout=30):
    """알림 보낼 때가 된 일정과 미리 알림 [{"user", "external_ids", "ids", "pre", "text"}]."""
    return _post("/internal/due", {"source": source}, timeout).get("items") or []


def briefing_due(source, timeout=600):
    """아침 브리핑을 보낼 사람과 글 [{"user", "external_ids", "date", "text"}]. 글을 만드느라 느릴 수 있다."""
    return _post("/internal/briefing/due", {"source": source}, timeout).get("items") or []


def briefing_ack(user, date, timeout=30):
    return _post("/internal/briefing/ack", {"user": user, "date": date}, timeout).get("ok", False)


def channels_list(timeout=30):
    """관리 채널 [{"id", "latest"}]."""
    return _post("/internal/channels/list", {}, timeout).get("items") or []


def channels_store(rows, live=False, timeout=60):
    """글과 첨부파일 정보를 저장한다. live 이면 읽어 볼 말인지 돌려준다."""
    return _post("/internal/channels/store", {"rows": rows, "live": live}, timeout).get("needs_reply", False)


def channels_request(channel_id, message_id, author_id, text, timeout=300):
    """관리 채널의 말 하나 처리 {"reply", "delete_ids"}. 모델을 불러 느릴 수 있다."""
    body = {"channel_id": channel_id, "message_id": message_id, "author_id": author_id, "text": text}
    return _post("/internal/channels/request", body, timeout)


def channels_edit(message_id, text, timeout=30):
    return _post("/internal/channels/edit", {"id": message_id, "text": text}, timeout).get("ok", False)


def channels_forget(ids, timeout=30):
    return _post("/internal/channels/forget", {"ids": list(ids)}, timeout).get("ok", False)


def news_due(timeout=300):
    """올릴 차례인 뉴스 구독 [{"id", "channel_id", "slot", "text", "titles", "turn"}]. 출처를 읽느라 느릴 수 있다."""
    return _post("/internal/news/due", {}, timeout).get("items") or []


def news_ack(sub_id, slot, titles, turn, timeout=30):
    body = {"id": sub_id, "slot": slot, "titles": list(titles), "turn": turn}
    return _post("/internal/news/ack", body, timeout).get("ok", False)


def ack(ids, timeout=30):
    """보낸 일정을 '보냄'으로 표시한다."""
    return _post("/internal/due/ack", {"ids": list(ids)}, timeout).get("marked", 0)


def ack_pre(pre, timeout=30):
    """보낸 미리 알림을 표시한다. pre: [[일정 번호, 몇 분 전]]"""
    return _post("/internal/due/ack", {"pre": list(pre)}, timeout).get("marked", 0)
