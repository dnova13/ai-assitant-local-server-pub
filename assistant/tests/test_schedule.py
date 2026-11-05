"""일정: 날짜·시각 해석, 저장·조회, 등록·삭제 확인 흐름, 알림 받아 가기와 보내기 (모델·네트워크·디스코드 없음)"""
import asyncio, os, sys, tempfile, time, unittest
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from core import apikeys, config, users
from core import assistant as A
from entrances import reminders
from features import schedule as S

KST = ZoneInfo("Asia/Seoul")


def dt(*args):
    return datetime(*args, tzinfo=KST)


NOW = dt(2026, 10, 21, 10, 0)  # 수요일


class Base(unittest.TestCase):
    def setUp(self):
        self.real = (config.DB_PATH, S.now)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "s.db")
        self.now = NOW
        S.now = lambda: self.now

    def tearDown(self):
        config.DB_PATH, S.now = self.real


class Parse(Base):
    def p(self, text):
        r = S.parse(text, NOW)
        return f"{r['at']:%m-%d %H:%M}", r["text"], r["guessed"]

    def test_date_time_text(self):
        self.assertEqual(self.p("내일 15시 치과"), ("10-22 15:00", "치과", False))
        self.assertEqual(self.p("모레 오후 3시 반 미용실"), ("10-23 15:30", "미용실", False))
        self.assertEqual(self.p("10월 23일 14:00 병원"), ("10-23 14:00", "병원", False))
        self.assertEqual(self.p("23일 오전 9시 30분 운동"), ("10-23 09:30", "운동", False))
        self.assertEqual(self.p("낼 정오 점심 약속"), ("10-22 12:00", "점심 약속", False))
        self.assertEqual(self.p("치과 내일 15시"), ("10-22 15:00", "치과", False))  # 순서가 달라도
        self.assertEqual(self.p("모레 오전 10시 회의 있다고"), ("10-23 10:00", "회의", False))

    def test_no_date_is_today(self):
        self.assertEqual(self.p("15시 회의"), ("10-21 15:00", "회의", False))

    def test_hour_without_am_pm_is_guessed(self):
        self.assertEqual(self.p("내일 3시에 치과 가기"), ("10-22 15:00", "치과 가기", True))
        self.assertEqual(self.p("내일 9시 출근"), ("10-22 09:00", "출근", True))
        self.assertEqual(self.p("내일 12시 점심"), ("10-22 12:00", "점심", True))
        self.assertEqual(self.p("내일 저녁 7시 저녁 약속"), ("10-22 19:00", "저녁 약속", False))

    def test_words_like_hours_are_content(self):
        self.assertEqual(self.p("내일 15시 2시간 회의"), ("10-22 15:00", "2시간 회의", False))
        self.assertEqual(self.p("내일 15시 금요일 발표 준비"), ("10-22 15:00", "금요일 발표 준비", False))

    def test_errors(self):
        for text, word in (("내일 치과", "몇 시"), ("내일 15시", "무엇을"), ("오늘 오전 9시 운동", "지난 시각"),
                           ("다다다음주 금요일 15시 회의", "날짜로 알 수 없"), ("지난주 금요일 15시 회의", "날짜로 알 수 없"),
                           ("매주 금요일 15시 회의", "날짜로 알 수 없"), ("다음 주 10시 회의", "날짜로 알 수 없"),
                           ("3일 뒤 15시 치과", "날짜로 알 수 없"), ("2월 30일 15시 치과", "없는 날짜"),
                           ("밤 12시 야식", "자정"), ("내일 15시 70분 치과", "분은"), ("내일 25시 치과", "25시")):
            with self.assertRaises(S.ScheduleError, msg=text) as cm:
                S.parse(text, NOW)
            self.assertIn(word, str(cm.exception), text)


class Weekday(Base):
    def at(self, text):
        r = S.parse(text, NOW)
        self.assertIsNone(r["ask"], text)
        return f"{r['at']:%m-%d %H:%M}", r["text"]

    def test_week_words(self):  # NOW 는 10월 21일 수요일
        self.assertEqual(self.at("이번주 금요일 15시 치과"), ("10-23 15:00", "치과"))
        self.assertEqual(self.at("이번 주 금요일 15시 치과"), ("10-23 15:00", "치과"))
        self.assertEqual(self.at("다음주 금요일 15시 치과"), ("10-30 15:00", "치과"))
        self.assertEqual(self.at("다음 주 월요일 15시 치과"), ("10-26 15:00", "치과"))
        self.assertEqual(self.at("다다음주 일요일 15시 치과"), ("11-08 15:00", "치과"))
        self.assertEqual(self.at("이번주 금요일에 15시 치과"), ("10-23 15:00", "치과"))
        self.assertEqual(self.at("치과 다음주 금요일 15시"), ("10-30 15:00", "치과"))

    def test_this_week_day_gone_is_past(self):
        with self.assertRaises(S.ScheduleError) as cm:
            S.parse("이번주 월요일 15시 치과", NOW)
        self.assertIn("지난 시각", str(cm.exception))

    def test_bare_weekday_asks_week(self):
        ask = S.parse("금요일 15시 치과", NOW)["ask"]
        self.assertEqual([c[0] for c in ask["choices"]], ["이번주", "다음주"])
        self.assertIn("10월 23일", ask["choices"][0][1])
        self.assertIn("10월 30일", ask["choices"][1][1])
        self.assertEqual(S.parse(ask["choices"][1][2], NOW)["at"], dt(2026, 10, 30, 15, 0))

    def test_bare_weekday_before_time_in_words(self):
        ask = S.parse("15시 금요일 치과", NOW)["ask"]
        self.assertEqual(S.parse(ask["choices"][0][2], NOW)["at"], dt(2026, 10, 23, 15, 0))

    def test_bare_weekday_already_gone_only_offers_next_week(self):
        ask = S.parse("월요일 15시 치과", NOW)["ask"]
        self.assertEqual([c[0] for c in ask["choices"]], ["다음주"])
        self.assertIn("10월 26일", ask["question"])

    def test_bare_today_weekday(self):
        self.assertEqual(len(S.parse("수요일 15시 치과", NOW)["ask"]["choices"]), 2)  # 아직 안 지남
        self.assertEqual(len(S.parse("수요일 오전 9시 치과", NOW)["ask"]["choices"]), 1)  # 지남

    def test_date_word_wins_over_weekday(self):
        self.assertEqual(self.at("내일 15시 금요일 발표 준비"), ("10-22 15:00", "금요일 발표 준비"))


