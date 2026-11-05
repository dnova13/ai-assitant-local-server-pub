"""디스코드 채널 관리 (README.md). 지정한 채널에서 평소 기능을 막고 대화하듯 메시지와 파일을 찾고 지운다.
- 모델은 뜻만 읽는다 (동작, 찾는 말, 사람). 날짜와 종류, 조회, 삭제 확인은 코드가 한다. 모델 답만으로는 아무것도 지우지 않는다.
- 단서 단어가 없는 말은 모델을 부르지 않고 답하지 않는다. 평소 분류기와 따로 있어 다른 채널에 영향이 없다.
- 찾기는 누구나, 삭제는 비서 DB 의 관리자만 한다. 본문은 지정한 채널 것만 저장하고, 해제하면 함께 지운다."""
import json
import logging
import re
import threading
import time
from datetime import timedelta

from clients import db
from core import users
from features import schedule

log = logging.getLogger("assistant.channels")
ADD_WORDS = {"지정", "등록"}
REMOVE_WORDS = {"해제", "삭제"}
FIND_LIMIT = 10
FIND_MAX = 20
SEARCH_CAP = 1000
MESSAGE_ALL_LIMIT = 100  # "다 보여줘"가 이보다 많으면 파일로 보낸다
MEMORY_SECONDS = 600  # 직전 찾기 조건을 기억하는 시간
DELETE_LIMIT = 50
CONFIRM_SECONDS = 60
PREVIEW = 5
TEXT_LIMIT = 4000
MODEL_TEXT_LIMIT = 500
ACTIONS = ("찾기", "삭제", "아님")
CLUE = re.compile(r"찾|보여|어디|검색|목록|지워|지우|지운|삭제|없애|사진|이미지|그림|동영상|영상|파일|첨부|pdf|zip|docx?|xlsx?|pptx?|hwp", re.I)
DELETE_WORD = re.compile(r"지워|지우|지운|삭제|없애")
ALL_WORD = re.compile(r"다\s*(지워|지우|삭제|없애)|전부|모두|전체")
MENTION = re.compile(r"<@!?(\d+)>")
COUNT_WORD = re.compile(r"(\d+)\s*(?:개|장)")
YES_WORDS = {"네", "예", "응", "어", "확인", "yes", "y", "ㅇㅇ"}
EXT = re.compile(r"\b(pdf|zip|docx?|xlsx?|pptx?|hwp|txt|csv|png|jpe?g|gif|mp4|mov)\b", re.I)
STOP_WORDS = {"사진", "이미지", "그림", "동영상", "영상", "파일", "첨부", "첨부파일", "메시지", "글", "찾아줘", "찾아", "보여줘", "보여",
              "지워줘", "지워", "삭제해줘", "삭제", "올린", "보낸", "준", "것", "거", "다", "전부", "모두", "전체", "최근", "뭐", "이전", "이후"}
