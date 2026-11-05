import datetime, io, json, os, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from core import classifier
from scripts import classifier_eval as ev


NOW = datetime.date(2026, 10, 20)


class DateFromPhrase(unittest.TestCase):
    def d(self, phrase):
        return classifier.date_from_phrase(phrase, NOW)

    def test_none_is_today(self):
        self.assertEqual(self.d(None), "오늘")
        self.assertEqual(self.d(" "), "오늘")

    def test_relative_words(self):
        self.assertEqual(self.d("오늘"), "오늘")
        self.assertEqual(self.d("지금"), "오늘")
        self.assertEqual(self.d("내일"), "내일")
        self.assertEqual(self.d("내일 아침"), "내일")
        self.assertEqual(self.d("어제"), "어제")

    def test_nael_means_tomorrow(self):
        self.assertEqual(self.d("낼"), "내일")
        self.assertEqual(self.d("낼 아침"), "내일")

    def test_day_after_tomorrow_is_plus_two(self):
        for w in ("모레", "내일모레", "내일 모레", "낼모레", "낼 모레"):
            self.assertEqual(self.d(w), "모레", w)

    def test_explicit_dates(self):
        self.assertEqual(self.d("10월 30일"), "2026-10-30")
        self.assertEqual(self.d("10/30"), "2026-10-30")
        self.assertEqual(self.d("28일"), "2026-10-28")
        self.assertEqual(self.d("10월 22일"), "모레")   # 이름이 있는 날은 이름으로
        self.assertEqual(self.d("10월 15일"), "2026-10-15")  # 지난 날도 받는다
        self.assertEqual(self.d("1월 5일"), "2027-01-05")    # 너무 오래 전이면 내년

    def test_unsupported_dates(self):
        for w in ("글피", "이번 주말", "다음 주 월요일", "금요일", "2월 30일", "3일 뒤", "엉뚱한 말"):
            self.assertIsNone(self.d(w), w)


class Parse(unittest.TestCase):
    def test_weather_slots(self):
        r = classifier.parse('{"의도": "날씨", "지역": "경기 광주", "날짜": "내일"}')
        self.assertEqual((r["의도"], r["지역"], r["날짜"]), ("날씨", "경기 광주", "내일"))

    def test_missing_date_is_today_and_unsupported_is_none(self):
        self.assertEqual(classifier.parse('{"의도": "날씨", "지역": "부산", "날짜": null}')["날짜"], "오늘")
        r = classifier.parse('{"의도": "날씨", "지역": "서울", "날짜": "이번 주말"}')
        self.assertIsNone(r["날짜"])
        self.assertEqual(r["날짜글자"], "이번 주말")
        self.assertEqual(classifier.parse('{"의도": "날씨", "지역": "서울", "날짜": "모레"}')["날짜"], "모레")

    def test_general_chat_clears_slots(self):
        r = classifier.parse('{"의도": "일반대화", "지역": "서울", "날짜": "내일"}')
        self.assertEqual(r, {"의도": "일반대화", "지역": None, "날짜": None, "날짜글자": None, "내용": None})

    def test_bad_format(self):
        for raw in ("날씨입니다", "[]", '{"의도": "비행기예약"}', '{"의도": "날씨", "지역": 3}',
                    '{"의도": "날씨", "날짜": 3}', '{"의도": "날씨", "지역": "부산 날씨 알려줘 오늘 내일 모레 어떻게 돼"}', None):
            self.assertIsNone(classifier.parse(raw), raw)

    def test_empty_region_is_none(self):
        self.assertIsNone(classifier.parse('{"의도": "날씨", "지역": "  ", "날짜": null}')["지역"])


