"""앱으로 등록한 컴퓨터 (파일 쪽). 서버는 번호와 이름, 지금 연결만 안다. 전원 쪽 컴퓨터(`computers` 표)와 따로 둔다.

- 이름은 `!파일 소니 이동`처럼 명령 안에 쓰므로 빈칸 없이, 동작 이름과 겹치지 않게 받는다. 계정마다 겹치면 거절한다.
- 지금 연결(_links)은 메모리에만 둔다. 서버가 다시 켜지면 앱이 다시 연결한다.
"""
import asyncio
import re
import secrets
import unicodedata
from datetime import timedelta

from core import users

MAX_NAME = 30
MAX_PER_USER = 20
BAD_NAME = re.compile(r"[\s/\\@!\"'`]|[\x00-\x1f\x7f]")
RESERVED = {"찾기", "열기", "보기", "위치", "이동", "복사", "이름", "이름변경", "이름수정", "폴더", "생성", "등록",
            "목록", "백업", "삭제", "추가", "추가해", "생성해", "등록해", "만들기", "만들어", "보내기", "받기", "도움말", "채널", "지정", "해제", "수정", "인증", "잠금",
            "기본", "파일", "컴퓨터", "전원", "둘다"}

_links = {}  # 컴퓨터 번호 -> 지금 연결
_loop = None  # 연결이 도는 이벤트 루프. 스레드에서 연결로 보낼 때 쓴다
DEFAULT_KEY = "file_default_computer"