SHOW_ALL = re.compile(r"(다|전부|모두|전체)\s*(보여|찾아)")
BARE_MORE = re.compile(r"(그거\s*)?(다|더|전부|모두|전체)\s*(보여\s*줘|보여\s*주세요|보여|찾아\s*줘)[\s.!~?]*")
ASK_DATE = "언제 올린 것을 볼까요? 예: 이번 달 사진 다 보여줘"
_CLOCK = r"(오전|오후|아침|새벽|낮|저녁|밤)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분|\s*(반))?"
TIME_RANGE = re.compile(_CLOCK + r"\s*(?:부터|에서)\s*" + _CLOCK + r"(?:\s*까지)?")
TIME_ONE = re.compile(_CLOCK + r"\s*(이전|전|까지|이후|후|부터)")
USAGE = """채널 관리 (관리자만)
  !채널관리 지정 (또는 등록)            이 채널을 관리 채널로
  !채널관리 해제 (또는 삭제)            평소 채널로 (저장한 내용도 지움, 한 번 확인)
  !채널관리 <채널이름> 지정 (또는 등록)  다른 채널을 지정. #채널 도 됨
  !채널관리 <채널이름> 해제 (또는 삭제)
  !채널관리 목록                       지정한 채널 보기
관리 채널에서는 평소 기능이 멈추고, 대화하듯 찾고 지워요. 찾기는 누구나, 삭제는 관리자만 해요."""
PROMPT = """너는 디스코드 채널 관리 요청을 읽는 도구다. 아래 JSON 하나만 출력해. 설명은 쓰지 마.
{"동작": "찾기 또는 삭제 또는 아님", "찾는말": "찾을 단어 또는 null", "사람": "올린 사람 이름 또는 null"}
- 찾기: 채널의 메시지, 파일, 사진을 찾거나 보여 달라는 요청.
- 삭제: 채널의 메시지나 파일을 지워 달라는 요청.
- 아님: 그 밖의 말. 사람끼리의 대화, 날씨, 메모, 일정, 잡담.
- 찾는말과 사람은 문장에 있는 글자 그대로 쓰고, 없으면 null. 조사는 뺀다. 날짜와 파일 종류는 찾는말에 넣지 않는다.
예: "지난주에 민수가 올린 견적서 찾아줘" -> {"동작":"찾기","찾는말":"견적서","사람":"민수"}
예: "어제 사진 보여줘" -> {"동작":"찾기","찾는말":null,"사람":null}
예: "민수 광고 글 지워줘" -> {"동작":"삭제","찾는말":"광고","사람":"민수"}
예: "오늘 오후 1시 이전 올린 사진" -> {"동작":"찾기","찾는말":null,"사람":null}
예: "파일 뭐 올렸어?" -> {"동작":"찾기","찾는말":null,"사람":null}
예: "이번 달 파일 다 보여줘" -> {"동작":"찾기","찾는말":null,"사람":null}
예: "사진 진짜 잘 나왔다" -> {"동작":"아님","찾는말":null,"사람":null}
예: "그 파일 어디 갔는지 모르겠네 ㅋㅋ" -> {"동작":"아님","찾는말":null,"사람":null}
예: "내일 날씨 어때?" -> {"동작":"아님","찾는말":null,"사람":null}"""

_pending = {}  # (채널, 보낸 사람) -> (지울 메시지들, 만료시각)
_memory = {}  # (채널, 보낸 사람) -> (직전 찾기 조건, 만료시각)
_lock = threading.Lock()


# --- 지정 ---
def _is_managed(channel_id):
    with db.connect() as c:
        return bool(c.execute("SELECT 1 FROM managed_channels WHERE channel_id=?", (channel_id,)).fetchone())


def managed():
    """[{"id", "latest"}] 지정한 채널과 저장한 가장 최근 메시지 시각 (봇이 놓친 글을 채울 때 쓴다)."""
    with db.connect() as c:
        rows = c.execute("SELECT m.channel_id, (SELECT MAX(at) FROM channel_messages x WHERE x.channel_id=m.channel_id) "
                         "FROM managed_channels m").fetchall()
    return [{"id": r[0], "latest": r[1].isoformat() if hasattr(r[1], "isoformat") else r[1]} for r in rows]


def _register(target):
    with db.connect() as c:
        done = c.execute("INSERT INTO managed_channels(channel_id, guild, name, created) VALUES (?,?,?,?) "
                         "ON CONFLICT (channel_id) DO NOTHING",
                         (target["id"], target.get("guild") or "", target.get("name") or "", schedule.now())).rowcount
    name = target.get("name") or target["id"]
    if not done:
        return f"#{name}은(는) 이미 관리 채널이에요."
    return (f"#{name}을(를) 관리 채널로 지정했어요. 평소 기능이 멈추고, 대화하듯 찾고 지울 수 있어요.\n"
            "이전 글을 불러오고, 앞으로의 글과 파일 정보를 이 서버 DB 에 저장해요. 채널 사람들에게도 알려 주세요.")


def _counts(channel_id):
    with db.connect() as c:
        return (c.execute("SELECT COUNT(*) FROM channel_messages WHERE channel_id=?", (channel_id,)).fetchone()[0],
                c.execute("SELECT COUNT(*) FROM channel_files WHERE channel_id=?", (channel_id,)).fetchone()[0])