class ParseMemo(unittest.TestCase):
    def p(self, raw, text=None):
        return classifier.parse(raw, text)

    def test_save_keeps_content_from_the_sentence(self):
        r = self.p('{"의도": "메모저장", "지역": "부산", "날짜": "내일", "내용": "내일 치과 3시"}', "내일 치과 3시 메모해줘")
        self.assertEqual((r["의도"], r["내용"], r["지역"], r["날짜"]), ("메모저장", "내일 치과 3시", None, None))

    def test_save_content_must_be_in_the_sentence(self):
        raw = '{"의도": "메모저장", "내용": "우유 사기"}'
        self.assertIsNone(self.p(raw, "계란 사야 해 적어 놔"))       # 지어낸 내용
        self.assertIsNotNone(self.p(raw, "우유  사기 메모해줘"))     # 띄어쓰기 차이는 허용
        self.assertIsNotNone(self.p(raw))                            # 원문을 안 주면 검사 안 함

    def test_save_needs_content(self):
        for c in ('null', '""', '"  "', '3', '"' + "가" * 501 + '"'):
            self.assertIsNone(self.p('{"의도": "메모저장", "내용": %s}' % c), c)

    def test_query(self):
        self.assertEqual(self.p('{"의도": "메모조회", "내용": "치과"}')["내용"], "치과")
        self.assertIsNone(self.p('{"의도": "메모조회", "내용": null}')["내용"])
        self.assertIsNone(self.p('{"의도": "메모조회", "내용": " "}')["내용"])
        self.assertIsNone(self.p('{"의도": "메모조회", "내용": 5}'))
        self.assertIsNone(self.p('{"의도": "메모조회", "내용": "' + "가" * 31 + '"}'))

    def test_delete_number(self):
        for c, want in (('"3"', 3), ('3', 3), ('"3번"', 3), ('"#12"', 12), ('null', None), ('"0"', None),
                        ('"방금"', None), ('true', None), ('-1', None)):
            self.assertEqual(self.p('{"의도": "메모삭제", "내용": %s}' % c)["내용"], want, c)

    def test_summary_and_chat_have_no_content(self):
        self.assertIsNone(self.p('{"의도": "메모요약", "내용": "x"}')["내용"])
        self.assertIsNone(self.p('{"의도": "일반대화", "내용": "x"}')["내용"])

    def test_weather_has_no_content(self):
        self.assertIsNone(self.p('{"의도": "날씨", "지역": "부산", "내용": "x"}')["내용"])

    def test_looks_like_memo(self):
        self.assertTrue(classifier.looks_like_memo("우유 적어 놔"))
        self.assertFalse(classifier.looks_like_memo("안녕"))

    # --- 일정 ---
    def test_schedule_add_keeps_content_from_the_sentence(self):
        r = self.p('{"의도": "일정등록", "지역": "부산", "날짜": "내일", "내용": "내일 오후 3시 치과"}', "내일 오후 3시 치과 일정 잡아줘")
        self.assertEqual((r["의도"], r["내용"], r["지역"], r["날짜"]), ("일정등록", "내일 오후 3시 치과", None, None))

    def test_schedule_add_content_must_be_in_the_sentence(self):
        self.assertIsNone(self.p('{"의도": "일정등록", "내용": "내일 3시 회의"}', "모레 2시 치과 일정 잡아줘"))
        self.assertIsNone(self.p('{"의도": "일정등록", "내용": null}'))

    def test_schedule_query_has_no_content(self):
        self.assertIsNone(self.p('{"의도": "일정조회", "내용": "x"}')["내용"])

    def test_looks_like_schedule(self):
        self.assertTrue(classifier.looks_like_schedule("내일 3시 일정 잡아"))
        self.assertFalse(classifier.looks_like_schedule("맛집 알려줘"))
        self.assertFalse(classifier.looks_like_schedule("여행 일정 짜줘"))  # 시각이 없으면 대화로


