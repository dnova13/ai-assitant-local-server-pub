"""일정 저장소와 날짜·시각 해석 (SQLite, 사용자별).
- 날짜·시각은 코드가 정한다. 날짜 말은 날씨와 같은 규칙(weather.normalize_day)을 쓴다.
- 등록 흐름, 알림 보내기는 비서 본체(assistant.py)와 디스코드 봇이 한다. 여기서는 저장·조회·해석만 한다.
- 알림 방식: 1번은 미리 알림(사용자별 분전 설정의 시점마다) + 그 시각, 2번은 그 시각에만. 분전 설정은 항목별이 아니라 사람마다 하나다.
- 시각은 시간대가 붙은 datetime 으로 다룬다 (저장 형식은 clients/db.py)."""
import math
import re
from datetime import datetime, time, timedelta

from core import users
from clients import db, llm
from features import weather

MAX_TEXT_LEN = 200
MODE_BEFORE, MODE_AT_TIME = 1, 2  # 알림 방식. 이 칸이 없던 때의 일정은 그 시각에만 보냈으므로 2번으로 둔다
OFFSET_KEY = "분전설정"
DEFAULT_OFFSETS = (30, 20, 10, 5)  # 미리 알림 시점 (분). 사용자가 바꾸기 전의 값
MAX_OFFSETS = 10
MAX_OFFSET_MINUTES = 1440
LATE_MINUTES = 5  # 알림 시각보다 이만큼 늦게 보내면 '놓친 일정'으로 안내한다
WEEKDAYS = weather.WEEKDAYS
# 시각: [오전|오후|아침|낮|저녁|밤|새벽] 3시 [30분|반] / [오전|오후] 15:30 / 정오.  뒤의 '에', '까지' 같은 말도 함께 뺀다
PERIOD = r"(오전|오후|아침|낮|저녁|밤|새벽)?\s*"
TIME_PATTERN = (rf"{PERIOD}(?<!\d)(\d{{1,2}})\s*:\s*(\d{{2}})(?!\d)"
                rf"|{PERIOD}(?<!\d)(\d{{1,2}})\s*시(?![간각즌])(?:\s*(반)|\s*(\d{{1,2}})\s*분)?"
                r"|(정오)")
AFTER_TIME = r"\s*(?:에|부터|까지)?(?=\s|$)"
# 상대 시간: 10분 뒤, 30분후, 2시간 뒤, 1시간 30분 뒤, 1시간 반 뒤, 반 시간 뒤. 바로 앞의 날짜 말도 함께 본다 ('3시 30분 뒤'는 상대 시간이 아니다)
RELATIVE_PATTERN = (r"(?:(오늘|지금|내일|낼|모레)\s*)?(?<![\d:시])(?<!시\s)"
                    r"(?:(\d{1,3})\s*시간(?:\s*(반)|\s*(\d{1,4})\s*분)?|(\d{1,4})\s*분|(반)\s*시간)\s*(?:뒤|후)")
# 내용 끝의 요청하는 말 ("빨래 알려줘" -> "빨래"). 이것만 남으면 그대로 둔다
REQUEST_END = (r"\s*(?:(?:나한테|나에게|내게|저한테)\s*)?"
               r"(?:알려\s*(?:줘요|줘라|줘|주세요|줄래|주라)?"
               r"|알림\s*(?:(?:좀\s*)?(?:줘|주세요|해\s*줘|해|보내\s*줘|잡아\s*줘|등록해\s*줘|넣어\s*줘))?"
               r"|(?:일정\s*)?(?:잡아\s*(?:줘|주세요)|등록해\s*(?:줘|주세요)|추가해\s*줘|넣어\s*줘))[.!?~ ]*$")
# "테스트라고" -> "테스트", "빨래 하라고" -> "빨래". "약 먹으라고", "가라고" 같은 동사는 그대로 둔다
QUOTE_END = r"\s*하라고$|\s*(?<![으가오보자])(?:이라고|라고)$"
# 날짜 말이 없을 때 날짜를 말하려 한 것으로 보는 말 (아직 지원하지 않음). 날짜 말이 있으면 내용으로 둔다 ("내일 15시 금요일 발표 준비")
UNSUPPORTED_DATE = r"[월화수목금토일]요일|주말|(?:다다음|다음|이번)\s*주|글피|" + weather.RELATIVE_DAYS_PATTERN
# 요일: 금요일 / 이번주·다음주·다다음주 금요일. '다다다음주', '지난주', '매주'의 요일은 받지 않는다
WEEKDAY_PATTERN = r"(?:(?<!다)(이번|다음|다다음)\s*주\s*|(?<!주)(?<!주\s))([월화수목금토일])요일(?:날|에)?"
WEEK_OFFSET = {"이번": 0, "다음": 1, "다다음": 2}
PM_WORDS = ("오후", "저녁", "밤")
AM_WORDS = ("오전", "아침", "새벽")


