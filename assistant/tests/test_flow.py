"""자유 문장 -> 분류기 -> 날씨/일반대화, 기본 위치 묻기·저장 흐름 (모델·네트워크 없이)"""
import datetime, json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from core import assistant as A
from core import classifier, config, users
from clients import llm
from features import notes, weather

SAMPLE = {
    "current": {"time": "2026-10-20T16:45", "temperature_2m": 18.0, "apparent_temperature": 17.0,
                "relative_humidity_2m": 55, "precipitation": 0.0, "weather_code": 61, "wind_speed_10m": 9.0},
    "daily": {"time": ["2026-10-20", "2026-10-21"], "weather_code": [61, 2],
              "temperature_2m_max": [20, 21], "temperature_2m_min": [12, 13],
              "precipitation_probability_max": [80, 10], "precipitation_sum": [5.0, 0.0]},
}
REAL = (classifier.ask_model, llm.chat, weather.fetch, weather.geocode, weather.today)


class Flow(unittest.TestCase):
    def setUp(self):
        self.real_db, config.DB_PATH = config.DB_PATH, os.path.join(tempfile.mkdtemp(), "f.db")
        self.real_cfg = (config.KAKAO_REST_API_KEY, config.WEATHER_LOOKUP)
        config.KAKAO_REST_API_KEY, config.WEATHER_LOOKUP = "", "both"
        self.answers = []      # 분류 모델이 돌려줄 응답(차례대로)
        self.asked = []        # 분류 모델에 보낸 문장
        self.chats, self.fetched = [], []

        def ask(text, model=None, timeout=120):
            self.asked.append(text)
            a = self.answers.pop(0) if self.answers else {"의도": "일반대화"}
            if isinstance(a, Exception):
                raise a
            return (a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)), 0.0
        classifier.ask_model = ask
        llm.chat = lambda messages, model=None, timeout=300: self.chats.append(messages) or "대화 답"
        weather.fetch = lambda lat=None, lon=None, timeout=10: self.fetched.append((lat, lon)) or SAMPLE
        weather.geocode = lambda name, timeout=10: None
        weather.today = lambda: datetime.date(2026, 10, 20)  # SAMPLE 의 첫 날
        self.bot = A.Assistant()

    def tearDown(self):
        classifier.ask_model, llm.chat, weather.fetch, weather.geocode, weather.today = REAL
        config.KAKAO_REST_API_KEY, config.WEATHER_LOOKUP = self.real_cfg
        config.DB_PATH = self.real_db

    def say(self, text, *answers):
        self.answers.extend(answers)
        return self.bot.handle("owner", text)

    # --- 분류 결과에 따라 ---
    def test_weather_sentence_goes_to_weather_not_chat(self):
        out = self.say("어쩔티비 부산 비 와?", {"의도": "날씨", "지역": "부산", "날짜": None})
        self.assertIn("📍 부산 날씨", out)
        self.assertEqual(self.chats, [])
        self.assertEqual(self.fetched, [weather.CITIES["부산"]])

    def test_tomorrow(self):
        out = self.say("내일 부산 우산 필요해?", {"의도": "날씨", "지역": "부산", "날짜": "내일"})
        self.assertNotIn("지금:", out)  # 내일이면 '지금' 줄이 없음

    def test_general_chat(self):
        self.assertEqual(self.say("파이썬 정렬", {"의도": "일반대화"}), "대화 답")
        self.assertEqual(len(self.chats), 1)

    def test_word_nalssi_no_longer_intercepted(self):
        # 예전 규칙은 '날씨' 글자만 보고 가로챘다. 이제는 분류기가 일반대화로 보면 대화로 간다
        self.assertEqual(self.say("쓸 만한 날씨 앱 추천해줘", {"의도": "일반대화"}), "대화 답")
        self.assertEqual(self.fetched, [])

    def test_unsupported_date_asks_for_a_date(self):
        out = self.say("이번 주말 서울 날씨", {"의도": "날씨", "지역": "서울", "날짜": "이번 주말"})
        self.assertIn("'이번 주말'은(는) 날짜로 알 수 없어요", out)
        self.assertIn("10월 23일", out)
        self.assertEqual(self.fetched, [])

    def test_day_after_tomorrow_is_supported(self):
        out = self.say("모레 서울 날씨", {"의도": "날씨", "지역": "서울", "날짜": "모레"})
        self.assertIn("📍 서울 날씨", out)
        self.assertNotIn("지금:", out)
        self.assertIn("10/22(목): 데이터가 없어요", out)
        self.assertNotIn("기준)", out)  # 시간 기준 표시는 오늘 질문에만  # SAMPLE 은 이틀치뿐

    def test_ambiguous_place(self):
        out = self.say("고성 비 와?", {"의도": "날씨", "지역": "고성", "날짜": None})
        self.assertIn("여러 곳", out)

    # --- 메모 ---
    def test_memo_save_from_sentence(self):
        out = self.say("내일 치과 3시 메모해줘", {"의도": "메모저장", "내용": "내일 치과 3시"})
        self.assertIn("메모 #1 저장했습니다.", out)
        self.assertEqual([r[1] for r in notes.recent("owner")], ["내일 치과 3시"])
        self.assertEqual(self.chats, [])

    def test_memo_save_with_invented_content_is_not_saved(self):
        out = self.say("우유 적어 놔", {"의도": "메모저장", "내용": "계란 사기"}, {"의도": "메모저장", "내용": "계란 사기"})
        self.assertIn("이해하지 못했어요", out)
        self.assertEqual(notes.recent("owner"), [])

    def test_schedule_add_from_sentence_saves_and_asks_mode(self):
        from features import schedule
        out = self.say("내일 15시 치과 일정 잡아줘", {"의도": "일정등록", "내용": "내일 15시 치과"})
        self.assertIn("등록했어요", out)
        self.assertIn("1. 미리 알림", out)
        self.assertEqual([r[2] for r in schedule.upcoming("owner")], ["치과"])
        self.assertEqual(self.chats, [])

    def test_schedule_sentence_without_time_asks(self):
        out = self.say("내일 치과 일정 잡아줘", {"의도": "일정등록", "내용": "내일 치과"})
        self.assertIn("몇 시", out)

    def test_schedule_misread_without_time_or_request_goes_to_chat(self):
        out = self.say("제주도 여행 3일 코스 짜줘", {"의도": "일정등록", "내용": "제주도 여행 3일 코스"})
        self.assertEqual(out, "대화 답")

    def test_schedule_relative_from_sentence(self):
        out = self.say("1분뒤에 qa 테스트 알려줘", {"의도": "일정등록", "내용": "1분뒤에 qa 테스트"})
        self.assertIn("qa 테스트 (1분 뒤)", out)

    def test_schedule_relative_without_request_word_is_kept(self):
        out = self.say("10분 뒤 빨래", {"의도": "일정등록", "내용": "10분 뒤 빨래"})
        self.assertIn("빨래 (10분 뒤)", out)

    def test_schedule_query_from_sentence(self):
        self.assertIn("일정이 없습니다", self.say("내 일정 뭐 있어?", {"의도": "일정조회"}))

    def test_memo_list_and_search(self):
        notes.add("owner", "치과 3시")
        notes.add("owner", "우유 사기")
        self.assertIn("우유 사기", self.say("내 메모 보여줘", {"의도": "메모조회", "내용": None}))
        out = self.say("치과 메모 찾아줘", {"의도": "메모조회", "내용": "치과"})
        self.assertIn("치과 3시", out)
        self.assertNotIn("우유", out)

    def test_memo_summary_goes_to_model(self):
        notes.add("owner", "치과 3시")
        self.say("메모 정리해줘", {"의도": "메모요약", "내용": None})
        self.assertIn("치과 3시", self.chats[0][-1]["content"])

    def test_memo_delete_needs_confirm(self):
        nid = notes.add("owner", "치과 3시")
        out = self.say(f"{nid}번 메모 지워줘", {"의도": "메모삭제", "내용": str(nid)})
        self.assertIn("지울까요", out)
        self.assertIn("치과 3시", out)
        self.assertEqual(len(notes.recent("owner")), 1)       # 아직 안 지워졌다
        out = self.bot.handle("owner", "확인")                 # '!' 없이도 된다
        self.assertEqual(out, f"메모 #{nid} 삭제했습니다.")
        self.assertEqual(notes.recent("owner"), [])
        self.assertNotEqual(self.bot.handle("owner", "확인"), f"메모 #{nid} 삭제했습니다.")  # 한 번만

    def test_memo_delete_confirm_with_bang_and_for_non_admin(self):
        nid = notes.add("owner", "치과 3시")
        self.say(f"{nid}번 메모 지워줘", {"의도": "메모삭제", "내용": str(nid)})
        out = self.bot.handle("owner", "!확인", allow_admin=False)  # 관리자가 아니어도 자기 메모 삭제 확인은 된다
        self.assertEqual(out, f"메모 #{nid} 삭제했습니다.")

    def test_memo_delete_cancelled_by_other_message(self):
        nid = notes.add("owner", "치과 3시")
        self.say(f"{nid}번 메모 지워줘", {"의도": "메모삭제", "내용": str(nid)})
        self.say("아니 됐어", {"의도": "일반대화"})
        self.bot.handle("owner", "확인")
        self.assertEqual(len(notes.recent("owner")), 1)

    def test_memo_delete_accepts_yes_words(self):
        for word in ("어", "네.", "OK", "응!", "해줘"):
            nid = notes.add("owner", "치과 3시")
            self.say(f"{nid}번 메모 지워줘", {"의도": "메모삭제", "내용": str(nid)})
            self.assertEqual(self.bot.handle("owner", word), f"메모 #{nid} 삭제했습니다.", word)
        self.assertEqual(notes.recent("owner"), [])

    def test_memo_delete_no_words_cancel_with_notice(self):
        nid = notes.add("owner", "치과 3시")
        for word in ("아니", "취소", "no", "ㄴㄴ"):
            self.say(f"{nid}번 메모 지워줘", {"의도": "메모삭제", "내용": str(nid)})
            self.assertEqual(self.bot.handle("owner", word), "메모 삭제 요청을 취소했어요.", word)
        self.assertEqual(len(notes.recent("owner")), 1)

    def test_other_message_cancels_with_notice_and_is_answered(self):
        nid = notes.add("owner", "치과 3시")
        self.say(f"{nid}번 메모 지워줘", {"의도": "메모삭제", "내용": str(nid)})
        out = self.say("안녕", {"의도": "일반대화"})
        self.assertEqual(out, "(메모 삭제 요청을 취소했어요.)\n대화 답")

    def test_yes_word_without_pending_is_normal_chat(self):
        self.assertEqual(self.say("어", {"의도": "일반대화"}), "대화 답")

    def test_yes_word_after_expiry_is_normal_chat(self):
        nid = notes.add("owner", "치과 3시")
        self.say(f"{nid}번 메모 지워줘", {"의도": "메모삭제", "내용": str(nid)})
        self.bot.pending_note["owner"] = (nid, time.time() - 1)
        self.assertEqual(self.say("어", {"의도": "일반대화"}), "대화 답")
        self.assertEqual(len(notes.recent("owner")), 1)

    def test_memo_delete_expires(self):
        nid = notes.add("owner", "치과 3시")
        self.say(f"{nid}번 메모 지워줘", {"의도": "메모삭제", "내용": str(nid)})
        u, (i, until) = "owner", self.bot.pending_note["owner"]
        self.bot.pending_note[u] = (i, time.time() - 1)
        self.bot.handle("owner", "확인")
        self.assertEqual(len(notes.recent("owner")), 1)

    def test_memo_delete_without_number_or_missing(self):
        self.assertIn("메모가 없습니다", self.say("방금 메모 지워줘", {"의도": "메모삭제", "내용": None}))
        nid = notes.add("owner", "남길 메모")
        out = self.say("방금 메모 지워줘", {"의도": "메모삭제", "내용": None})
        self.assertIn("남길 메모", out)
        self.assertIn("몇 번", out)
        notes.delete("owner", nid)
        self.assertIn("해당 번호의 메모가 없습니다", self.say("99번 메모 지워줘", {"의도": "메모삭제", "내용": "99"}))
        self.assertNotIn("owner", self.bot.pending_note)

    def test_cannot_delete_other_users_note(self):
        nid = notes.add("family1", "남의 메모")
        self.assertIn("해당 번호의 메모가 없습니다", self.say(f"{nid}번 메모 지워줘", {"의도": "메모삭제", "내용": str(nid)}))
        self.assertEqual(len(notes.recent("family1")), 1)

    def test_bang_delete_is_still_immediate(self):
        nid = notes.add("owner", "치과 3시")
        self.assertEqual(self.bot.handle("owner", f"!메모 삭제 {nid}"), "삭제했습니다.")

    def test_memo_subcommands_are_not_saved_as_content(self):
        self.assertIn("저장된 메모가 없습니다", self.bot.handle("owner", "!메모 목록"))
        self.assertIn("저장된 메모가 없습니다", self.bot.handle("owner", "!메모목록"))
        self.assertIn("메모가 없습니다", self.bot.handle("owner", "!메모 삭제"))
        self.assertIn("단어를", self.bot.handle("owner", "!메모 찾기"))
        self.assertEqual(notes.recent("owner"), [])

    def test_memo_delete_phrases_need_confirm(self):
        for phrase in ("{} 지워", "{}번 삭제해줘", "#{} 삭제", "{}번 없애줘", "{} 지워주세요"):
            nid = notes.add("owner", "지울 메모")
            out = self.bot.handle("owner", "!메모 " + phrase.format(nid))
            self.assertIn("지울까요", out, phrase)
            self.assertIn("30초", out)
            self.assertEqual(notes.get("owner", nid)[0], nid)
            self.assertIn("삭제했습니다", self.bot.handle("owner", "네"))
            self.assertIsNone(notes.get("owner", nid))

    def test_memo_delete_without_number_shows_list_and_saves_nothing(self):
        nid = notes.add("owner", "남길 메모")
        for text in ("!메모 지워", "!메모 삭제", "!메모 삭제해줘"):
            out = self.bot.handle("owner", text)
            self.assertIn("남길 메모", out, text)
            self.assertIn("몇 번", out, text)
        self.assertEqual([r[0] for r in notes.recent("owner")], [nid])

    def test_memo_bulk_asks_twice(self):
        a, b = notes.add("owner", "하나"), notes.add("owner", "둘")
        keep = notes.add("family1", "남의 메모")
        for text in ("메모 다 지워줘", "내 메모 전부 삭제해줘", "!메모 다 지워", "!메모 전체 삭제"):
            out = self.bot.handle("owner", text)
            self.assertIn("메모 2개를 모두 지울까요", out, text)
            self.assertIn("정말", self.bot.handle("owner", "네"))
            self.assertIsNotNone(notes.get("owner", a))
            self.bot.handle("owner", "아니")
        self.assertEqual(self.asked, [])
        self.bot.handle("owner", "메모 다 지워줘")
        self.bot.handle("owner", "네")
        self.assertIn("메모 2개를 지웠어요", self.bot.handle("owner", "네"))
        self.assertIsNone(notes.get("owner", b))
        self.assertIsNotNone(notes.get("family1", keep))
        self.assertIn("지울 메모가 없습니다", self.bot.handle("owner", "메모 다 지워줘"))
        self.assertIn("기준이 없어요", self.bot.handle("owner", "!메모 지난 거 다 지워"))

    def test_relative_days_weather_does_not_answer_today(self):
        out = self.say("3일 뒤 부산 날씨", {"의도": "날씨", "지역": "부산", "날짜": None, "내용": None})
        self.assertIn("'3일 뒤'은(는) 날짜로 알 수 없어요", out)
        self.assertEqual(self.fetched, [])

    def test_memo_sentence_read_as_schedule_is_saved_as_memo(self):
        out = self.say("엄마 생일 11월 3일 기억해줘", {"의도": "일정등록", "내용": "엄마 생일 11월 3일"})
        self.assertIn("저장했습니다", out)
        self.assertEqual(notes.recent("owner")[0][1], "엄마 생일 11월 3일")

    def test_memo_old_delete_is_still_immediate(self):
        nid = notes.add("owner", "바로 지움")
        self.assertEqual(self.bot.handle("owner", f"!메모 삭제 {nid}"), "삭제했습니다.")

    def test_memo_text_that_only_looks_like_delete_is_saved(self):
        self.assertIn("저장했습니다", self.bot.handle("owner", "!메모 3번 파일 지워야 함"))

    def test_delete_sentence_goes_by_the_word_not_the_model(self):
        nid = notes.add("owner", "남길 메모")
        out = self.say(f"{nid}번 메모 지워줘", {"의도": "일정삭제", "내용": str(nid)})
        self.assertIn("이 메모를 지울까요", out)
        self.bot.handle("owner", "아니")

    def test_delete_sentence_without_target_deletes_nothing(self):
        nid = notes.add("owner", "남길 메모")
        for text in (f"{nid}번 지워줘", f"{nid}번 메모 일정 지워줘"):
            self.assertIn("메모인지 일정인지", self.say(text, {"의도": "메모삭제", "내용": str(nid)}), text)
            self.assertNotIn("owner", self.bot.pending_note)
        self.assertIsNotNone(notes.get("owner", nid))

    def test_pasted_memo_line_asks_about_that_memo_without_model(self):
        nid = notes.add("owner", "잠온다")
        line = notes.fmt(notes.recent("owner"))
        for tail in ("지워", "이거 지워줘"):
            out = self.bot.handle("owner", f"{line} {tail}")
            self.assertIn("이 메모를 지울까요", out, tail)
            self.assertIn("잠온다", out)
            self.bot.handle("owner", "아니")
        self.assertEqual(self.asked, [])
        self.assertIsNotNone(notes.get("owner", nid))

    def test_memo_content_starting_with_subcommand_word_is_saved(self):
        self.assertIn("저장했습니다", self.bot.handle("owner", "!메모 삭제 요청 보내기"))
        self.assertIn("저장했습니다", self.bot.handle("owner", "!메모 목록 정리하기"))
        self.assertEqual({r[1] for r in notes.recent("owner")}, {"삭제 요청 보내기", "목록 정리하기"})

    def test_memo_subcommands_joined_and_spaced(self):
        nid = notes.add("owner", "치과 3시")
        self.assertIn("치과", self.bot.handle("owner", "!메모찾기 치과"))
        self.assertIn("치과", self.bot.handle("owner", "!메모 목록 5"))
        self.assertEqual(self.bot.handle("owner", f"!메모삭제 {nid}"), "삭제했습니다.")

    def test_bare_list_find_delete_are_gone(self):
        nid = notes.add("owner", "치과 3시")
        for text in ("!목록", "!찾기 치과", f"!삭제 {nid}"):
            self.assertIn("모르는 명령어", self.bot.handle("owner", text))
        self.assertEqual(len(notes.recent("owner")), 1)

    def test_sentence_memo_with_subcommand_word_is_saved(self):
        out = self.say("목록 메모해줘", {"의도": "메모저장", "내용": "목록"})
        self.assertIn("저장했습니다", out)

    def test_bad_format_memoish_asks_again(self):
        out = self.say("우유 적어 놔", "x", "y")
        self.assertIn("메모 요청인지", out)
        self.assertEqual(self.chats, [])

    # --- 형식 실패 ---
    def test_bad_format_retries_once_then_chat(self):
        out = self.say("오늘 기분 별로야", "형식 아님", "또 형식 아님")
        self.assertEqual(out, "대화 답")
        self.assertEqual(len(self.asked), 2)

    def test_bad_format_then_ok_on_retry(self):
        out = self.say("부산 비 와?", "형식 아님", {"의도": "날씨", "지역": "부산", "날짜": None})
        self.assertIn("📍 부산 날씨", out)

    def test_bad_format_weatherish_asks_again(self):
        out = self.say("부산 비 와?", "x", "y")
        self.assertIn("다시 말씀해 주세요", out)
        self.assertEqual(self.chats, [])

    def test_classifier_connection_error(self):
        out = self.say("안녕", classifier.ClassifierError("Ollama 호출 오류"))
        self.assertIn("⚠️", out)

    # --- 기본 위치 ---
    def test_no_default_asks_then_saves_and_answers(self):
        out = self.say("비 와?", {"의도": "날씨", "지역": None, "날짜": "내일"})
        self.assertIn("기본 위치가 아직 없어요", out)
        out = self.say("부산")
        self.assertIn("기본 위치를 '부산'(으)로 저장했어요", out)
        self.assertIn("📍 부산 날씨", out)
        self.assertNotIn("지금:", out)  # 처음 물었던 '내일'로 답함
        self.assertEqual(users.get_setting("owner", "기본위치"), "부산")
        self.assertEqual(len(self.asked), 1)  # 답('부산')은 분류기에 보내지 않음
        out = self.say("우산 필요해?", {"의도": "날씨", "지역": None, "날짜": None})
        self.assertIn("📍 부산 날씨", out)  # 이후에는 기본 위치로 바로

    def test_waiting_ambiguous_then_ok(self):
        self.say("비 와?", {"의도": "날씨", "지역": None, "날짜": None})
        self.assertIn("여러 곳", self.say("고성"))
        self.assertIn("저장했어요", self.say("대구"))

    def test_waiting_not_found_twice_falls_back(self):
        self.say("비 와?", {"의도": "날씨", "지역": None, "날짜": None})
        self.assertIn("찾지 못했어요", self.say("아무데나"))
        out = self.say("몰라 됐어", {"의도": "일반대화"})  # 두 번째도 못 찾으면 평소 문장으로
        self.assertEqual(out, "대화 답")
        self.assertIsNone(users.get_setting("owner", "기본위치"))

    def test_waiting_long_sentence_or_command_cancels(self):
        self.say("비 와?", {"의도": "날씨", "지역": None, "날짜": None})
        self.assertEqual(self.say("그건 됐고 파이썬 리스트 정렬하는 법 알려줘", {"의도": "일반대화"}), "대화 답")
        self.say("비 와?", {"의도": "날씨", "지역": None, "날짜": None})
        self.assertIn("요일", self.say("!날짜"))
        self.assertNotIn("owner", self.bot.waiting)

    def test_waiting_expires(self):
        self.say("비 와?", {"의도": "날씨", "지역": None, "날짜": None})
        kind, data, _ = self.bot.waiting["owner"]
        self.bot.waiting["owner"] = (kind, data, time.time() - 1)
        self.assertEqual(self.say("부산", {"의도": "일반대화"}), "대화 답")

    def test_default_place_command(self):
        self.assertIn("기본 위치가 없습니다", self.say("!기본위치"))
        self.assertIn("저장했어요", self.say("!기본위치 대전"))
        self.assertIn("기본 위치: 대전", self.say("!기본위치"))
        self.assertIn("여러 곳", self.say("!기본위치 고성"))
        self.assertIn("찾지 못했습니다", self.say("!기본위치 아무데나"))
        self.assertEqual(users.get_setting("owner", "기본위치"), "대전")
        self.assertEqual(self.asked, [])  # 명령어는 분류기를 쓰지 않음

    def test_weather_command_uses_user_default(self):
        self.assertIn("기본 위치가 아직 없어요", self.say("!날씨"))
        self.say("!기본위치 광주")
        self.assertIn("📍 광주 날씨", self.say("!날씨"))
        self.assertIn("📍 대구 날씨", self.say("!날씨 대구"))
        self.assertEqual(self.asked, [])

    def test_first_word_is_not_a_command_without_bang(self):
        # '!' 없이는 첫 단어가 명령어 이름이어도 분류기가 뜻을 읽는다
        self.answers.append({"의도": "일반대화", "지역": None, "날짜": None})
        self.say("날씨 앱 추천해줘")
        self.assertEqual(self.asked, ["날씨 앱 추천해줘"])
        self.assertEqual(self.bot.history, self.bot.history)  # 메모 등 명령 실행 안 됨

    def test_memo_word_is_not_saved_without_bang(self):
        self.answers.append({"의도": "일반대화", "지역": None, "날짜": None})
        self.say("메모 앱 추천해줘")
        self.assertEqual(self.asked, ["메모 앱 추천해줘"])
        self.assertNotIn("앱 추천해줘", self.say("!메모 목록"))

    def test_unknown_bang_command(self):
        self.assertIn("모르는 명령어", self.say("!없는명령"))
        self.assertEqual(self.asked, [])

    def test_defaults_are_per_user(self):
        users.set_setting("owner", "기본위치", "부산")
        self.answers.append({"의도": "날씨", "지역": None, "날짜": None})
        self.assertIn("기본 위치가 아직 없어요", self.bot.handle("family1", "비 와?"))


if __name__ == "__main__":
    unittest.main()