def _release(target, confirmed, again):
    if not _is_managed(target["id"]):
        return f"#{target.get('name') or target['id']}은(는) 관리 채널이 아니에요."
    messages, files = _counts(target["id"])
    name = target.get("name") or target["id"]
    if not confirmed:
        return (f"#{name} 관리를 해제하면 저장한 글 {messages}개와 파일 정보 {files}개도 이 서버 DB 에서 지워요. "
                f"디스코드의 원본은 그대로예요. 진행하려면: {again} 확인")
    with db.connect() as c:
        for table in ("channel_files", "channel_messages", "managed_channels"):
            c.execute(f"DELETE FROM {table} WHERE channel_id=?", (target["id"],))
    return f"#{name} 관리를 해제하고 저장한 내용을 지웠어요."


def list_text():
    with db.connect() as c:
        rows = c.execute("SELECT channel_id, guild, name, created FROM managed_channels ORDER BY created").fetchall()
    if not rows:
        return "관리 채널이 없어요. 지정하려면: !채널관리 지정"
    lines = ["관리 채널"]
    for channel_id, guild, name, created in rows:
        messages, files = _counts(channel_id)
        where = f" ({guild})" if guild else ""
        lines.append(f"- #{name or channel_id}{where}: {created:%Y-%m-%d} 지정, 글 {messages}개, 파일 {files}개")
    return "\n".join(lines)


def _stored_targets(word):
    wanted = re.fullmatch(r"<#(\d+)>", word)
    with db.connect() as c:
        rows = c.execute("SELECT channel_id, guild, name FROM managed_channels").fetchall()
    name = word.lstrip("#")
    return [{"id": r[0], "guild": r[1], "name": r[2]} for r in rows if (r[0] == wanted.group(1) if wanted else r[2] == name)]


def _pick_target(word, targets, removing):
    """이름 -> 채널 하나. 봇이 찾은 후보를 쓰고, 해제는 저장된 이름도 본다. 없거나 여럿이면 (None, 안내)."""
    found = list(targets or [])
    if removing and not found:
        found = _stored_targets(word)
    if not found:
        return None, f"'{word}' 채널을 찾지 못했어요. 이름과 봇의 채널 보기 권한을 확인해 주세요."
    if len(found) > 1:
        names = ", ".join(f"#{t.get('name')} ({t.get('guild') or '서버 모름'})" for t in found)
        return None, f"같은 이름의 채널이 여럿이에요: {names}. 원하는 채널을 #으로 골라 다시 쳐 주세요."
    return found[0], None


def command(rest, channel=None, targets=None):
    """!채널관리 뒤의 글자. 권한은 입구에서 먼저 본다."""
    words = rest.split()
    if not words or words[0] in ("도움말", "사용법"):
        return USAGE
    if words == ["목록"]:
        return list_text()
    head = words[0]
    if head in ADD_WORDS | REMOVE_WORDS:
        if channel is None:
            return "채널 안에서 쓰거나 이름을 함께 써 주세요. 예: !채널관리 자료실 등록"
        if head in ADD_WORDS and len(words) == 1:
            return _register(channel)
        if head in REMOVE_WORDS and words[1:] in ([], ["확인"]):
            return _release(channel, len(words) == 2, f"!채널관리 {head}")
        return USAGE
    action = words[1] if len(words) > 1 else ""
    if action in ADD_WORDS and len(words) == 2:
        target, message = _pick_target(head, targets, False)
        return message or _register(target)
    if action in REMOVE_WORDS and words[2:] in ([], ["확인"]):
        target, message = _pick_target(head, targets, True)
        return message or _release(target, len(words) == 3, f"!채널관리 {head} {action}")
    return USAGE


# --- 저장 ---
def _clean_row(row):
    return (str(row["id"]), str(row["channel_id"]), str(row.get("author_id") or ""), str(row.get("author_name") or ""),
            str(row.get("text") or "")[:TEXT_LIMIT], str(row.get("link") or ""), row["at"])