class Period(Base):
    def test_asks_when_missing(self):
        ask = S.parse("오늘 6시 10분 짜장", NOW)["ask"]
        self.assertEqual([c[1] for c in ask["choices"]], ["오전 6시 10분", "오후 6시 10분"])
        self.assertEqual(S.parse(ask["choices"][1][2], NOW)["at"], dt(2026, 10, 21, 18, 10))

    def test_colon_time_asks_unless_24_hour(self):
        self.assertIsNotNone(S.parse("내일 6:10 치과", NOW)["ask"])
        self.assertIsNone(S.parse("내일 18:10 치과", NOW)["ask"])
        self.assertIsNone(S.parse("내일 15시 치과", NOW)["ask"])
        self.assertIsNone(S.parse("내일 정오 치과", NOW)["ask"])
        self.assertIsNone(S.parse("내일 오후 3시 치과", NOW)["ask"])
        self.assertIsNone(S.parse("10분 뒤 치과", NOW)["ask"])

    def test_twelve_asks(self):
        ask = S.parse("내일 12시 치과", NOW)["ask"]
        self.assertEqual([c[1] for c in ask["choices"]], ["오전 12시(자정)", "오후 12시(낮)"])
        self.assertEqual(S.parse(ask["choices"][0][2], NOW)["at"], dt(2026, 10, 22, 0, 0))
        self.assertEqual(S.parse(ask["choices"][1][2], NOW)["at"], dt(2026, 10, 22, 12, 0))

    def test_morning_gone_still_asks_and_morning_answer_is_past(self):
        ask = S.parse("오늘 9시 운동", NOW)["ask"]  # 오전 9시는 지났지만 오후 9시는 남았다
        with self.assertRaises(S.ScheduleError):
            S.parse(ask["choices"][0][2], NOW)
        self.assertEqual(S.parse(ask["choices"][1][2], NOW)["at"], dt(2026, 10, 21, 21, 0))

    def test_both_gone_is_past(self):
        with self.assertRaises(S.ScheduleError) as cm:
            S.parse("오늘 9시 운동", dt(2026, 10, 21, 22, 0))
        self.assertIn("지난 시각", str(cm.exception))


class Relative(Base):
    def p(self, text):
        r = S.parse(text, NOW)
        return f"{r['at']:%m-%d %H:%M}", r["text"], r["after"]

    def test_minutes_and_hours(self):
        self.assertEqual(self.p("10분 뒤 빨래"), ("10-21 10:10", "빨래", "10분"))
        self.assertEqual(self.p("30분후 회의"), ("10-21 10:30", "회의", "30분"))
        self.assertEqual(self.p("2시간 뒤 운동"), ("10-21 12:00", "운동", "2시간"))
        self.assertEqual(self.p("1시간 30분 뒤에 약"), ("10-21 11:30", "약", "1시간 30분"))
        self.assertEqual(self.p("1시간 반 후 약"), ("10-21 11:30", "약", "1시간 30분"))
        self.assertEqual(self.p("반 시간 뒤 약"), ("10-21 10:30", "약", "30분"))
        self.assertEqual(self.p("빨래 10분 뒤"), ("10-21 10:10", "빨래", "10분"))
        self.assertEqual(self.p("20시간 뒤 일어나기"), ("10-22 06:00", "일어나기", "20시간"))

    def test_real_qa_sentences(self):
        self.assertEqual(self.p("오늘 1분뒤 테스트라고 알려줘"), ("10-21 10:01", "테스트", "1분"))
        self.assertEqual(self.p("1분뒤에 qa 테스트 알려줘"), ("10-21 10:01", "qa 테스트", "1분"))
        self.assertEqual(self.p("지금 5분 뒤 빨래"), ("10-21 10:05", "빨래", "5분"))

    def test_errors(self):
        for text, word in (("내일 10분 뒤 빨래", "같이 쓸 수 없"), ("0분 뒤 빨래", "1분 이상"), ("10분 뒤", "무엇을")):
            with self.assertRaises(S.ScheduleError, msg=text) as cm:
                S.parse(text, NOW)
            self.assertIn(word, str(cm.exception), text)

    def test_not_relative(self):
        self.assertEqual(S.parse("15시 30분 뒤풀이", NOW)["after"], None)  # 시각과 내용
        self.assertEqual(S.parse("내일 15시 2시간 회의", NOW)["after"], None)
        with self.assertRaises(S.ScheduleError):
            S.parse("3일 뒤 15시 치과", NOW)

    def test_has_time(self):
        self.assertTrue(S.has_time("10분 뒤 빨래"))
        self.assertTrue(S.has_time("내일 15시 치과"))
        self.assertTrue(S.has_time("25시 치과"))
        self.assertFalse(S.has_time("제주도 여행 3일 코스"))

    def test_fmt_minutes(self):
        self.assertEqual([S.fmt_minutes(m) for m in (5, 60, 90)], ["5분", "1시간", "1시간 30분"])


