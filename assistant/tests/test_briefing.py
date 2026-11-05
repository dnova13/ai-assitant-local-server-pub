"""아침 브리핑: 묶기(일부 실패해도 보냄), 뉴스가 없는지, 보낼 때 판단, 명령, 입구 (모델·네트워크·디스코드 없음)"""
import asyncio, os, sys, tempfile, unittest
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from core import apikeys, config, users
from clients import llm
from core import assistant as A
from entrances import reminders
from features import briefing as B
from features import notes, schedule, weather

NOW = datetime(2026, 10, 22, 7, 40)  # 목요일 아침

class Base(unittest.TestCase):
    def setUp(self):
        self.real = (config.DB_PATH, schedule.now, weather.report_place, llm.chat, config.BRIEFING_TIME)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "b.db")
        self.now = NOW
        schedule.now = lambda: self.now
        config.BRIEFING_TIME = "07:30"
        self.weather_calls = []
        weather.report_place = lambda place, day="오늘": self.weather_calls.append(place) or f"📍 {place or '기본'} 날씨"
        self.chats = []
        llm.chat = lambda messages, model=None, timeout=300: self.chats.append(messages) or "• 요약 줄"

    def tearDown(self):
        config.DB_PATH, schedule.now, weather.report_place, llm.chat, config.BRIEFING_TIME = self.real


class Build(Base):
    def test_all_parts(self):
        notes.add("owner", "우유 사기")
        schedule.add("owner", datetime(2026, 10, 22, 15, 0), "치과")
        schedule.add("owner", datetime(2026, 10, 23, 9, 0), "내일 일")
        users.set_setting("owner", B.PLACE_KEY, "부산")
        out = B.build("owner")
        self.assertIn("10월 22일(목) 브리핑", out)
        self.assertIn("📍 부산 날씨", out)
        self.assertIn("15:00 치과", out)
        self.assertNotIn("내일 일", out)
        self.assertIn("우유 사기", out)
        self.assertNotIn("뉴스", out)

    def test_no_place_uses_default_and_empty_parts(self):
        out = B.build("owner")
        self.assertEqual(self.weather_calls, [None])  # None 이면 .env 기본 위치
        self.assertIn("오늘 일정이 없어요", out)
        self.assertIn("저장된 메모가 없어요", out)

    def test_one_part_fails_others_sent(self):
        def bad(place, day="오늘"):
            raise weather.WeatherError("날씨 서버 오류")
        weather.report_place = bad
        out = B.build("owner")
        self.assertIn("⚠️ 날씨 서버 오류", out)
        self.assertIn("오늘 일정이 없어요", out)


class Schedule(Base):
    def test_parse_hhmm(self):
        self.assertEqual(B.parse_hhmm("7:30"), "07:30")
        self.assertEqual(B.parse_hhmm("7시 반"), "07:30")
        self.assertEqual(B.parse_hhmm("오전 6시"), "06:00")
        self.assertEqual(B.parse_hhmm("6:45"), "06:45")  # 오전·오후가 없으면 오전
        self.assertEqual(B.parse_hhmm("저녁 6시"), "18:00")
        with self.assertRaises(B.BriefingError):
            B.parse_hhmm("아침에")

    def test_should_send_window_and_once_a_day(self):
        self.assertFalse(B.should_send("owner"))  # 꺼져 있음
        B.turn_on("owner")
        self.assertTrue(B.should_send("owner"))  # 07:30 이후 07:40
        self.now = datetime(2026, 10, 22, 7, 0)
        self.assertFalse(B.should_send("owner"))  # 아직
        self.now = datetime(2026, 10, 22, 10, 31)
        self.assertFalse(B.should_send("owner"))  # 3시간이 지나면 보내지 않음
        self.now = NOW
        B.mark_sent("owner", NOW.date())
        self.assertFalse(B.should_send("owner"))  # 오늘 보냄
        self.now = datetime(2026, 10, 23, 7, 30)
        self.assertTrue(B.should_send("owner"))  # 다음 날
        B.turn_off("owner")
        self.assertFalse(B.should_send("owner"))

    def test_should_send_with_real_clock_time(self):
        """실제 schedule.now() 는 시간대가 붙어 있다."""
        B.turn_on("owner")
        self.now = NOW.replace(tzinfo=ZoneInfo(config.TIMEZONE))
        self.assertTrue(B.should_send("owner"))
        self.now = self.now.replace(hour=7, minute=0)
        self.assertFalse(B.should_send("owner"))


