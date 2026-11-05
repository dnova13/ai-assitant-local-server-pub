"""디스코드 메시지에 답할지 정하는 규칙 (디스코드 라이브러리 없이 시험한다).
- DM: 모든 메시지. 등록한 서버(DISCORD_CHAT_GUILDS)와 채널(DISCORD_CHAT_CHANNELS)도 문장까지 받는다.
- 그 밖의 채널: '!'로 시작하는 메시지만.
연결되지 않은 사람의 메시지는 여기서 거르지 않고 입구가 무시한다 (internal_message)."""
import re

from core import config

MENTION = r"^<@!?\d+>\s*"  # 문장 맨 앞의 멘션 (모델에게 보내지 않는다)


def is_chat_place(guild_id, channel_ids):
    """문장으로 대화하는 곳인지. channel_ids 는 채널과 (스레드라면) 그 부모 채널."""
    return (str(guild_id) in config.DISCORD_CHAT_GUILDS
            or any(str(c) in config.DISCORD_CHAT_CHANNELS for c in channel_ids if c is not None))


def shows_rows(is_dm, guild_id=None, channel_ids=()):
    """DB 행 내용과 내부 주소를 다뤄도 되는 곳인지: DM 과 등록한 서버·채널."""
    return is_dm or is_chat_place(guild_id, channel_ids)


def message_text(content, is_dm, guild_id=None, channel_ids=()):
    """비서에게 보낼 글자. 답하지 않을 메시지면 None."""
    text = content.strip()
    if text.startswith("!") or (text and is_dm):
        return text
    if text and is_chat_place(guild_id, channel_ids):
        return re.sub(MENTION, "", text) or None
    return None


FILE_CODE = r"^인증\s*\d{3}\s?\d{3}$"  # 채널에 잘못 친 인증 코드: 서버가 지울지 정한다


class FileWaits:
    """채널에서 앱이 답을 기다리는 사람. 이때만 `!` 없는 말을 서버로 보낸다."""

    def __init__(self, seconds=60):
        self.seconds, self.until = seconds, {}

    def set(self, channel_id, author_id, now):
        self.until[(channel_id, author_id)] = now + self.seconds

    def clear(self, channel_id, author_id):
        self.until.pop((channel_id, author_id), None)

    def active(self, channel_id, author_id, now):
        return self.until.get((channel_id, author_id), 0) > now


def is_discord_command(content):
    """채널 설정 명령은 관리 채널에서도 일반 비서 입구로 전달한다."""
    return bool(re.match(r"^!?디스코드(?:\s|$)", content.strip()))


def file_text(content, waiting):
    """비서에게 보내지 않는 채널 말 중 파일 쪽으로 보낼 글자, 없으면 None."""
    text = content.strip()
    if text and (waiting or is_discord_command(text) or re.match(FILE_CODE, text) or re.match(r"^파일(\s|$)", text)):
        return text
    return None


class Backlog:
    """관리 채널에서 답할 말의 대기열. GPU 가 하나라 넘치면 오래된 것부터 버린다 (버린 수를 돌려준다)."""

    def __init__(self, limit=5):
        self.limit, self.items = limit, []

    def push(self, item):
        self.items.append(item)
        dropped = max(0, len(self.items) - self.limit)
        del self.items[:dropped]
        return dropped

    def pop(self):
        return self.items.pop(0) if self.items else None


def is_channel_command(content):
    """관리 채널에서도 받는 명령 (!채널관리)인지."""
    return bool(re.match(r"[!/]\s*채널관리(\s|$)", content.strip()))


def channel_word(content):
    """!채널관리 뒤 첫 단어. 다른 채널을 이름으로 고를 때 쓴다."""
    words = content.strip().lstrip("!/").split()
    return words[1] if len(words) > 1 and words[0] == "채널관리" else None


def attachment_name(title, filename):
    """첨부파일의 원래 이름. 디스코드의 filename 은 영어와 숫자만 남긴 값일 수 있어 title 을 먼저 쓴다.
    title 에 확장자가 없으면 filename 의 확장자를 붙인다."""
    title = (title or "").strip()
    if not title:
        return filename
    extension = "." + filename.rsplit(".", 1)[1] if "." in filename else ""
    return title if not extension or title.lower().endswith(extension.lower()) else title + extension


def split_message(text, limit=1900):
    """디스코드 한 메시지(2000자)에 맞게 줄 단위로 나눈다. 코드 블록이 나뉘면 메시지마다 닫고 다시 연다."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    out, fenced = [], False
    for chunk in (c.rstrip("\n") for c in chunks if c.strip()):
        was_fenced = fenced
        fenced ^= sum(line.startswith("```") for line in chunk.split("\n")) % 2 == 1
        out.append(("```\n" if was_fenced else "") + chunk + ("\n```" if fenced else ""))
    return out or ["(빈 응답)"]