def store(rows, live=False):
    """봇이 넘긴 글과 첨부파일 정보를 저장한다 (지정한 채널만). live: 방금 온 글 하나면, 읽어 볼 말인지 돌려준다."""
    needs_reply = False
    managed_ids = {m["id"] for m in managed()}
    with db.connect() as c:
        for row in rows:
            if str(row["channel_id"]) not in managed_ids:
                continue
            values = _clean_row(row)
            c.execute("INSERT INTO channel_messages(message_id, channel_id, author_id, author_name, text, link, at) "
                      "VALUES (?,?,?,?,?,?,?) ON CONFLICT (message_id) DO UPDATE SET text=excluded.text", values)
            c.execute("DELETE FROM channel_files WHERE message_id=?", (values[0],))
            for f in row.get("files") or []:
                c.insert("INSERT INTO channel_files(message_id, channel_id, author_id, author_name, filename, kind, size, link, at) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (values[0], values[1], values[2], values[3], str(f.get("filename") or "")[:300],
                                                        str(f.get("kind") or "file"), int(f.get("size") or 0), values[5], values[6]))
            if live:
                with _lock:
                    waiting = (values[1], values[2]) in _pending
                needs_reply = waiting or bool(CLUE.search(values[4]))
    return needs_reply


def edit(message_id, text):
    with db.connect() as c:
        c.execute("UPDATE channel_messages SET text=? WHERE message_id=?", (str(text)[:TEXT_LIMIT], str(message_id)))


def forget(ids):
    """디스코드에서 지운 글을 DB 에서도 지운다."""
    with db.connect() as c:
        for message_id in ids:
            c.execute("DELETE FROM channel_files WHERE message_id=?", (str(message_id),))
            c.execute("DELETE FROM channel_messages WHERE message_id=?", (str(message_id),))


# --- 문장 읽기 ---
def date_range(text, current):
    """문장의 날짜 표현 -> (시작, 끝, 표현). 없으면 None. 끝은 포함하지 않는다."""
    day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    previous_month = (month - timedelta(days=1)).replace(day=1)
    next_month = (month + timedelta(days=32)).replace(day=1)
    fixed = ((r"오늘", day, day + timedelta(days=1)), (r"어제", day - timedelta(days=1), day),
             (r"그저께|그제", day - timedelta(days=2), day - timedelta(days=1)),
             (r"이번\s*주", week, week + timedelta(days=7)), (r"(지난|저번)\s*주", week - timedelta(days=7), week),
             (r"이번\s*달", month, next_month), (r"(지난|저번)\s*달", previous_month, month))
    for pattern, start, end in fixed:
        found = re.search(pattern, text)
        if found:
            return start, end, found.group(0)
    found = re.search(r"(\d+)\s*일\s*전", text)
    if found:
        return day - timedelta(days=int(found.group(1))), current + timedelta(seconds=1), found.group(0)
    found = re.search(r"(\d{1,2})\s*월(?:\s*(\d{1,2})\s*일)?", text)
    if found and 1 <= int(found.group(1)) <= 12:
        m, d = int(found.group(1)), int(found.group(2)) if found.group(2) else None
        try:
            start = day.replace(month=m, day=d or 1)
        except ValueError:
            return None
        if start > day:
            start = start.replace(year=start.year - 1)
        end = start + timedelta(days=1) if d else (start + timedelta(days=32)).replace(day=1)
        return start, end, found.group(0)
    return None


def _hour(part, hour, half_or_minute):
    """시각 글자 -> (시, 분). 오전·오후가 없는 1~6시는 일정과 같이 오후로 본다. 틀리면 None."""
    if hour > 24 or half_or_minute > 59:
        return None
    if part in ("오후", "낮", "저녁", "밤") and hour < 12:
        hour += 12
    elif part in ("오전", "아침", "새벽") and hour == 12:
        hour = 0
    elif not part and 1 <= hour <= 6:
        hour += 12
    return None if hour > 23 else (hour, half_or_minute)


def _clock(part, hour, minute, half):
    return _hour(part, int(hour), 30 if half else int(minute or 0))


def time_range(text, day):
    """그날의 시각 조건 -> (시작, 끝, 표시). 없으면 None."""
    found = TIME_RANGE.search(text)
    next_day = day + timedelta(days=1)
    if found:
        first, last = _clock(*found.groups()[:4]), _clock(*found.groups()[4:8])
        if first and last:
            return (day.replace(hour=first[0], minute=first[1]), day.replace(hour=last[0], minute=last[1]) + timedelta(minutes=1),
                    f"{first[0]:02d}:{first[1]:02d}~{last[0]:02d}:{last[1]:02d}")
        return None
    found = TIME_ONE.search(text)
    if not found:
        return None
    at = _clock(*found.groups()[:4])
    if not at:
        return None
    moment = day.replace(hour=at[0], minute=at[1])
    label = f"{at[0]:02d}:{at[1]:02d}"
    word = found.group(5)
    if word in ("이전", "전"):
        return day, moment, f"{label} 이전"
    if word == "까지":
        return day, moment + timedelta(minutes=1), f"{label}까지"
    return moment, next_day, f"{label} 이후"