class ScheduleError(Exception):
    pass


def _conn():
    return db.connect()


def now():
    """서버 시간대의 지금 (초 단위, 시간대 포함). 시험에서 바꿔 끼운다."""
    return llm.now().replace(microsecond=0)


# --- 해석 ---
def _hour(period, h):
    """(시, 짐작했는지). 오전·오후 말이 없으면 1~6시는 오후, 7~12시는 그대로 두고 짐작했다고 알린다 (일정은 사용자에게 묻는다)."""
    if h > 24 or (h == 24 and period):
        raise ScheduleError(f"{h}시는 시각으로 알 수 없어요. 0~23시나 '오후 3시'처럼 적어 주세요.")
    if h == 24:
        return 0, False
    if period in ("저녁", "밤") and h == 12:  # 자정은 날짜가 헷갈리므로 '내일 0시'처럼 받는다
        raise ScheduleError("밤 12시(자정)는 날짜가 헷갈려요. '내일 0시'처럼 날짜와 함께 적어 주세요.")
    if period in PM_WORDS:
        return (h + 12 if h < 12 else h), False
    if period in AM_WORDS:
        return (0 if h == 12 else h), False
    if period == "낮":
        return (h + 12 if 1 <= h <= 6 else h), False
    if 1 <= h <= 6:
        return h + 12, True
    return h, 7 <= h <= 12


def parse_time(text):
    """글자에서 시각을 찾아 ((시, 분), 짐작했는지, 찾은 범위) 를 돌려준다. 없으면 None."""
    m = re.search(TIME_PATTERN, text)
    if not m:
        return None
    if m.group(8):  # 정오
        hm, guessed = (12, 0), False
    elif m.group(2) is not None:  # 15:30
        h, guessed = _hour(m.group(1), int(m.group(2)))
        hm = (h, int(m.group(3)))
    else:  # 3시 반 / 3시 30분
        h, guessed = _hour(m.group(4), int(m.group(5)))
        hm = (h, 30 if m.group(6) else int(m.group(7) or 0))
    if hm[1] > 59:
        raise ScheduleError(f"{hm[1]}분은 시각으로 알 수 없어요.")
    tail = re.match(AFTER_TIME, text[m.end():])
    return hm, guessed, (m.start(), m.end() + (tail.end() if tail else 0))


def parse_relative(text):
    """글자에서 상대 시간을 찾아 (분, 찾은 범위) 를 돌려준다. 없으면 None. 내일·모레와 함께 쓰면 ScheduleError."""
    m = re.search(RELATIVE_PATTERN, text)
    if not m:
        return None
    if m.group(1) in ("내일", "낼", "모레"):
        raise ScheduleError("날짜와 'N분 뒤'는 같이 쓸 수 없어요. '10분 뒤 빨래'나 '내일 15시 치과'처럼 적어 주세요.")
    if m.group(6):  # 반 시간
        minutes = 30
    elif m.group(2):  # 2시간 / 1시간 반 / 1시간 30분
        minutes = int(m.group(2)) * 60 + (30 if m.group(3) else int(m.group(4) or 0))
    else:  # 10분
        minutes = int(m.group(5))
    if minutes <= 0:
        raise ScheduleError("1분 이상으로 적어 주세요. 예: 일정 10분 뒤 빨래")
    tail = re.match(AFTER_TIME, text[m.end():])
    return minutes, (m.start(), m.end() + (tail.end() if tail else 0))


def has_time(text):
    """시각이나 상대 시간이 있는지 (분류기가 일정 요청인지 볼 때 쓴다). 틀린 시각도 시각을 말하려 한 것으로 본다."""
    try:
        return parse_time(text) is not None or parse_relative(text) is not None
    except ScheduleError:
        return True


def fmt_minutes(minutes):
    """90 -> '1시간 30분'."""
    h, m = divmod(minutes, 60)
    return " ".join(x for x in (f"{h}시간" if h else "", f"{m}분" if m else "") if x)