class ThinkOption(unittest.TestCase):
    """--think 는 시험에서만 쓴다. 주지 않으면 요청에 넣지 않는다"""
    def sent(self, option):
        self.addCleanup(classifier.set_think, None)
        classifier.set_think(option)
        with mock.patch.object(classifier.requests, "post") as post:
            post.return_value.json.return_value = {"message": {"content": "{}"}}
            classifier.ask_system("지시", "문장", "qwen3:8b")
        return post.call_args.kwargs["json"]

    def test_off_and_on(self):
        self.assertIs(self.sent("off")["think"], False)
        self.assertIs(self.sent("on")["think"], True)

    def test_default_sends_nothing(self):
        self.assertNotIn("think", self.sent(None))

    def test_assistant_default_is_unset(self):
        self.assertIsNone(classifier.THINK)

    def test_every_eval_script_has_the_option(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for script in ("classifier_eval", "file_eval"):
            with self.subTest(script), self.assertRaises(SystemExit) as stop, \
                 mock.patch("sys.stdout", io.StringIO()) as out, mock.patch("sys.argv", [script, "--help"]):
                __import__(f"scripts.{script}", fromlist=["main"]).main()
            self.assertEqual(stop.exception.code, 0)
            self.assertIn("--think", out.getvalue())


class CodeReadsDates(unittest.TestCase):
    """모델이 날짜를 틀리거나 빼먹어도 코드가 원문에서 되살린다 (README.md)"""
    def weather(self, text, phrase):
        return classifier.parse(json.dumps({"의도": "날씨", "지역": "서울", "날짜": phrase}), text)

    def event(self, text, content):
        return classifier.parse(json.dumps({"의도": "일정등록", "내용": content}), text)["내용"]

    def test_wrong_or_missing_weather_date_uses_the_sentence(self):
        for phrase in ("오늘", None, "모레"):
            r = self.weather("낼 서울 비 와?", phrase)
            self.assertEqual((r["날짜"], r["날짜글자"]), ("내일", "낼"), phrase)

    def test_date_in_the_sentence_from_the_model_is_kept(self):
        self.assertEqual(self.weather("어제 말한 내일 서울 날씨", "내일")["날짜"], "내일")
        self.assertEqual(self.weather("오늘 서울 추워?", "오늘")["날짜"], "오늘")

    def test_no_date_in_the_sentence_stays_today(self):
        self.assertEqual(self.weather("서울 비 와?", None)["날짜"], "오늘")

    def test_unsupported_date_words_stay_unknown(self):
        self.assertIsNone(self.weather("이번 주말 서울 비 와?", "이번 주말")["날짜"])
        self.assertIsNone(self.weather("다음 주 23일 서울 비 와?", "다음 주")["날짜"])
        self.assertIsNone(self.weather("3일 뒤 서울 비 와?", None)["날짜"])

    def test_missed_relative_days_are_not_today(self):
        for text in ("3일 뒤 부산 날씨", "3일 후 부산 날씨", "3일 전 부산 날씨", "3일뒤 부산 날씨"):
            for phrase in (None, "오늘", "3일"):
                r = self.weather(text, phrase)
                self.assertIsNone(r["날짜"], (text, phrase))
                self.assertIn("3일", r["날짜글자"])
        self.assertEqual(self.weather("내일 부산 비 와? 3일 뒤 출장 가", "내일")["날짜"], "내일")

    def test_memo_words_correct_schedule_intent(self):
        def p(text, content):
            r = classifier.parse('{"의도": "일정등록", "내용": "%s"}' % content, text)
            return r["의도"], r["내용"]
        self.assertEqual(p("엄마 생일 11월 3일 기억해줘", "엄마 생일 11월 3일"), ("메모저장", "엄마 생일 11월 3일"))
        self.assertEqual(p("내일 3시 병원 메모해줘", "내일 3시 병원"), ("메모저장", "내일 3시 병원"))
        self.assertEqual(p("내일 3시 회의 메모 일정 잡아줘", "내일 3시 회의 메모"), ("일정등록", "내일 3시 회의 메모"))
        self.assertEqual(p("내일 3시 치과 일정 잡아줘", "내일 3시 치과"), ("일정등록", "내일 3시 치과"))

    def test_dropped_schedule_date_is_restored(self):
        self.assertEqual(self.event("10월 30일 저녁 7시 친구 약속 일정 넣어줘", "저녁 7시 친구 약속"), "10월 30일 저녁 7시 친구 약속")
        self.assertEqual(self.event("낼 3시 치과 일정 잡아줘", "3시 치과"), "낼 3시 치과")

    def test_restored_date_is_used_by_schedule_parse(self):
        from zoneinfo import ZoneInfo
        from features import schedule
        now = datetime.datetime(2026, 10, 20, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        content = self.event("10월 30일 저녁 7시 친구 약속 일정 넣어줘", "저녁 7시 친구 약속")
        self.assertEqual(schedule.parse(content, now)["at"].strftime("%m-%d %H:%M"), "10-30 19:00")

    def test_schedule_content_with_a_date_or_relative_time_is_kept(self):
        self.assertEqual(self.event("내일 3시 치과 일정 잡아줘", "내일 3시 치과"), "내일 3시 치과")
        self.assertEqual(self.event("10분 뒤에 빨래 알려줘", "10분 뒤에 빨래"), "10분 뒤에 빨래")
        self.assertEqual(self.event("오후 3시 치과 알림 잡아줘", "오후 3시 치과"), "오후 3시 치과")

    def test_memo_is_not_touched(self):
        r = classifier.parse('{"의도": "메모저장", "내용": "치과"}', "내일 치과 메모해줘")
        self.assertEqual(r["내용"], "치과")


class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    def raise_for_status(self):
        if self.status >= 400:
            raise classifier.requests.HTTPError(str(self.status))

    def json(self):
        return self.data


class AskModel(unittest.TestCase):
    def setUp(self):
        self.real_post, self.sent = classifier.requests.post, []

    def tearDown(self):
        classifier.requests.post = self.real_post

    def test_request_and_response(self):
        def post(url, json=None, timeout=None, headers=None):
            self.sent.append((url, json))
            return FakeResp({"message": {"content": '{"의도": "일반대화"}'}})
        classifier.requests.post = post
        r, raw, secs = classifier.classify("안녕", "m1")
        self.assertEqual(r["의도"], "일반대화")
        url, body = self.sent[0]
        self.assertTrue(url.endswith("/api/chat"))
        self.assertEqual((body["model"], body["format"], body["options"]["temperature"], body["stream"]), ("m1", "json", 0, False))
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1]["content"], "문장: 안녕")

    def test_errors(self):
        classifier.requests.post = lambda *a, **k: FakeResp({}, status=404)
        with self.assertRaises(classifier.ClassifierError):
            classifier.ask_model("x", "m")
        classifier.requests.post = lambda *a, **k: FakeResp({"oops": 1})
        with self.assertRaises(classifier.ClassifierError):
            classifier.ask_model("x", "m")