def _kind_of(text):
    if re.search(r"사진|이미지|그림", text):
        return "image"
    if re.search(r"동영상|영상", text):
        return "video"
    return None


def conditions(text, result, current):
    """문장과 모델 결과 -> 찾을 조건."""
    author = MENTION.search(text)
    ext = EXT.search(text)
    period = date_range(text, current)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    day = period[0] if period and period[1] - period[0] == timedelta(days=1) else (today if not period else None)
    date_word = period[2] if period else None
    clock = time_range(text, day) if day is not None else None  # 시각은 하루 안에서만 본다
    if clock:
        period = (clock[0], clock[1], f"{period[2]} {clock[2]}" if period else f"오늘 {clock[2]}")
    word = result.get("찾는말")
    if word:
        if date_word:
            word = word.replace(date_word, " ")
        word = TIME_ONE.sub(" ", TIME_RANGE.sub(" ", word))
        word = EXT.sub(" ", COUNT_WORD.sub(" ", word))
        word = " ".join(w for w in word.split() if w not in STOP_WORDS) or None
    return {"word": word, "person": result.get("사람"), "author_id": author.group(1) if author else None,
            "kind": _kind_of(text), "ext": ext.group(1).lower() if ext else None,
            "file_only": bool(_kind_of(text) or ext or re.search(r"파일|첨부", text)),
            "start": period[0] if period else None, "end": period[1] if period else None,
            "period": period[2] if period else None}


def parse(raw, text):
    """모델 응답 -> {"동작", "찾는말", "사람"}. 형식이 틀리면 None. 문장에 없는 글자는 버리고, 삭제는 문장에 지우라는 말이 있어야 한다."""
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("동작") not in ACTIONS:
        return None

    def inside(value):
        ok = isinstance(value, str) and value.strip() and "".join(value.split()) in "".join(text.split())
        return value.strip() if ok else None
    action = d["동작"]
    if action == "삭제" and not DELETE_WORD.search(text):
        action = "아님"
    return {"동작": action, "찾는말": inside(d.get("찾는말")), "사람": inside(d.get("사람"))}


def classify(text, lock, model=None, timeout=120):
    """전용 지시문으로 묻는다. 형식이 틀리면 한 번 더 묻고, 그래도 틀리면 None. 접속 오류는 classifier.ClassifierError."""
    from core import classifier  # classifier 가 features 를 불러오므로 여기서 불러온다
    short = text[:MODEL_TEXT_LIMIT]
    for _ in range(2):
        with lock:
            raw, _used = classifier.ask_system(PROMPT, MENTION.sub(" ", short), model, timeout)
        result = parse(raw, short)
        if result is not None:
            return result
    return None


# --- 찾기와 삭제 ---
def _filters(channel_id, f, prefix=""):
    where, params = [f"{prefix}channel_id=?"], [channel_id]
    if f["start"] is not None:
        where.append(f"{prefix}at>=? AND {prefix}at<?")
        params += [f["start"], f["end"]]
    if f["author_id"]:
        where.append(f"{prefix}author_id=?")
        params.append(f["author_id"])
    if f["person"]:
        where.append(rf"LOWER({prefix}author_name) LIKE ? ESCAPE '\'")
        params.append(_like(f["person"]))
    return where, params


def _like(word):
    return "%" + word.lower().replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_") + "%"