def _clean_text(t):
    """날짜·시각을 뺀 나머지에서 내용만 남긴다. 요청하는 말과 '~라고', '있다고'를 끝에서 뺀다."""
    content = re.sub(r"\s+", " ", t).strip(" ,.~에")
    content = re.sub(REQUEST_END, "", content) or content
    content = re.sub(r"\s*(?:있다고|있다|있어|있음)$", "", content) or content  # "회의 있다고" -> "회의"
    content = re.sub(QUOTE_END, "", content) or content
    if not content:
        raise ScheduleError("무엇을 하는 일정인지 적어 주세요. 예: 일정 내일 15시 치과")
    if len(content) > MAX_TEXT_LEN:
        raise ScheduleError(f"일정 내용은 {MAX_TEXT_LEN}자까지 적을 수 있어요.")
    return content


def _ask_period(t, start, h, mi):
    """오전·오후 질문. 고르면 시각 앞에 그 말을 넣은 글자로 다시 해석한다."""
    hour = h % 12 or 12
    clock = f"{hour}시" + (f" {mi}분" if mi else "")
    notes = {"오전": "(자정)", "오후": "(낮)"} if hour == 12 else {"오전": "", "오후": ""}
    return {"question": "오전인가요, 오후인가요?",
            "choices": [(w, f"{w} {clock}{notes[w]}", f"{t[:start]}{w} {t[start:]}") for w in ("오전", "오후")]}


def _ask_week(t, name, this_day, last_hm, current):
    """주 말이 없는 요일의 질문. 고르면 요일 앞에 주 말을 넣은 글자로 다시 해석한다. 이번주가 지났으면 다음주인지만 확인한다."""
    def with_week(word):
        start = re.search(WEEKDAY_PATTERN, t).start()
        return f"{t[:start]}{word} {t[start:]}"

    def day_name(word, d):
        return f"{word} {name}요일({d.month}월 {d.day}일)"
    next_day = this_day + timedelta(days=7)
    this_at = datetime(this_day.year, this_day.month, this_day.day, *last_hm, tzinfo=current.tzinfo)
    if this_at <= current:
        return {"question": f"이번주 {name}요일은 지났어요. {day_name('다음주', next_day)}로 등록할까요?",
                "choices": [("다음주", None, with_week("다음주"))]}
    return {"question": "이번주인가요, 다음주인가요?",
            "choices": [(w, day_name(w, d), with_week(w)) for w, d in (("이번주", this_day), ("다음주", next_day))]}


def parse(text, current=None):
    """'내일 15시 치과' -> {"at": datetime, "text": "치과", "guessed": bool, "after": None, "ask": None}. 알 수 없으면 ScheduleError (안내 문구).
    '10분 뒤 빨래'처럼 상대 시간이면 지금에 더하고 "after" 에 '10분'을 넣는다.
    날짜가 없으면 오늘. 지난 시각, 날짜로 바꿀 수 없는 말(주말, 3일 뒤 등)은 안내한다.
    사용자에게 물어야 하면 "ask" 에 {"question", "choices": [(대답 말, 보여 줄 글자, 다시 해석할 글자)]} 를 넣는다:
    주 말이 없는 요일(이때 "at"은 None)을 먼저, 그다음 오전·오후가 없는 시각을 묻는다."""
    current = current or now()
    t = text.strip()
    rel = parse_relative(t)
    if rel is not None:
        minutes, (s, e) = rel
        return {"at": current + timedelta(minutes=minutes), "text": _clean_text(f"{t[:s]} {t[e:]}"), "guessed": False,
                "after": fmt_minutes(minutes), "ask": None}
    found = parse_time(t)
    if found is None:
        raise ScheduleError("몇 시인지 알려 주세요. 예: 일정 내일 15시 치과, 일정 모레 오후 3시 반 미용실, 알림 10분 뒤 빨래")
    (h, mi), guessed, (s, e) = found
    original, t = t, f"{t[:s]} {t[e:]}"
    last_hm = (h % 12 + 12 if guessed else h, mi)  # 오전·오후를 물을 때는 늦은 쪽까지 지나야 지난 시각이다
    m = re.search(weather.DATE_PATTERN, t)
    week = None if m else re.search(WEEKDAY_PATTERN, t)
    if m:
        day = weather.normalize_day(m.group(0), current.date())
        if day is None:
            raise ScheduleError(f"'{m.group(0).strip()}'은(는) 없는 날짜예요.")
        d = weather.to_date(day, current.date())
        t = f"{t[:m.start()]} {t[m.end():]}"
    elif week:
        monday = current.date() - timedelta(days=current.weekday())
        d = monday + timedelta(days=WEEKDAYS.index(week.group(2)) + 7 * WEEK_OFFSET.get(week.group(1), 0))
        t = f"{t[:week.start()]} {t[week.end():]}"
    elif re.search(UNSUPPORTED_DATE, t):
        raise ScheduleError("'주말', '3일 뒤', 다다음주보다 먼 말은 날짜로 알 수 없어요.\n"
                            "오늘·내일·모레, '이번주 금요일', '10월 23일', '23일'처럼 적어 주세요. 예: 일정 10월 23일 15시 치과")
    else:
        d = current.date()
    content = _clean_text(t)
    if week and week.group(1) is None:
        return {"at": None, "text": content, "guessed": guessed, "after": None,
                "ask": _ask_week(original, week.group(2), d, last_hm, current)}
    at = datetime(d.year, d.month, d.day, h, mi, tzinfo=current.tzinfo)
    last = datetime(d.year, d.month, d.day, *last_hm, tzinfo=current.tzinfo)
    if last <= current:
        raise ScheduleError(f"{fmt_at(last)}은(는) 이미 지난 시각이에요. 날짜나 시각을 다시 적어 주세요.")
    return {"at": at, "text": content, "guessed": guessed, "after": None,
            "ask": _ask_period(original, s, h, mi) if guessed else None}