class ComputerError(Exception):
    """사용자에게 그대로 보여 줄 거절 이유."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def clean_name(name):
    if not isinstance(name, str):
        raise ComputerError("컴퓨터 이름을 적어 주세요.")
    name = unicodedata.normalize("NFC", name).strip()
    if not name:
        raise ComputerError("컴퓨터 이름을 적어 주세요.")
    if len(name) > MAX_NAME:
        raise ComputerError(f"컴퓨터 이름은 {MAX_NAME}자까지입니다.")
    if BAD_NAME.search(name):
        raise ComputerError("컴퓨터 이름에는 빈칸과 / \\ @ ! 따옴표를 쓸 수 없어요.")
    if name.casefold() in RESERVED:
        raise ComputerError(f"'{name}'은(는) 명령에 쓰는 말이라 컴퓨터 이름으로 쓸 수 없어요.")
    return name


def _row(row):
    return {"id": row[0], "user": row[1], "name": row[2], "created": row[3], "last_seen": row[4]}


def _select(c, where, args):
    return c.execute(f"SELECT id, user_id, name, created, last_seen FROM app_computers WHERE {where}", args)


def register(user, name, computer_id=None):
    """기억한 ID는 소유자를 확인해 재사용하고, 서버에 없는 ID는 새로 발급한다."""
    name = clean_name(name)
    if computer_id is not None and (not isinstance(computer_id, str) or not re.fullmatch(r"pc_[A-Za-z0-9_-]{1,64}", computer_id)):
        raise ComputerError("컴퓨터 고유 번호 형식이 틀렸습니다.")
    renamed = False
    with users._conn() as c:
        row = _select(c, "id=?", (computer_id,)).fetchone() if computer_id else None
        existing = _row(row) if row else None
        if existing and existing["user"] != user:
            raise ComputerError("이 컴퓨터 등록은 다른 사용자 소유입니다.", 403)
        mine = [_row(r) for r in _select(c, "user_id=?", (user,)).fetchall()]
        if any(m["name"].casefold() == name.casefold() and (not existing or m["id"] != existing["id"]) for m in mine):
            raise ComputerError("이미 있는 이름입니다.", 409)
        if existing:
            computer_id = existing["id"]
            if existing["name"] != name:
                c.execute("UPDATE app_computers SET name=? WHERE id=? AND user_id=?", (name, computer_id, user))
                users._log(c, user, "app_computer_rename", user, f"{computer_id} {name}")
                renamed = True
        else:
            if len(mine) >= MAX_PER_USER:
                raise ComputerError(f"컴퓨터는 {MAX_PER_USER}대까지 등록할 수 있어요.", 409)
            computer_id = "pc_" + secrets.token_urlsafe(12)
            position = max((r[0] for r in c.execute("SELECT position FROM app_computers WHERE user_id=?", (user,)).fetchall()), default=0) + 1
            c.execute("INSERT INTO app_computers(id, user_id, name, created, position) VALUES (?,?,?,?,?)",
                      (computer_id, user, name, users._now(), position))
            users._log(c, user, "app_computer_add", user, f"{computer_id} {name}")
    if renamed:
        send_soon(computer_id, {"type": "renamed", "name": name})
    return {"id": computer_id, "name": name}


def get(computer_id):
    with users._conn() as c:
        row = _select(c, "id=?", (computer_id,)).fetchone()
    return _row(row) if row else None


def list_for(user):
    with users._conn() as c:
        rows = _select(c, "user_id=? ORDER BY position", (user,)).fetchall()
    return [{"id": r[0], "name": r[2], "online": r[0] in _links, "last_seen": r[4]} for r in rows]


def remove(user, computer_id):
    """자기 컴퓨터만 지운다. 지웠으면 True."""
    with users._conn() as c:
        if _select(c, "id=? AND user_id=?", (computer_id, user)).fetchone() is None:
            return False
        c.execute("DELETE FROM app_computers WHERE id=?", (computer_id,))
        users._log(c, user, "app_computer_remove", user, computer_id)
    return True


def by_name(user, name):
    """이 사람의 컴퓨터 하나. 대소문자는 가리지 않는다."""
    wanted = unicodedata.normalize("NFC", name).casefold()
    with users._conn() as c:
        rows = _select(c, "user_id=?", (user,)).fetchall()
    return next((_row(r) for r in rows if r[2].casefold() == wanted), None)


def rename(user, computer_id, new):
    """이름을 바꾸고 새 이름을 돌려준다. 연결돼 있으면 앱에 알린다."""
    new = clean_name(new)
    same = by_name(user, new)
    if same and same["id"] != computer_id:
        raise ComputerError("이미 있는 이름입니다.", 409)
    with users._conn() as c:
        if c.execute("UPDATE app_computers SET name=? WHERE id=? AND user_id=?", (new, computer_id, user)).rowcount == 0:
            raise ComputerError("등록되지 않은 컴퓨터입니다.", 404)
        users._log(c, user, "app_computer_rename", user, f"{computer_id} {new}")
    send_soon(computer_id, {"type": "renamed", "name": new})
    return new


def default_of(user):
    """기본 컴퓨터. 정하지 않았거나 지워졌으면 처음 등록한 것. 없으면 None."""
    saved = users.get_setting(user, DEFAULT_KEY)
    rows = list_for(user)
    return next((r for r in rows if r["id"] == saved), rows[0] if rows else None)


def set_default(user, computer_id):
    users.set_setting(user, DEFAULT_KEY, computer_id)


def status_word(computer_id):
    return "파일 연결됨" if computer_id in _links else "파일 앱 꺼짐"


def send_soon(computer_id, message, close_code=None):
    """스레드에서 연결로 메시지를 보낸다. 연결이 없으면 아무것도 안 한다 (다음 연결이나 ping 때 검사한다)."""
    link = _links.get(computer_id)
    if link is None or _loop is None or _loop.is_closed():
        return

    async def run():
        try:
            await link.send_json(message)
            if close_code:
                await link.close(close_code)
        except Exception:  # 이미 끊긴 연결
            pass

    asyncio.run_coroutine_threadsafe(run(), _loop)


def designate(user, channel_id, name):
    """파일 명령을 받을 채널로 지정한다. 안내 문구를 돌려준다."""
    with users._conn() as c:
        if c.execute("SELECT 1 FROM managed_channels WHERE channel_id=?", (channel_id,)).fetchone():
            raise ComputerError("`!채널관리` 채널에는 지정할 수 없어요.")
        if c.execute("SELECT 1 FROM app_file_channels WHERE channel_id=? AND user_id=?", (channel_id, user)).fetchone():
            return "이미 지정한 채널이에요."
        c.execute("INSERT INTO app_file_channels(channel_id, user_id, name, created) VALUES (?,?,?,?)",
                  (channel_id, user, name, users._now()))
    return f"채널 {channel_id}을(를) 지정했어요. 내 파일 명령만 받고, 인증 코드는 DM으로 받아요."


def release(user, channel_id):
    with users._conn() as c:
        n = c.execute("DELETE FROM app_file_channels WHERE channel_id=? AND user_id=?", (channel_id, user)).rowcount
    return "지정을 해제했어요." if n else "지정한 채널이 아니에요."


def channel_entries(user):
    """기존 이름과 관리에 필요한 채널 ID를 함께 돌려준다."""
    with users._conn() as c:
        return [{"id": r[0], "name": r[1]} for r in c.execute(
            "SELECT channel_id, name FROM app_file_channels WHERE user_id=? ORDER BY created, name", (user,)).fetchall()]


def channels_of(user):
    return [row["name"] for row in channel_entries(user)]


def is_designated(user, channel_id):
    with users._conn() as c:
        return c.execute("SELECT 1 FROM app_file_channels WHERE channel_id=? AND user_id=?", (channel_id, user)).fetchone() is not None


def record_sent(user, channel_id, message_id, name, days):
    """비서 봇이 올린 파일 메시지. 번호와 기한만 저장한다 (파일은 저장하지 않는다)."""
    days = min(max(int(days), 1), 365)
    with users._conn() as c:
        c.execute("INSERT INTO app_sent_files(message_id, channel_id, user_id, name, delete_at) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(message_id) DO NOTHING",
                  (message_id, channel_id, user, name, users._now() + timedelta(days=days)))


def due_sent(limit=50):
    with users._conn() as c:
        rows = c.execute("SELECT message_id, channel_id FROM app_sent_files WHERE delete_at<=? ORDER BY delete_at LIMIT ?",
                         (users._now(), limit)).fetchall()
    return [{"message_id": r[0], "channel_id": r[1]} for r in rows]


def forget_sent(message_ids):
    with users._conn() as c:
        for message_id in message_ids:
            c.execute("DELETE FROM app_sent_files WHERE message_id=?", (message_id,))


def touch(computer_id):
    with users._conn() as c:
        c.execute("UPDATE app_computers SET last_seen=? WHERE id=?", (users._now(), computer_id))


def attach(computer_id, link):
    """지금 연결로 등록하고, 밀려난 이전 연결을 돌려준다."""
    global _loop
    _loop = asyncio.get_running_loop()
    old = _links.get(computer_id)
    _links[computer_id] = link
    return old


def detach(computer_id, link):
    """이 연결이 아직 지금 연결일 때만 뺀다 (새 연결이 밀어냈으면 그대로 둔다)."""
    if _links.get(computer_id) is link:
        del _links[computer_id]
        return True
    return False


def link_of(computer_id):
    return _links.get(computer_id)