def search(channel_id, f):
    """조건에 맞는 파일과 글을 최근 순서로 [(메시지 번호, 글자, 올린 사람, 시각, 링크)]. 파일은 하나마다 한 줄이다.
    파일은 이름이나 함께 쓴 글로 찾는다. 글 줄은 그 메시지의 파일이 이미 나왔으면 넣지 않는다. 많아도 SEARCH_CAP 까지만 본다."""
    with db.connect() as c:
        where, params = _filters(channel_id, f, "f.")
        if f["word"]:
            where.append(r"(LOWER(f.filename) LIKE ? ESCAPE '\' OR LOWER(m.text) LIKE ? ESCAPE '\')")
            params += [_like(f["word"]), _like(f["word"])]
        if f["kind"]:
            where.append("f.kind=?")
            params.append(f["kind"])
        if f["ext"]:
            where.append(r"LOWER(f.filename) LIKE ? ESCAPE '\'")
            params.append("%." + f["ext"])
        rows = c.execute("SELECT f.message_id, f.filename, f.author_name, f.at, f.link FROM channel_files f "
                         f"JOIN channel_messages m ON m.message_id=f.message_id WHERE {' AND '.join(where)} "
                         "ORDER BY f.at DESC, f.id LIMIT ?", (*params, SEARCH_CAP)).fetchall()
        rows = [tuple(r) for r in rows]
        if not f["file_only"]:
            where, params = _filters(channel_id, f)
            where.append("text<>''")
            if f["word"]:
                where.append(r"LOWER(text) LIKE ? ESCAPE '\'")
                params.append(_like(f["word"]))
            shown = {r[0] for r in rows}
            found = c.execute(f"SELECT message_id, text, author_name, at, link FROM channel_messages WHERE {' AND '.join(where)} "
                              "ORDER BY at DESC LIMIT ?", (*params, SEARCH_CAP)).fetchall()
            rows += [tuple(r) for r in found if r[0] not in shown]
    return sorted(rows, key=lambda r: r[3], reverse=True)[:SEARCH_CAP]


def _attachment_count(ids):
    with db.connect() as c:
        return sum(c.execute("SELECT COUNT(*) FROM channel_files WHERE message_id=?", (i,)).fetchone()[0] for i in ids)


def _condition_text(f):
    parts = [f["word"], f["person"], "멘션한 사람" if f["author_id"] else None, f["period"], f["ext"],
             {"image": "사진", "video": "동영상"}.get(f["kind"]), "파일" if f["file_only"] and not (f["kind"] or f["ext"]) else None]
    return ", ".join(p for p in parts if p) or "없음"


def _has_condition(f):
    return bool(f["word"] or f["person"] or f["author_id"] or f["start"] is not None or f["file_only"])


def _line(i, row):
    excerpt = " ".join(row[1].split())
    excerpt = excerpt[:60] + "…" if len(excerpt) > 60 else excerpt
    return f"{i}. {excerpt} ({row[2]}, {row[3]:%m-%d})\n   <{row[4]}>"


def _wanted_count(text):
    found = COUNT_WORD.search(text)
    return max(1, min(int(found.group(1)), FIND_MAX)) if found else FIND_LIMIT


def _file_text(rows):
    lines = []
    for i, r in enumerate(rows, 1):
        lines += [f"{i}. {' '.join(r[1].split())} ({r[2]}, {r[3]:%Y-%m-%d %H:%M})", f"   {r[4]}"]
    return "\n".join(lines) + "\n"


def _answer(reply):
    return {"reply": reply, "delete_ids": []}


def _find(channel_id, f, text, show_all=False):
    """{"reply", "delete_ids", "file"}. 다 보여 달라면 날짜가 꼭 있어야 하고, 많으면 목록을 파일로 준다."""
    if not _has_condition(f):
        return _answer("무엇을 찾을까요? 예: 지난주에 올린 견적서 찾아줘")
    if show_all and f["start"] is None:
        return _answer(ASK_DATE)
    rows = search(channel_id, f)
    if not rows:
        return _answer(f"찾지 못했어요. 찾은 조건: {_condition_text(f)}")
    capped = f" {SEARCH_CAP}개까지만 보여 드려요." if len(rows) >= SEARCH_CAP else ""
    total = f"{SEARCH_CAP}개 이상" if capped else f"{len(rows)}개"
    if show_all and len(rows) > MESSAGE_ALL_LIMIT:
        reply = f"🔎 찾은 조건: {_condition_text(f)} (전체 {total}). {MESSAGE_ALL_LIMIT}개가 넘어 목록을 파일로 보내요.{capped}"
        return {**_answer(reply), "file": {"name": "search-results.txt", "text": _file_text(rows)}}
    shown = rows if show_all else rows[:_wanted_count(text)]
    amount = f"전체 {total}" if len(shown) == len(rows) else f"전체 {total} 중 최근 {len(shown)}개"
    lines = [f"🔎 찾은 조건: {_condition_text(f)} ({amount}){capped}"] + [_line(i, r) for i, r in enumerate(shown, 1)]
    if len(shown) < len(rows):
        lines.append("전부 보려면 날짜를 넣어 '이번 달 사진 다 보여줘'처럼 말해 주세요."
                     + (f" '{FIND_MAX}개 보여줘'도 돼요." if len(shown) < FIND_MAX else ""))
    return _answer("\n".join(lines))