class Seconds(Base):
    def setUp(self):
        super().setUp()
        self.now = dt(2026, 10, 21, 10, 0, 50)

    def test_relative_keeps_seconds(self):
        self.assertEqual(S.parse("1분 뒤 빨래", self.now)["at"], dt(2026, 10, 21, 10, 1, 50))

    def test_due_waits_until_exact_second(self):
        a = S.add("owner", S.parse("1분 뒤 빨래", self.now)["at"], "빨래")
        self.now = dt(2026, 10, 21, 10, 1, 49)
        self.assertEqual(S.due(), [])
        self.now = dt(2026, 10, 21, 10, 1, 50)
        self.assertEqual([r[0] for r in S.due()], [a])

    def test_pre_text_rounds_up(self):
        self.now = dt(2026, 10, 21, 10, 30, 5)
        self.assertIn("30분 뒤 11:00 치과", S.pre_text([(1, dt(2026, 10, 21, 11, 0), "치과")]))

    def test_pre_alert_on_exact_second(self):
        S.add("owner", dt(2026, 10, 21, 11, 0, 50), "치과", mode=S.MODE_BEFORE)
        self.now = dt(2026, 10, 21, 10, 30, 49)
        self.assertEqual(S.pre_due(), [])
        self.now = dt(2026, 10, 21, 10, 30, 50)
        self.assertEqual(S.pre_due()[0][4], 30)


class Ending(Base):
    def text(self, s):
        return S.parse(f"내일 15시 {s}", NOW)["text"]

    def test_request_words_are_removed(self):
        for s, want in (("빨래 알려줘", "빨래"), ("빨래 알려 주세요", "빨래"), ("빨래 알림", "빨래"), ("빨래 알림 줘", "빨래"),
                        ("빨래 알림 잡아줘", "빨래"), ("치과 일정 잡아줘", "치과"), ("치과 등록해줘", "치과"),
                        ("테스트라고 알려줘", "테스트"), ("회의라고 나한테 알려줘", "회의"), ("수업이라고", "수업"),
                        ("빨래 하라고 알려줘", "빨래"), ("회의 있다고 알려줘", "회의")):
            self.assertEqual(self.text(s), want, s)

    def test_verbs_and_lone_words_stay(self):
        self.assertEqual(self.text("약 먹으라고 알려줘"), "약 먹으라고")
        self.assertEqual(self.text("치과 가라고 알려줘"), "치과 가라고")
        self.assertEqual(self.text("알림"), "알림")  # 이것만 남으면 그대로
        self.assertEqual(self.text("치과 예약"), "치과 예약")


class Store(Base):
    def test_add_list_delete_by_user(self):
        a = S.add("owner", dt(2026, 10, 22, 15, 0), "치과")
        S.add("owner", dt(2026, 10, 21, 9, 0), "지난 일")
        S.add("family1", dt(2026, 10, 22, 9, 0), "가족 일정")
        self.assertEqual([r[2] for r in S.upcoming("owner")], ["치과"])  # 지난 것과 남의 것은 안 보임
        self.assertIsNone(S.get("family1", a))
        self.assertEqual(S.delete("family1", a), 0)
        self.assertEqual(S.delete("owner", a), 1)
        self.assertEqual(S.upcoming("owner"), [])

    def test_on_day(self):
        S.add("owner", dt(2026, 10, 22, 15, 0), "치과")
        S.add("owner", dt(2026, 10, 22, 9, 0), "회의")
        S.add("owner", dt(2026, 10, 23, 9, 0), "다른 날")
        self.assertEqual([r[2] for r in S.on_day("owner", dt(2026, 10, 22).date())], ["회의", "치과"])

    def test_due_and_mark(self):
        a = S.add("owner", dt(2026, 10, 21, 9, 58), "곧")
        S.add("owner", dt(2026, 10, 21, 10, 30), "나중")
        self.assertEqual([r[0] for r in S.due()], [a])
        self.assertEqual(S.mark_notified([a]), 1)
        self.assertEqual(S.due(), [])

    def test_reminder_text_on_time_and_missed(self):
        rows = [(1, dt(2026, 10, 21, 9, 58), "곧"), (2, dt(2026, 10, 20, 15, 0), "어제 일")]
        out = S.reminder_text(rows, NOW)
        self.assertIn("⏰ 일정 알림: 09:58 곧", out)
        self.assertIn("놓친 일정", out)
        self.assertIn("10월 20일(화) 15:00 어제 일", out)