class ScheduleDelete(unittest.TestCase):
    def p(self, content, text=None):
        return classifier.parse('{"의도": "일정삭제", "내용": %s}' % content, text)["내용"]

    def test_number(self):
        self.assertEqual(self.p('"3"', "3번 일정 지워줘"), 3)
        self.assertEqual(self.p('"#3번"'), 3)
        self.assertIsNone(self.p("null", "일정 지워줘"))

    def test_invented_number_is_dropped(self):
        self.assertIsNone(self.p('"1"', "방금 잡은 일정 지워줘"))
        self.assertIsNone(classifier.parse('{"의도": "메모삭제", "내용": "1"}', "방금 쓴 메모 지워줘")["내용"])

    def test_weekday_is_put_back_when_model_drops_it(self):
        raw = '{"의도": "일정등록", "내용": "15시 치과"}'
        self.assertEqual(classifier.parse(raw, "금요일 15시 치과 일정 잡아줘")["내용"], "금요일 15시 치과")
        raw = '{"의도": "일정등록", "내용": "이번주 금요일 15시 치과"}'
        self.assertEqual(classifier.parse(raw, "이번주 금요일 15시 치과 일정 잡아줘")["내용"], "이번주 금요일 15시 치과")


class EvalData(unittest.TestCase):
    def test_cases_are_consistent(self):
        ids = [c[0] for c in ev.CASES]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))
        for _, text, intent, region, date in ev.CASES:
            self.assertTrue(text)
            if intent not in ev.SLOT_INTENTS:
                self.assertEqual((region, date), (None, None), text)
            if intent != "날씨":
                self.assertIsNone(date, text)
            self.assertIn(date, ("어제", "오늘", "내일", "모레", ev.DATE, None), text)

    def test_new_sentences_not_in_prompt_examples(self):
        for case in ev.CASES:
            if case[0] >= 28:
                self.assertNotIn(f"문장: {case[1]}\n", classifier.SYSTEM_PROMPT, case[1])


if __name__ == "__main__":
    unittest.main()