def _ask_delete(channel_id, author_id, f, text):
    if not _has_condition(f) and not ALL_WORD.search(text):
        return "어떤 메시지를 지울까요? 예: 어제 올린 광고 지워줘"
    first = {}
    for row in search(channel_id, f):  # 지우는 단위는 메시지다
        first.setdefault(row[0], row)
    rows = list(first.values())[:DELETE_LIMIT]
    if not rows:
        return f"지울 메시지를 찾지 못했어요. 찾은 조건: {_condition_text(f)}"
    ids = [r[0] for r in rows]
    with _lock:
        _pending[(channel_id, author_id)] = (ids, time.monotonic() + CONFIRM_SECONDS)
    files = _attachment_count(ids)
    amount = f"메시지 {len(ids)}개" + (f"(첨부파일 {files}개 포함)" if files else "")
    lines = [f"🗑️ 찾은 조건: {_condition_text(f)}. {amount}를 지울까요? 메시지째 지워져 되돌릴 수 없어요. 네 / 아니오"]
    lines += [_line(i, r) for i, r in enumerate(rows[:PREVIEW], 1)]
    if len(rows) > PREVIEW:
        lines.append(f"... 외 메시지 {len(rows) - PREVIEW}개")
    if len(first) > DELETE_LIMIT:
        lines.append(f"한 번에 메시지 {DELETE_LIMIT}개까지 지워요. 나머지는 다시 요청해 주세요.")
    return "\n".join(lines)


def _is_admin(author_id):
    user = users.resolve("discord", author_id) if author_id else None
    return bool(user and users.is_admin(user))


def request(channel_id, message_id, author_id, text, lock, model=None):
    """관리 채널의 말 하나 처리: {"reply", "delete_ids"}. 답할 말이 아니면 reply 가 None."""
    from core import classifier
    nothing = {"reply": None, "delete_ids": []}
    if not _is_managed(channel_id):
        return nothing
    text = text.strip()
    with _lock:
        waiting = _pending.pop((channel_id, author_id), None)
    if waiting and time.monotonic() <= waiting[1]:
        forget([message_id])
        if text.strip(" .!").lower() in YES_WORDS and _is_admin(author_id):
            return {"reply": None, "delete_ids": waiting[0]}
        return {"reply": "삭제를 취소했어요.", "delete_ids": []}
    if BARE_MORE.fullmatch(text):  # 모델 없이 직전 조건으로 이어서 찾는다
        forget([message_id])
        with _lock:
            remembered = _memory.get((channel_id, author_id))
        if not remembered or time.monotonic() > remembered[1] or remembered[0]["start"] is None:
            return _answer(ASK_DATE)
        return _find(channel_id, remembered[0], text, show_all=True)
    if not CLUE.search(text):
        return nothing
    try:
        result = classify(text, lock, model)
    except classifier.ClassifierError as e:
        return {"reply": f"⚠️ {e}", "delete_ids": []}
    if result is None or result["동작"] == "아님":
        return nothing
    forget([message_id])  # 요청 글은 찾을 내용이 아니다
    f = conditions(text, result, schedule.now())
    if result["동작"] == "찾기":
        with _lock:
            _memory[(channel_id, author_id)] = (f, time.monotonic() + MEMORY_SECONDS)
        return _find(channel_id, f, text, show_all=bool(SHOW_ALL.search(text)))
    if not _is_admin(author_id):
        return {"reply": "메시지 삭제는 관리자만 할 수 있어요.", "delete_ids": []}
    return {"reply": _ask_delete(channel_id, author_id, f, text), "delete_ids": []}