class Flow(Base):
    def setUp(self):
        super().setUp()
        from core import classifier
        from clients import llm
        self.real_models = (classifier.ask_model, llm.chat)
        classifier.ask_model = lambda text, model=None, timeout=120: ('{"의도": "일반대화"}', 0.0)  # 실제 모델을 부르지 않는다
        llm.chat = lambda messages, model=None, timeout=300: "대화 답"
        self.bot = A.Assistant()

    def tearDown(self):
        from core import classifier
        from clients import llm
        classifier.ask_model, llm.chat = self.real_models
        super().tearDown()

    def say(self, text):
        return self.bot.handle("owner", text)

    def mode_of(self, eid):
        with S._conn() as c:
            return c.execute("SELECT mode FROM events WHERE id=?", (eid,)).fetchone()[0]

    def test_register_saves_at_once_and_asks_mode(self):
        out = self.say("!일정 내일 15시 치과")
        self.assertIn("10월 22일(목) 15:00 치과", out)
        self.assertIn("1. 미리 알림: 30분 전, 20분 전, 10분 전, 5분 전 + 그 시각", out)
        self.assertIn("2. 그 시각에만", out)
        eid = S.upcoming("owner")[0][0]
        self.assertEqual(self.mode_of(eid), S.MODE_BEFORE)

    def test_mode_two(self):
        self.say("!일정 내일 15시 치과")
        eid = S.upcoming("owner")[0][0]
        self.assertIn("그 시각에만", self.say("2"))
        self.assertEqual(self.mode_of(eid), S.MODE_AT_TIME)

    def test_mode_defaults_to_one(self):
        for word in ("1", "그냥 해", "네", "", "!1"):
            self.say("!일정 내일 15시 치과")
            eid = S.upcoming("owner")[-1][0]
            self.assertIn("미리 알림으로", self.say(word), word)
            self.assertEqual(self.mode_of(eid), S.MODE_BEFORE)

    def test_mode_other_message_keeps_default_and_is_answered(self):
        self.say("!일정 내일 15시 치과")
        eid = S.upcoming("owner")[0][0]
        self.assertEqual(self.say("안녕"), "대화 답")
        self.assertEqual(self.mode_of(eid), S.MODE_BEFORE)
        self.assertNotIn("owner", self.bot.pending_mode)

    def test_mode_answer_after_timeout_is_normal_message(self):
        self.say("!일정 내일 15시 치과")
        eid = S.upcoming("owner")[0][0]
        self.bot.pending_mode["owner"] = (eid, time.time() - 1)
        self.assertEqual(self.say("2"), "대화 답")
        self.assertEqual(self.mode_of(eid), S.MODE_BEFORE)

    def test_mode_cancel_deletes(self):
        self.say("!일정 내일 15시 치과")
        self.assertIn("취소", self.say("취소"))
        self.assertEqual(S.upcoming("owner"), [])

    def test_no_question_when_no_offset_applies(self):
        out = self.say("!알림 3분 뒤 빨래")
        self.assertNotIn("1. 미리 알림", out)
        self.assertIn("DM으로", out)
        self.assertNotIn("owner", self.bot.pending_mode)

    def test_question_shows_only_future_offsets(self):
        out = self.say("!알림 15분 뒤 빨래")
        self.assertIn("1. 미리 알림: 10분 전, 5분 전 + 그 시각", out)

    def test_period_is_asked_before_saving(self):
        for cmd in ("일정", "알림"):
            out = self.say(f"!{cmd} 내일 3시 치과")
            self.assertIn("오전인가요, 오후인가요?", out)
            self.assertIn("30초", out)
            self.assertEqual(S.upcoming("owner"), [])
            out = self.say("오전")
            self.assertIn("📅 일정(알림) #", out)
            self.assertIn("03:00", out)
            self.assertIn("알림 방식", out)  # 이어서 알림 방식을 묻는다
            self.say("2")
            eid = S.upcoming("owner")[0][0]
            self.assertEqual(self.mode_of(eid), S.MODE_AT_TIME)
            S.delete("owner", eid)

    def test_period_number_answer(self):
        self.say("!일정 내일 3시 치과")
        self.assertIn("15:00", self.say("2"))

    def test_period_other_message_cancels(self):
        self.say("!일정 내일 3시 치과")
        out = self.say("안녕")
        self.assertIn("등록을 취소했어요", out)
        self.assertIn("대화 답", out)
        self.assertEqual(S.upcoming("owner"), [])

    def test_period_cancel_word(self):
        self.say("!일정 내일 3시 치과")
        self.assertIn("취소했어요", self.say("취소"))
        self.assertEqual(S.upcoming("owner"), [])

    def test_period_timeout_saves_nothing_and_tells(self):
        self.say("!일정 내일 3시 치과")
        ask, original, _ = self.bot.pending_schedule["owner"]
        self.bot.pending_schedule["owner"] = (ask, original, time.time() - 1)
        self.assertIn("등록하지 않았어요", self.say("오전"))
        self.assertEqual(S.upcoming("owner"), [])
        self.say("!일정 내일 3시 치과")
        ask, original, _ = self.bot.pending_schedule["owner"]
        self.bot.pending_schedule["owner"] = (ask, original, time.time() - 1)
        out = self.say("안녕")
        self.assertIn("등록하지 않았어요", out)
        self.assertIn("대화 답", out)

    def test_period_answer_in_the_past_is_explained(self):
        self.say("!일정 오늘 9시 운동")
        self.assertIn("지난 시각", self.say("오전"))
        self.assertEqual(S.upcoming("owner"), [])

    def test_week_then_period_then_mode(self):
        self.assertIn("이번주인가요, 다음주인가요?", self.say("!일정 금요일 3시 치과"))
        self.assertIn("오전인가요, 오후인가요?", self.say("다음 주"))
        out = self.say("오후")
        self.assertIn("10월 30일(금) 15:00 치과", out)
        self.assertIn("알림 방식", out)

    def test_weekday_gone_confirms_next_week(self):
        self.assertIn("등록할까요", self.say("!일정 월요일 15시 치과"))
        self.assertIn("10월 26일(월) 15:00", self.say("네"))
        self.say("2")
        self.say("!일정 월요일 16시 치과")
        self.assertIn("취소했어요", self.say("아니"))
        self.assertEqual(len(S.upcoming("owner")), 1)

    def test_headers(self):
        self.assertIn("📅 일정(알림) #", self.say("!일정 내일 15시 치과"))
        self.say("2")
        self.assertIn("📅 앞으로의 일정(알림)", self.say("!일정 목록"))

    def test_delete_phrases_need_confirm(self):
        for cmd in ("일정", "알림"):
            for phrase in ("{} 지워", "{}번 삭제해줘", "#{} 삭제", "{}번 없애줘", "{} 지워 줘"):
                eid = S.add("owner", dt(2026, 10, 22, 15, 0), "치과")
                out = self.say(f"!{cmd} " + phrase.format(eid))
                self.assertIn("지울까요", out, phrase)
                self.assertIn("30초", out)
                self.assertIsNotNone(S.get("owner", eid))
                self.assertIn("삭제했습니다", self.say("네"))
                self.assertIsNone(S.get("owner", eid))

    def test_delete_without_number_shows_list(self):
        S.add("owner", dt(2026, 10, 22, 15, 0), "치과")
        for text in ("!일정 지워", "!알림 삭제해줘", "!일정 삭제"):
            out = self.say(text)
            self.assertIn("치과", out, text)
            self.assertIn("몇 번", out, text)
        self.assertEqual(len(S.upcoming("owner")), 1)

    def test_delete_phrase_like_content_still_registers(self):
        self.assertIn("등록했어요", self.say("!일정 내일 15시 치과 예약 삭제"))

    def test_pasted_event_line_asks_about_that_event(self):
        from features import notes
        nid = notes.add("owner", "잠온다")
        eid = S.add("owner", dt(2026, 10, 22, 15, 0), "짜장 시켜먹기")
        line = S.fmt(S.upcoming("owner"))
        out = self.say(f"{line} 이거 지워")
        self.assertIn("이 일정을 지울까요", out)
        self.assertIn("짜장 시켜먹기", out)
        self.assertIn("삭제했습니다", self.say("어"))
        self.assertIsNone(S.get("owner", eid))
        self.assertIsNotNone(notes.get("owner", nid))  # 메모는 그대로

    def test_delete_sentence_for_event_words(self):
        from core import classifier
        real = classifier.ask_model
        try:
            for word in ("일정", "알림", "약속"):
                eid = S.add("owner", dt(2026, 10, 22, 15, 0), "치과")
                # 모델이 메모삭제로 잘못 읽어도 문장의 말로 일정을 지운다
                classifier.ask_model = lambda text, model=None, timeout=120: ('{"의도": "메모삭제", "내용": "%d"}' % eid, 0.0)
                self.assertIn("이 일정을 지울까요", self.say(f"{eid}번 {word} 지워줘"), word)
                self.assertIn("삭제했습니다", self.say("네"))
            classifier.ask_model = lambda text, model=None, timeout=120: ('{"의도": "일정삭제", "내용": null}', 0.0)
            self.assertIn("일정이 없습니다", self.say("약속 지워줘"))
        finally:
            classifier.ask_model = real

    def add_two(self):  # NOW 기준으로 지난 것 하나, 앞으로의 것 하나
        return S.add("owner", dt(2026, 10, 20, 15, 0), "지난 치과"), S.add("owner", dt(2026, 10, 22, 15, 0), "다음 치과")

    def test_bulk_asks_twice_then_deletes_everything(self):
        self.add_two()
        for text in ("약속 다 지워줘", "일정 전부 지워줘", "알림 모두 삭제해줘", "내 일정 전체 삭제해", "!일정 다 지워", "!알림 전체 삭제"):
            out = self.say(text)
            self.assertIn("일정 2개를 모두 지울까요", out, text)
            self.assertIn("지난 치과", out)
            self.assertEqual(len(S.everything("owner")), 2)
            self.assertIn("정말", self.say("네"), text)
            self.assertEqual(len(S.everything("owner")), 2)
            self.say("아니")
        self.say("약속 다 지워줘")
        self.say("네")
        self.assertIn("일정 2개를 지웠어요", self.say("확인"))
        self.assertEqual(S.everything("owner"), [])

    def test_bulk_past_only_deletes_past(self):
        _, later = self.add_two()
        for text in ("지난 약속 다 지워줘", "지난 일정 전부 삭제해줘", "!일정 지난 거 다 지워"):
            out = self.say(text)
            self.assertIn("지난 일정 1개를 모두 지울까요", out, text)
            self.assertNotIn("다음 치과", out)
            self.say("취소")
        self.say("지난 약속 다 지워줘")
        self.say("네")
        self.assertIn("지난 일정 1개를 지웠어요", self.say("네"))
        self.assertEqual([r[0] for r in S.everything("owner")], [later])
        self.assertIn("지난 일정이 없습니다", self.say("지난 약속 다 지워줘"))

    def test_bulk_no_answer_deletes_nothing_at_either_step(self):
        self.add_two()
        for yes_first in (False, True):
            self.say("약속 다 지워줘")
            if yes_first:
                self.say("네")
            kind, what, ids, step, _ = self.bot.pending_bulk["owner"]
            self.bot.pending_bulk["owner"] = (kind, what, ids, step, time.time() - 1)
            self.assertIn("아무것도 지우지 않았어요", self.say("네"))
            self.assertEqual(len(S.everything("owner")), 2)
        self.say("약속 다 지워줘")
        kind, what, ids, step, _ = self.bot.pending_bulk["owner"]
        self.bot.pending_bulk["owner"] = (kind, what, ids, step, time.time() - 1)
        self.assertIn("아무것도 지우지 않았어요", self.say("안녕"))

    def test_bulk_other_message_cancels_at_either_step(self):
        self.add_two()
        self.say("약속 다 지워줘")
        out = self.say("안녕")
        self.assertIn("아무것도 지우지 않았어요", out)
        self.assertIn("대화 답", out)
        self.say("약속 다 지워줘")
        self.say("네")
        self.assertIn("아무것도 지우지 않았어요", self.say("안녕"))
        self.assertIn("대화 답", self.say("네"))  # 이미 취소됐다
        self.assertEqual(len(S.everything("owner")), 2)

    def test_bulk_deletes_only_what_was_shown(self):
        self.add_two()
        self.say("약속 다 지워줘")
        self.say("네")
        added = S.add("owner", dt(2026, 10, 23, 15, 0), "새 일정")
        self.say("네")
        self.assertEqual([r[0] for r in S.everything("owner")], [added])

    def test_bulk_does_not_touch_other_users_or_memos(self):
        from features import notes
        self.add_two()
        other = S.add("family1", dt(2026, 10, 22, 15, 0), "남의 일정")
        nid = notes.add("owner", "메모")
        self.say("약속 다 지워줘")
        self.say("네")
        self.say("네")
        self.assertIsNotNone(S.get("family1", other))
        self.assertIsNotNone(notes.get("owner", nid))

    def test_bulk_narrow_or_bare_sentences_delete_nothing(self):
        self.add_two()
        for text, word in (("다 지워줘", "메모인지 일정인지"), ("전부 삭제해줘", "메모인지 일정인지"),
                           ("지난주 약속 다 지워", "전체 삭제는"), ("치과 일정 다 지워줘", "전체 삭제는"),
                           ("내일 일정 전부 삭제해줘", "전체 삭제는"), ("지난 메모 다 지워줘", "기준이 없어요")):
            self.assertIn(word, self.say(text), text)
            self.assertNotIn("owner", self.bot.pending_bulk)
        self.assertEqual(len(S.everything("owner")), 2)

    def test_bulk_with_number_is_only_that_number(self):
        from core import classifier
        first, _ = self.add_two()
        real = classifier.ask_model
        try:
            classifier.ask_model = lambda text, model=None, timeout=120: ('{"의도": "일정삭제", "내용": "%d"}' % first, 0.0)
            self.assertIn("이 일정을 지울까요", self.say(f"{first}번 약속 다 지워줘"))
        finally:
            classifier.ask_model = real
        self.assertNotIn("owner", self.bot.pending_bulk)

    def test_bulk_look_alike_goes_to_chat(self):
        for text in ("다음 일정 알려줘", "일정 앱 알림 전부 없애는 방법", "코드 주석 다 지워줘"):
            self.assertEqual(self.say(text), "대화 답", text)

    def test_bulk_list_is_cut_short(self):
        for i in range(A.BULK_SHOW + 2):
            S.add("owner", dt(2026, 10, 22, 15, 0), f"일정{i}")
        out = self.say("약속 다 지워줘")
        self.assertIn(f"일정 {A.BULK_SHOW + 2}개", out)
        self.assertIn("외 2개", out)

    def test_offset_setting(self):
        self.assertIn("30분 전, 20분 전, 10분 전, 5분 전", self.say("!분전설정"))
        self.assertIn("1시간 전, 15분 전", self.say("!분전설정 15분, 1시간"))
        self.assertIn("1시간 전, 15분 전", self.say("!분전 설정"))
        self.assertIn("10분 전", self.say("!분전 설정 10분"))
        self.assertIn("10분 전", self.say("!분전알림"))
        self.assertIn("30분 전", self.say("!분전설정 기본"))
        self.assertIn("알 수 없어요", self.say("!분전설정 조금"))
        self.assertIn("1분에서 24시간", self.say("!분전설정 0분"))

    def test_offset_setting_is_per_user(self):
        self.say("!분전설정 7분")
        self.assertEqual(S.offsets_of("owner"), [7])
        self.assertEqual(S.offsets_of("family1"), list(S.DEFAULT_OFFSETS))

    def test_bad_input_shows_help(self):
        self.assertIn("몇 시", self.say("!일정 내일 치과"))
        self.assertEqual(S.upcoming("owner"), [])

    def test_delete_needs_confirm(self):
        eid = S.add("owner", dt(2026, 10, 22, 15, 0), "치과")
        self.assertIn("지울까요", self.say(f"!일정삭제 {eid}"))
        self.assertEqual(len(S.upcoming("owner")), 1)
        self.assertIn("삭제했습니다", self.say("확인"))
        self.assertEqual(S.upcoming("owner"), [])

    def test_delete_accepts_yes_and_no_words(self):
        eid = S.add("owner", dt(2026, 10, 22, 15, 0), "치과")
        self.say(f"!일정 삭제 {eid}")
        self.assertIn("취소했어요", self.say("아니"))
        self.assertEqual(len(S.upcoming("owner")), 1)
        self.say(f"!일정 삭제 {eid}")
        self.assertIn("삭제했습니다", self.say("응"))
        self.assertEqual(S.upcoming("owner"), [])

    def test_delete_wrong_number(self):
        self.assertIn("없습니다", self.say("!일정삭제 99"))
        self.assertIn("번호를", self.say("!일정삭제 치과"))

    def test_empty_list_and_bare_command(self):
        self.assertIn("일정이 없습니다", self.say("!일정"))
        self.assertIn("일정이 없습니다", self.say("!일정 목록"))

    def test_spaced_delete(self):
        eid = S.add("owner", dt(2026, 10, 22, 15, 0), "치과")
        self.assertIn("지울까요", self.say(f"!일정 삭제 {eid}"))
        self.assertIn("삭제했습니다", self.say("확인"))
        self.assertIn("일정이 없습니다", self.say("!일정 삭제"))

    def test_memo_delete_confirm_replaces_event_delete(self):
        from features import notes
        nid = notes.add("owner", "지울 메모")
        eid = S.add("owner", dt(2026, 10, 22, 15, 0), "치과")
        self.say(f"!일정 삭제 {eid}")
        self.bot.pending_event.pop("owner")
        self.bot._ask_delete_note("owner", nid)
        self.assertIn("메모", self.say("확인"))
        self.assertEqual(len(S.upcoming("owner")), 1)