def fmt_at(at):
    at = db.local(at)
    return f"{at.month}월 {at.day}일({WEEKDAYS[at.weekday()]}) {at:%H:%M}"


# --- 저장·조회 ---
def add(user, at, text, mode=MODE_AT_TIME):
    with _conn() as c:
        return c.insert("INSERT INTO events(user_id, at, text, created, mode) VALUES (?,?,?,?,?)",
                        (user, at, text, now(), mode))


def set_mode(user, event_id, mode):
    with _conn() as c:
        return c.execute("UPDATE events SET mode=? WHERE user_id=? AND id=?", (mode, user, event_id)).rowcount


def get(user, event_id):
    """번호로 일정 한 개 (없으면 None). 다른 사용자의 일정은 보이지 않는다."""
    with _conn() as c:
        return c.execute("SELECT id, at, text FROM events WHERE user_id=? AND id=?", (user, event_id)).fetchone()


def upcoming(user, limit=20, current=None):
    """지금부터 앞으로의 일정 (시각 순)."""
    with _conn() as c:
        return c.execute("SELECT id, at, text FROM events WHERE user_id=? AND at>=? ORDER BY at, id LIMIT ?",
                         (user, current or now(), limit)).fetchall()


def on_day(user, day):
    """그날의 일정 (아침 브리핑용, 시각 순). day 는 date."""
    start = datetime.combine(day, time.min, tzinfo=db.zone())
    with _conn() as c:
        return c.execute("SELECT id, at, text FROM events WHERE user_id=? AND at>=? AND at<? ORDER BY at, id",
                         (user, start, start + timedelta(days=1))).fetchall()


def delete(user, event_id):
    with _conn() as c:
        return c.execute("DELETE FROM events WHERE user_id=? AND id=?", (user, event_id)).rowcount


def everything(user):
    """지난 것까지 모든 일정 (시각 순)."""
    with _conn() as c:
        return c.execute("SELECT id, at, text FROM events WHERE user_id=? ORDER BY at, id", (user,)).fetchall()


def past(user, current=None):
    """이미 지난 일정 (시각 순)."""
    with _conn() as c:
        return c.execute("SELECT id, at, text FROM events WHERE user_id=? AND at<? ORDER BY at, id",
                         (user, current or now())).fetchall()


def delete_many(user, ids):
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    with _conn() as c:
        return c.execute(f"DELETE FROM events WHERE user_id=? AND id IN ({','.join('?' * len(ids))})", [user, *ids]).rowcount


def due(current=None):
    """알림을 보낼 때가 된(시각이 지났고 아직 안 보낸) 모든 사용자의 일정. [(id, user, at, text)]"""
    with _conn() as c:
        return c.execute("SELECT id, user_id, at, text FROM events WHERE notified=0 AND at<=? ORDER BY at, id",
                         (current or now(),)).fetchall()


def mark_notified(ids):
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    with _conn() as c:
        return c.execute(f"UPDATE events SET notified=1 WHERE id IN ({','.join('?' * len(ids))})", ids).rowcount


# --- 미리 알림 ---
def offsets_of(user):
    """그 사람의 미리 알림 시점 (분, 큰 것부터)."""
    saved = users.get_setting(user, OFFSET_KEY)
    return [int(x) for x in saved.split(",")] if saved else list(DEFAULT_OFFSETS)