class Commands(Base):
    def setUp(self):
        super().setUp()
        self.bot = A.Assistant()

    def say(self, text):
        return self.bot.handle("owner", text)

    def test_now(self):
        self.assertIn("브리핑", self.say("!브리핑"))
        self.assertEqual(self.chats, [])  # 모델을 부르지 않는다

    def test_on_off_status(self):
        self.assertIn("07:30", self.say("!브리핑 켜기"))
        self.assertIn("06:45", self.say("!브리핑 켜기 6:45"))
        self.assertIn("매일 06:45", self.say("!브리핑 상태"))
        self.assertIn("껐어요", self.say("!브리핑 끄기"))
        self.assertIn("꺼져 있어요", self.say("!브리핑 상태"))
        self.assertIn("시각을 알 수 없어요", self.say("!브리핑 켜기 아침에"))
        self.assertIn("사용법", self.say("!브리핑 아무말"))


class Send(unittest.TestCase):
    def test_run_briefings(self):
        acked, sent = [], []

        async def fetch():
            return [{"user": "a", "external_ids": ["1"], "date": "2026-10-22", "text": "브"},
                    {"user": "b", "external_ids": ["2"], "date": "2026-10-22", "text": "리"}]

        async def send(ext, text):
            if ext == "2":
                raise RuntimeError("실패")
            sent.append(ext)

        async def ack(user, date):
            acked.append((user, date))
        done = asyncio.run(reminders.run_briefings(fetch, send, ack))
        self.assertEqual((done, sent, acked), (["a"], ["1"], [("a", "2026-10-22")]))  # 실패한 b는 다음에 다시


try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi 가 없어서 건너뜀")
class Api(Base):
    def setUp(self):
        super().setUp()
        from entrances import api_server
        from entrances.api import common
        self.common = common
        self.api = api_server
        self.hosts = set(self.common.LOCAL_HOSTS)
        self.common.LOCAL_HOSTS.add("testclient")
        users.bootstrap([])
        users.add("owner", "family1", "가족")
        users.link("owner", "owner", "discord", "111")
        self.key = apikeys.create("owner", "디스코드 봇", kind="bot")
        self.client = TestClient(api_server.app)

    def tearDown(self):
        self.common.LOCAL_HOSTS.clear()
        self.common.LOCAL_HOSTS.update(self.hosts)
        super().tearDown()

    def post(self, path, body):
        return self.client.post(path, json=body, headers={"Authorization": f"Bearer {self.key}"})

    def test_due_only_turned_on_and_linked_then_ack(self):
        B.turn_on("owner")
        B.turn_on("family1")  # 디스코드 연결이 없어 보내지 않는다
        items = self.post("/internal/briefing/due", {"source": "discord"}).json()["items"]
        self.assertEqual([(i["user"], i["external_ids"], i["date"]) for i in items], [("owner", ["111"], "2026-10-22")])
        self.assertIn("브리핑", items[0]["text"])
        self.assertEqual(self.post("/internal/briefing/ack", {"user": "owner", "date": "2026-10-22"}).json(), {"ok": True})
        self.assertEqual(self.post("/internal/briefing/due", {"source": "discord"}).json()["items"], [])

    def test_ack_bad_input(self):
        self.assertEqual(self.post("/internal/briefing/ack", {"user": "없는사람", "date": "2026-10-22"}).status_code, 400)
        self.assertEqual(self.post("/internal/briefing/ack", {"user": "owner", "date": "10/22"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
