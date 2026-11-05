"""아침 브리핑: 날씨 + 오늘 일정 + 최근 메모를 한 번에 묶는다. 뉴스는 features/news.py 로 옮겼다 (README.md).
- 값은 모두 코드가 가져온다. 모델은 쓰지 않는다.
- 한 부분이 실패해도 나머지는 보낸다 (그 부분만 "가져오지 못했어요").
- 자동 발송: 사람마다 켜고 시각을 정한다 (사용자 설정 '브리핑시각'). 하루 한 번, 정한 시각부터 SEND_WINDOW 안에서만 보낸다."""
import logging
from datetime import datetime, timedelta

from core import config, users
from features import notes, schedule, weather

log = logging.getLogger("assistant.briefing")
TIME_KEY = "브리핑시각"       # 켠 사람만 값이 있다 (HH:MM)
SENT_KEY = "브리핑보낸날"     # YYYY-MM-DD
PLACE_KEY = "기본위치"        # 날씨와 같은 설정
SEND_WINDOW = timedelta(hours=3)  # 봇이 꺼져 있다 켜져도 이 시간 안이면 보낸다 (늦은 아침 브리핑은 보내지 않음)
MEMO_COUNT = 3


class BriefingError(Exception):
    pass


# --- 묶기 ---
def _section(title, fn):
    try:
        return f"{title}\n{fn()}"
    except Exception as e:  # 한 부분이 실패해도 나머지는 보낸다
        log.warning("브리핑 %s 실패: %s", title, type(e).__name__)
        msg = str(e) if isinstance(e, (weather.WeatherError, BriefingError)) else "가져오지 못했어요."
        return f"{title}\n⚠️ {msg}"


def build(user, current=None):
    """브리핑 글."""
    current = current or schedule.now()
    day = current.date()

    def weather_part():
        return weather.report_place(users.get_setting(user, PLACE_KEY), "오늘")

    def schedule_part():
        rows = schedule.on_day(user, day)
        return schedule.fmt(rows) if rows else "오늘 일정이 없어요."

    def memo_part():
        rows = notes.recent(user, MEMO_COUNT)
        return notes.fmt(rows) if rows else "저장된 메모가 없어요."

    head = f"☀️ {day.month}월 {day.day}일({schedule.WEEKDAYS[day.weekday()]}) 브리핑"
    parts = [_section("🌤️ 날씨", weather_part), _section("📅 오늘 일정(알림)", schedule_part),
             _section("📝 최근 메모", memo_part)]
    return "\n\n".join([head] + parts)


# --- 자동 발송 설정 ---
def parse_hhmm(text):
    """'7:30', '07:30', '7시', '오전 7시 30분' -> 'HH:MM'. 알 수 없으면 BriefingError."""
    try:
        found = schedule.parse_time(text)
    except schedule.ScheduleError as e:
        raise BriefingError(str(e))
    if found is None:
        raise BriefingError("시각을 알 수 없어요. 예: 브리핑 켜기 7:30")
    (h, m), guessed, _ = found
    if guessed and h > 12:  # 아침 브리핑이라 오전·오후가 없으면 오전으로 본다 (일정은 1~6시를 오후로 본다)
        h -= 12
    return f"{h:02d}:{m:02d}"


def turn_on(user, hhmm=None):
    users.set_setting(user, TIME_KEY, hhmm or config.BRIEFING_TIME)
    return users.get_setting(user, TIME_KEY)


def turn_off(user):
    users.set_setting(user, TIME_KEY, "")


def time_of(user):
    return users.get_setting(user, TIME_KEY) or None


def should_send(user, current=None):
    """오늘 브리핑을 보낼 때인가: 켰고, 정한 시각부터 SEND_WINDOW 안이고, 오늘 아직 안 보냄."""
    hhmm = time_of(user)
    if not hhmm:
        return False
    current = current or schedule.now()
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except ValueError:
        return False
    start = datetime(current.year, current.month, current.day, h, m, tzinfo=current.tzinfo)
    return start <= current < start + SEND_WINDOW and users.get_setting(user, SENT_KEY) != current.date().isoformat()


def mark_sent(user, day):
    users.set_setting(user, SENT_KEY, day.isoformat())