def set_offsets(user, offsets):
    users.set_setting(user, OFFSET_KEY, ",".join(str(x) for x in offsets))


def parse_offsets(text):
    """'30분, 20분, 1시간 전' -> [60, 30, 20]. 알 수 없으면 ScheduleError."""
    found = set()
    for word in re.split(r"[,\s]+", text.strip()):
        if not word or word == "전":
            continue
        m = re.fullmatch(r"(\d{1,4})(분|시간)?전?", word)
        if not m:
            raise ScheduleError(f"'{word}'은(는) 시점으로 알 수 없어요. 예: 분전설정 30분, 20분, 10분, 5분")
        found.add(int(m.group(1)) * (60 if m.group(2) == "시간" else 1))
    if not found:
        raise ScheduleError("시점을 적어 주세요. 예: 분전설정 30분, 20분, 10분, 5분")
    if len(found) > MAX_OFFSETS or min(found) < 1 or max(found) > MAX_OFFSET_MINUTES:
        raise ScheduleError(f"시점은 {MAX_OFFSETS}개까지, 1분에서 24시간 사이로 적어 주세요.")
    return sorted(found, reverse=True)


def fmt_offsets(offsets):
    return ", ".join(f"{fmt_minutes(o)} 전" for o in offsets)


def applicable_offsets(user, at, created=None):
    """등록한 때보다 뒤에 오는 시점만 (15분 뒤의 일정이면 30분 전과 20분 전은 이미 지났다)."""
    return _after_created(offsets_of(user), at, created or now())


def _after_created(offsets, at, created):
    return [o for o in offsets if at - timedelta(minutes=o) > created]


def pre_due(current=None):
    """미리 알림을 보낼 때가 된 일정 [(id, user, at, text, offset)]. 한 일정에 하나만, 지금과 가장 가까운 시점으로 보낸다
    (봇이 꺼져 있었어도 지난 시점을 한꺼번에 보내지 않는다)."""
    current = current or now()
    with _conn() as c:
        rows = c.execute("SELECT id, user_id, at, text, created, last_offset FROM events "
                         "WHERE notified=0 AND mode=? AND at>? ORDER BY at, id",
                         (MODE_BEFORE, current)).fetchall()
    out, offsets = [], {}
    for eid, user, at, text, created, last in rows:
        if user not in offsets:
            offsets[user] = offsets_of(user)
        ready = [o for o in _after_created(offsets[user], at, created)
                 if at - timedelta(minutes=o) <= current and (last is None or o < last)]
        if ready:
            out.append((eid, user, at, text, min(ready)))
    return out


def mark_pre(pairs):
    """보낸 미리 알림을 표시한다. pairs: [(일정 번호, 몇 분 전)]"""
    with _conn() as c:
        return sum(c.execute("UPDATE events SET last_offset=? WHERE id=?", (int(o), int(i))).rowcount for i, o in pairs)


def pre_text(rows, current=None):
    """한 사람의 미리 알림 문구. rows: [(id, at, text)]. 실제로 남은 시간을 적는다."""
    current = current or now()
    lines = []
    for _, at, text in rows:
        dt = db.local(at)
        when = f"{dt:%H:%M}" if dt.date() == db.local(current).date() else fmt_at(dt)
        left = math.ceil((dt - current).total_seconds() / 60)  # 몇 초 지나 보내도 '30분 뒤'로 적는다
        lines.append(f"🔔 미리 알림: {fmt_minutes(left)} 뒤 {when} {text}")
    return "\n".join(lines)


def fmt(rows):
    return "\n".join(f"#{eid} {fmt_at(at)} {text}" for eid, at, text in rows)


def reminder_text(rows, current=None):
    """한 사람의 알림 문구. rows: [(id, at, text)]. 늦게 보내는 것은 '놓친 일정'으로 묶는다."""
    current = current or now()
    on_time, late = [], []
    for _, at, text in rows:
        dt = db.local(at)
        (late if current - dt > timedelta(minutes=LATE_MINUTES) else on_time).append((dt, text))
    lines = [f"⏰ 일정 알림: {dt:%H:%M} {text}" for dt, text in on_time]
    if late:
        lines.append("📋 놓친 일정 (알림을 보내지 못한 동안 지난 일정):")
        lines += [f"- {fmt_at(dt)} {text}" for dt, text in late]
    return "\n".join(lines)