class PreDue(Base):
    def ev(self, at, text="치과", mode=S.MODE_BEFORE, user="owner"):
        return S.add(user, at, text, mode=mode)  # 등록 시각은 self.now (10:00)

    def at(self, hour, minute):
        return dt(2026, 10, 21, hour, minute)

    def test_each_offset_once_in_order(self):
        a = self.ev(self.at(11, 0))
        sent = []
        for minute, want in ((29, None), (30, 30), (31, None), (40, 20), (50, 10), (55, 5), (59, None)):
            self.now = self.at(10, minute)
            rows = S.pre_due()
            self.assertEqual(rows[0][4] if rows else None, want, minute)
            if rows:
                S.mark_pre([(rows[0][0], rows[0][4])])
                sent.append(rows[0][4])
        self.assertEqual(sent, [30, 20, 10, 5])
        self.now = self.at(11, 0)
        self.assertEqual(S.pre_due(), [])  # 그 시각부터는 일반 알림
        self.assertEqual([r[0] for r in S.due()], [a])

    def test_offsets_before_registration_are_skipped(self):
        self.ev(self.at(10, 15))  # 15분 뒤: 30분 전과 20분 전은 이미 지났다
        self.assertEqual(S.pre_due(), [])
        self.now = self.at(10, 5)
        self.assertEqual(S.pre_due()[0][4], 10)

    def test_equal_to_registration_is_skipped(self):
        self.ev(self.at(10, 10))  # 10분 뒤의 '10분 전'은 지금이라 보내지 않는다
        self.assertEqual(S.pre_due(), [])

    def test_late_bot_sends_only_nearest(self):
        self.ev(self.at(11, 0))
        self.now = self.at(10, 52)  # 30·20·10분 전이 다 지남
        self.assertEqual([r[4] for r in S.pre_due()], [10])

    def test_mode_two_gets_no_pre(self):
        self.ev(self.at(11, 0), mode=S.MODE_AT_TIME)
        self.now = self.at(10, 55)
        self.assertEqual(S.pre_due(), [])

    def test_notified_gets_no_pre(self):
        a = self.ev(self.at(11, 0))
        S.mark_notified([a])
        self.now = self.at(10, 55)
        self.assertEqual(S.pre_due(), [])

    def test_follows_changed_setting(self):
        self.ev(self.at(11, 0))
        S.set_offsets("owner", [15])
        self.now = self.at(10, 30)
        self.assertEqual(S.pre_due(), [])
        self.now = self.at(10, 45)
        self.assertEqual(S.pre_due()[0][4], 15)

    def test_setting_uses_each_owner(self):
        self.ev(self.at(11, 0), user="owner")
        self.ev(self.at(11, 0), user="family1")
        S.set_offsets("family1", [10])
        self.now = self.at(10, 30)
        self.assertEqual([r[1] for r in S.pre_due()], ["owner"])

    def test_pre_text_uses_real_time_left(self):
        rows = [(1, dt(2026, 10, 21, 11, 0), "치과"), (2, dt(2026, 10, 22, 9, 0), "회의")]
        self.now = self.at(10, 52)
        out = S.pre_text(rows)
        self.assertIn("🔔 미리 알림: 8분 뒤 11:00 치과", out)
        self.assertIn("10월 22일(목) 09:00 회의", out)


class ParseOffsets(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(S.parse_offsets("30분, 20분, 10분, 5분"), [30, 20, 10, 5])
        self.assertEqual(S.parse_offsets("5 10 10"), [10, 5])
        self.assertEqual(S.parse_offsets("1시간, 30분전"), [60, 30])
        self.assertEqual(S.parse_offsets("10분 전"), [10])

    def test_errors(self):
        for text in ("", "조금", "0분", "2000분", ",".join(str(i) for i in range(1, 13))):
            with self.assertRaises(S.ScheduleError, msg=text):
                S.parse_offsets(text)


class Remind(unittest.TestCase):
    def run_once(self, items, fail=(), give_up=()):
        sent, acked = [], []

        async def fetch():
            return items

        async def send(ext, text):
            if ext in give_up:
                raise reminders.GiveUp("NotFound")
            if ext in fail:
                raise RuntimeError("보내기 실패")
            sent.append((ext, text))

        async def ack(ids):
            acked.extend(ids)
        out = asyncio.run(reminders.run_once(fetch, send, ack))
        return out, sent, acked

    def test_sends_and_acks(self):
        out, sent, acked = self.run_once([{"user": "owner", "external_ids": ["111"], "ids": [1, 2], "text": "알림"}])
        self.assertEqual((out, sent, acked), ([1, 2], [("111", "알림")], [1, 2]))

    def test_failed_person_is_retried_others_not_blocked(self):
        items = [{"user": "a", "external_ids": ["1"], "ids": [1], "text": "가"},
                 {"user": "b", "external_ids": ["2"], "ids": [2], "text": "나"}]
        out, sent, acked = self.run_once(items, fail=("1",))
        self.assertEqual((out, acked), ([2], [2]))  # 실패한 1번은 표시하지 않아 다음에 다시 보낸다

    def test_one_of_two_ids_is_enough(self):
        out, _, _ = self.run_once([{"user": "a", "external_ids": ["1", "2"], "ids": [5], "text": "가"}], fail=("1",))
        self.assertEqual(out, [5])

    def test_permanent_failure_is_marked_not_retried(self):
        out, sent, acked = self.run_once([{"user": "a", "external_ids": ["1"], "ids": [7], "text": "가"}], give_up=("1",))
        self.assertEqual((out, sent, acked), ([7], [], [7]))

    def test_mixed_failures_retry(self):
        out, _, acked = self.run_once([{"user": "a", "external_ids": ["1", "2"], "ids": [7], "text": "가"}],
                                      fail=("1",), give_up=("2",))
        self.assertEqual((out, acked), ([], []))  # 잠깐의 실패가 하나라도 있으면 다음에 다시

    def test_pre_is_acked_separately_after_sending(self):
        sent, acked, pre_acked = [], [], []

        async def fetch():
            return [{"user": "a", "external_ids": ["1"], "ids": [], "pre": [[5, 10]], "text": "미리"}]

        async def send(ext, text):
            sent.append(text)

        async def ack(ids):
            acked.extend(ids)

        async def ack_pre(pre):
            pre_acked.extend(pre)
        asyncio.run(reminders.run_once(fetch, send, ack, ack_pre))
        self.assertEqual((sent, acked, pre_acked), (["미리"], [], [[5, 10]]))

    def test_failed_pre_is_not_acked(self):
        pre_acked = []

        async def fetch():
            return [{"user": "a", "external_ids": ["1"], "ids": [], "pre": [[5, 10]], "text": "미리"}]

        async def send(ext, text):
            raise RuntimeError("보내기 실패")

        async def ack(ids):
            pass

        async def ack_pre(pre):
            pre_acked.extend(pre)
        asyncio.run(reminders.run_once(fetch, send, ack, ack_pre))
        self.assertEqual(pre_acked, [])

    def test_nothing_due_no_ack(self):
        self.assertEqual(self.run_once([]), ([], [], []))


try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi 가 없어서 건너뜀")
class DueApi(Base):
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
        self.bot_key = apikeys.create("owner", "디스코드 봇", kind="bot")
        self.app_key = apikeys.create("owner", "맥 앱")
        self.client = TestClient(api_server.app)

    def tearDown(self):
        self.common.LOCAL_HOSTS.clear()
        self.common.LOCAL_HOSTS.update(self.hosts)
        super().tearDown()

    def post(self, path, body, key=None):
        return self.client.post(path, json=body, headers={"Authorization": f"Bearer {key or self.bot_key}"})

    def test_due_grouped_by_person_and_ack(self):
        a = S.add("owner", dt(2026, 10, 21, 9, 59), "치과")
        b = S.add("owner", dt(2026, 10, 20, 15, 0), "어제 일")
        S.add("family1", dt(2026, 10, 21, 9, 0), "연결 없는 사람")  # 디스코드 연결이 없어 남겨 둔다
        S.add("owner", dt(2026, 10, 21, 11, 0), "아직")
        items = self.post("/internal/due", {"source": "discord"}).json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["user"], items[0]["external_ids"], sorted(items[0]["ids"])), ("owner", ["111"], sorted([a, b])))
        self.assertIn("놓친 일정", items[0]["text"])
        self.assertEqual(self.post("/internal/due/ack", {"ids": items[0]["ids"]}).json(), {"marked": 2})
        self.assertEqual(self.post("/internal/due", {"source": "discord"}).json()["items"], [])

    def test_pre_alert_flow(self):
        a = S.add("owner", dt(2026, 10, 21, 11, 0), "치과", mode=S.MODE_BEFORE)
        self.now = dt(2026, 10, 21, 10, 30)
        items = self.post("/internal/due", {"source": "discord"}).json()["items"]
        self.assertEqual((items[0]["ids"], items[0]["pre"]), ([], [[a, 30]]))
        self.assertIn("🔔 미리 알림: 30분 뒤 11:00 치과", items[0]["text"])
        self.assertEqual(self.post("/internal/due/ack", {"pre": items[0]["pre"]}).status_code, 200)
        self.assertEqual(self.post("/internal/due", {"source": "discord"}).json()["items"], [])

    def test_ack_rejects_bad_pre(self):
        self.assertEqual(self.post("/internal/due/ack", {"pre": "1"}).status_code, 400)
        self.assertEqual(self.post("/internal/due/ack", {"pre": [[1]]}).status_code, 400)
        self.assertEqual(self.post("/internal/due/ack", {"pre": [[True, 1]]}).status_code, 400)

    def test_access_log_hides_only_successful_due_polling(self):
        import logging

        def keep(path, status):
            args = ("127.0.0.1:1", "POST", path, "1.1", status)
            record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', args, None)
            return self.api.QuietDuePolling().filter(record)
        self.assertFalse(keep("/internal/due", 200))
        self.assertTrue(keep("/internal/due", 500))
        self.assertTrue(keep("/internal/due/ack", 200))
        self.assertTrue(keep("/internal/message", 200))

    def test_due_needs_bot_key_and_server(self):
        self.assertEqual(self.post("/internal/due", {}, key=self.app_key).status_code, 403)
        self.common.LOCAL_HOSTS.discard("testclient")
        self.assertEqual(self.post("/internal/due", {}).status_code, 403)

    def test_ack_rejects_bad_ids(self):
        self.assertEqual(self.post("/internal/due/ack", {"ids": "1"}).status_code, 400)
        self.assertEqual(self.post("/internal/due/ack", {"ids": [True]}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
