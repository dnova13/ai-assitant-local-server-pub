"""디스코드 채널 관리: 지정, 저장, 문장 읽기, 찾기, 삭제 권한과 확인, 입구 (모델·네트워크·디스코드 없음)"""
import json, os, sys, tempfile, threading, unittest
from datetime import datetime, timedelta
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from clients import db
from core import apikeys, assistant as A, classifier, config, users
from entrances import discord_rules as R
from features import channel_manage as C, schedule

NOW = datetime(2026, 11, 4, 10, 0)  # 수요일
ROOM = {"id": "100", "name": "자료실", "guild": "우리 서버"}
OTHER = {"id": "200", "name": "다른방", "guild": "우리 서버"}
ADMIN, FRIEND = "111", "999"


def row(mid, text="", author=FRIEND, name="민수", at=NOW, channel="100", files=()):
    return {"id": str(mid), "channel_id": channel, "author_id": author, "author_name": name, "text": text,
            "link": f"https://x/{mid}", "at": at, "files": list(files)}


class Base(unittest.TestCase):
    def setUp(self):
        self.real = (config.DB_PATH, schedule.now)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "c.db")
        schedule.now = lambda: NOW
        C._pending.clear()
        C._memory.clear()
        users.bootstrap([])
        users.link("owner", "owner", "discord", ADMIN)
        self.answer = {"동작": "찾기", "찾는말": None, "사람": None}
        self.asked = []

        def ask(prompt, text, model=None, timeout=120):
            self.asked.append(text)
            return json.dumps(self.answer, ensure_ascii=False), {}
        patch = mock.patch.object(classifier, "ask_system", side_effect=ask)
        patch.start()
        self.addCleanup(patch.stop)
        self.lock = threading.RLock()
        C.command("지정", ROOM)

    def tearDown(self):
        config.DB_PATH, schedule.now = self.real

    def req(self, text, author=FRIEND, mid="7777"):
        return C.request("100", mid, author, text, self.lock)


class Commands(Base):
    def test_add_words_and_list(self):
        self.assertIn("이미", C.command("등록", ROOM))
        self.assertIn("지정했어요", C.command("등록", OTHER))
        out = C.command("목록")
        self.assertIn("#자료실 (우리 서버)", out)
        self.assertIn("#다른방", out)

    def test_needs_place_or_name(self):
        self.assertIn("채널 안에서", C.command("지정"))
        self.assertIn("!채널관리 목록", C.command("사용법"))

    def test_by_name(self):
        self.assertIn("지정했어요", C.command("다른방 등록", None, [OTHER]))
        self.assertIn("#다른방", C.command("목록"))
        self.assertIn("찾지 못했어요", C.command("없는방 등록", None, []))

    def test_same_name_asks(self):
        out = C.command("회의 등록", None, [{"id": "1", "name": "회의", "guild": "가"}, {"id": "2", "name": "회의", "guild": "나"}])
        self.assertIn("여럿", out)
        self.assertIn("(가)", out)
        self.assertEqual(len(C.managed()), 1)

    def test_release_asks_then_deletes_stored_only(self):
        C.store([row(1, "글", files=[{"filename": "a.pdf", "kind": "file", "size": 1}])])
        out = C.command("해제", ROOM)
        self.assertIn("글 1개와 파일 정보 1개", out)
        self.assertIn("!채널관리 해제 확인", out)
        self.assertEqual(C._counts("100"), (1, 1))
        self.assertIn("해제하고", C.command("삭제 확인", ROOM))
        self.assertEqual(C._counts("100"), (0, 0))
        self.assertEqual(C.managed(), [])

    def test_release_by_stored_name_when_bot_cannot_find(self):
        self.assertIn("!채널관리 자료실 해제 확인", C.command("자료실 해제", None, []))
        self.assertIn("해제하고", C.command("자료실 해제 확인", None, []))

    def test_non_admin_refused_in_assistant(self):
        bot = A.Assistant()
        self.assertIn("관리자만", bot.handle("owner", "!채널관리 목록", allow_admin=False))
        self.assertIn("#자료실", bot.handle("owner", "!채널관리 목록"))
        self.assertIn("지정했어요", bot.handle("owner", "!채널관리 지정", channel=OTHER))
        self.assertIn("지정했어요", bot.handle("owner", "!채널관리 새방 등록", targets=[{"id": "300", "name": "새방", "guild": ""}]))

    def test_rules(self):
        self.assertTrue(R.is_channel_command("!채널관리 지정"))
        self.assertTrue(R.is_channel_command("/채널관리"))
        self.assertFalse(R.is_channel_command("!채널관리자"))
        self.assertFalse(R.is_channel_command("채널관리 지정"))
        self.assertEqual(R.channel_word("!채널관리 자료실 등록"), "자료실")
        self.assertIsNone(R.channel_word("!채널관리"))

    def test_backlog_drops_oldest(self):
        b = R.Backlog(limit=2)
        self.assertEqual([b.push(1), b.push(2), b.push(3)], [0, 0, 1])
        self.assertEqual([b.pop(), b.pop(), b.pop()], [2, 3, None])


class Store(Base):
    def test_only_managed_channel(self):
        C.store([row(1, "저장"), row(2, "안 저장", channel="200")])
        self.assertEqual(C._counts("100")[0], 1)
        self.assertEqual(C._counts("200")[0], 0)

    def test_needs_reply(self):
        self.assertFalse(C.store([row(1, "ㅋㅋ 오늘 뭐 먹지")], live=True))
        self.assertFalse(C.store([row(2, "내일 날씨 어때?")], live=True))
        self.assertTrue(C.store([row(3, "어제 사진 보여줘")], live=True))
        self.assertFalse(C.store([row(4, "사진 보여줘", channel="200")], live=True))
        self.assertFalse(C.store([row(5, "사진 보여줘")]))  # 지난 글을 채울 때는 묻지 않는다

    def test_waiting_answer_needs_reply(self):
        C._pending[("100", ADMIN)] = (["1"], float("inf"))
        self.assertTrue(C.store([row(6, "네", author=ADMIN)], live=True))

    def test_upsert_edit_forget(self):
        C.store([row(1, "처음", files=[{"filename": "a.pdf", "kind": "file", "size": 1}])])
        C.store([row(1, "다시", files=[{"filename": "b.pdf", "kind": "file", "size": 1}])])
        self.assertEqual(C._counts("100"), (1, 1))
        C.edit("1", "고침")
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT text FROM channel_messages").fetchone()[0], "고침")
        C.forget(["1"])
        self.assertEqual(C._counts("100"), (0, 0))


class Reading(Base):
    def test_parse(self):
        text = "지난주에 민수가 올린 견적서 찾아줘"
        self.assertEqual(C.parse('{"동작":"찾기","찾는말":"견적서","사람":"민수"}', text), {"동작": "찾기", "찾는말": "견적서", "사람": "민수"})
        self.assertEqual(C.parse('{"동작":"찾기","찾는말":"계약서","사람":"철수"}', text), {"동작": "찾기", "찾는말": None, "사람": None})
        self.assertIsNone(C.parse("뭔가요", text))
        self.assertIsNone(C.parse('{"동작":"폭파"}', text))
        self.assertEqual(C.parse('{"동작":"삭제"}', "견적서 찾아줘")["동작"], "아님")  # 지우라는 말이 없다

    def test_dates(self):
        cases = {"오늘": (NOW.replace(hour=0), NOW.replace(hour=0) + timedelta(days=1)),
                 "어제": (datetime(2026, 11, 3), datetime(2026, 11, 4)), "그저께": (datetime(2026, 11, 2), datetime(2026, 11, 3)),
                 "이번주": (datetime(2026, 11, 2), datetime(2026, 11, 9)), "지난 주": (datetime(2026, 10, 26), datetime(2026, 11, 2)),
                 "이번달": (datetime(2026, 11, 1), datetime(2026, 12, 1)), "지난달": (datetime(2026, 10, 1), datetime(2026, 11, 1)),
                 "10월": (datetime(2026, 10, 1), datetime(2026, 11, 1)), "10월 3일": (datetime(2026, 10, 3), datetime(2026, 10, 4)),
                 "12월": (datetime(2025, 12, 1), datetime(2026, 1, 1))}
        for text, (start, end) in cases.items():
            self.assertEqual(C.date_range(f"{text} 사진", NOW)[:2], (start, end), text)
        self.assertIsNone(C.date_range("견적서", NOW))
        self.assertIsNone(C.date_range("13월", NOW))
        self.assertEqual(C.date_range("3일 전 글", NOW)[0], datetime(2026, 11, 1))

    def test_conditions(self):
        f = C.conditions("지난주 <@55> 견적서 pdf 파일", {"찾는말": "지난주 견적서 pdf 파일", "사람": None}, NOW)
        self.assertEqual((f["word"], f["author_id"], f["ext"], f["kind"], f["file_only"]), ("견적서", "55", "pdf", None, True))
        f = C.conditions("사진 보여줘", {"찾는말": "사진", "사람": None}, NOW)
        self.assertEqual((f["word"], f["kind"]), (None, "image"))


class Find(Base):
    def setUp(self):
        super().setUp()
        C.store([row(10, "서버 점검 일정", name="민수", at=NOW - timedelta(days=1)),
                 row(11, "", name="철수", at=NOW - timedelta(days=9), files=[{"filename": "견적서.pdf", "kind": "file", "size": 9}]),
                 row(12, "", name="민수", at=NOW - timedelta(days=1), files=[{"filename": "cat.png", "kind": "image", "size": 9}]),
                 row(13, "@everyone 100% 광고", name="광고꾼", at=NOW - timedelta(days=2))])

    def find(self, text, word=None, person=None):
        self.answer = {"동작": "찾기", "찾는말": word, "사람": person}
        return self.req(text)["reply"]

    def test_anyone_can_find_with_links(self):
        out = self.find("서버 점검 찾아줘", "서버 점검")
        self.assertIn("서버 점검 일정 (민수, 11-03)", out)
        self.assertIn("<https://x/10>", out)

    def test_kind_date_person(self):
        self.assertIn("cat.png", self.find("어제 사진 보여줘"))
        self.assertNotIn("견적서", self.find("어제 사진 보여줘"))
        self.assertIn("견적서.pdf", self.find("철수가 올린 pdf 찾아줘", None, "철수"))
        self.assertIn("찾지 못했어요", self.find("어제 pdf 찾아줘"))

    def test_not_found_shows_conditions(self):
        out = self.find("계약서 찾아줘", "계약서")
        self.assertIn("찾은 조건: 계약서", out)

    def test_like_wildcards_are_plain(self):
        self.assertNotIn("서버 점검", self.find("100% 찾아줘", "100%"))
        self.assertIn("찾지 못했어요", self.find("_ 찾아줘", "_"))

    def test_no_condition_asks(self):
        self.assertIn("무엇을 찾을까요", self.find("찾아줘"))

    def test_other_channel_not_found(self):
        C.command("지정", OTHER)
        C.store([row(20, "비밀 견적", channel="200")])
        self.assertIn("찾지 못했어요", self.find("비밀 찾아줘", "비밀"))

    def test_request_itself_not_found(self):
        C.store([row(30, "튤립 찾아줘")])
        self.answer = {"동작": "찾기", "찾는말": "튤립", "사람": None}
        self.assertIn("찾지 못했어요", self.req("튤립 찾아줘", mid="30")["reply"])

    def test_no_clue_no_model_no_reply(self):
        self.assertEqual(self.req("ㅋㅋ 오늘 뭐 먹지"), {"reply": None, "delete_ids": []})
        self.assertEqual(self.asked, [])

    def test_not_a_request_stays_silent_and_stored(self):
        C.store([row(40, "그 파일 어디 갔지 ㅋㅋ")])
        self.answer = {"동작": "아님", "찾는말": None, "사람": None}
        self.assertIsNone(self.req("그 파일 어디 갔지 ㅋㅋ", mid="40")["reply"])
        with db.connect() as c:
            self.assertTrue(c.execute("SELECT 1 FROM channel_messages WHERE message_id='40'").fetchone())

    def test_bad_model_answer_silent(self):
        with mock.patch.object(classifier, "ask_system", return_value=("모름", {})) as ask:
            self.assertIsNone(self.req("사진 보여줘")["reply"])
        self.assertEqual(ask.call_count, 2)

    def test_model_down_tells(self):
        with mock.patch.object(classifier, "ask_system", side_effect=classifier.ClassifierError("모델 꺼짐")):
            self.assertIn("⚠️", self.req("사진 보여줘")["reply"])

    def test_unmanaged_channel_nothing(self):
        self.assertEqual(C.request("777", "1", ADMIN, "사진 보여줘", self.lock), {"reply": None, "delete_ids": []})


class ManyFiles(Base):
    def setUp(self):
        super().setUp()
        pictures = lambda p, n: [{"filename": f"{p}{i}.png", "kind": "image", "size": 1} for i in range(n)]
        C.store([row(1, "", at=NOW, files=pictures("가", 8)), row(2, "고양이 모음", at=NOW - timedelta(minutes=1), files=pictures("나", 5)),
                 row(3, "", at=NOW - timedelta(minutes=2), files=pictures("다", 10)),
                 row(4, "", at=NOW - timedelta(minutes=3), files=[{"filename": f"{i}.pdf", "kind": "file", "size": 1} for i in range(3)]),
                 row(5, "그냥 글", at=NOW - timedelta(minutes=4))])

    def find(self, text, word=None):
        self.answer = {"동작": "찾기", "찾는말": word, "사람": None}
        return self.req(text)["reply"]

    def test_each_file_one_line(self):
        out = self.find("오늘 사진 찾아줘")
        self.assertIn("전체 23개 중 최근 10개", out)
        self.assertEqual(out.count("<https://x/1>"), 8)
        self.assertEqual(out.count("<https://x/2>"), 2)
        self.assertIn("20개 보여줘", out)
        self.assertIn("다 보여줘", out)

    def test_count_word(self):
        out = self.find("사진 20개 보여줘", "20개")
        self.assertIn("전체 23개 중 최근 20개", out)
        self.assertEqual(out.count("<https://x/3>"), 7)
        self.assertIn("다 보여줘", out)
        self.assertIn("최근 3개", self.find("사진 3장 보여줘"))
        self.assertIn("최근 20개", self.find("사진 99개 보여줘"))

    def test_files_too(self):
        out = self.find("pdf 파일 찾아줘")
        self.assertIn("전체 3개", out)
        self.assertNotIn("중 최근", out)

    def test_finds_by_text_with_picture(self):
        out = self.find("고양이 사진 찾아줘", "고양이")
        self.assertIn("전체 5개", out)
        self.assertIn("나0.png", out)

    def test_text_line_not_repeated_when_file_shown(self):
        out = self.find("고양이 찾아줘", "고양이")
        self.assertIn("전체 5개", out)
        self.assertNotIn("고양이 모음", out)
        self.assertIn("그냥 글", self.find("그냥 글 찾아줘", "그냥 글"))

    def test_delete_counts_messages_and_attachments(self):
        self.answer = {"동작": "삭제", "찾는말": None, "사람": None}
        out = self.req("사진 다 지워줘", ADMIN)["reply"]
        self.assertIn("메시지 3개(첨부파일 23개 포함)를 지울까요", out)
        self.assertEqual(sorted(self.req("네", ADMIN)["delete_ids"]), ["1", "2", "3"])

    def test_delete_limit_counts_messages(self):
        C.store([row(100 + i, "", at=NOW - timedelta(hours=1, minutes=i), files=[{"filename": f"{i}-{j}.png", "kind": "image", "size": 1} for j in range(3)])
                 for i in range(60)])
        self.answer = {"동작": "삭제", "찾는말": None, "사람": None}
        self.req("사진 다 지워줘", ADMIN)
        self.assertEqual(len(C._pending[("100", ADMIN)][0]), 50)


class ShowAll(Base):
    def setUp(self):
        super().setUp()
        self.answer = {"동작": "찾기", "찾는말": None, "사람": None}

    def pictures(self, count, at=NOW):
        C.store([row(1000 + i, "", at=at - timedelta(minutes=i), files=[{"filename": f"p{i}.png", "kind": "image", "size": 1}])
                 for i in range(count)])

    def test_default_is_ten(self):
        self.pictures(30)
        self.assertIn("전체 30개 중 최근 10개", self.req("오늘 올린 사진")["reply"])

    def test_all_with_date(self):
        self.pictures(30)
        for text in ("오늘 올린 사진 다 보여줘", "이번 달 올린 사진 전부 보여줘"):
            out = self.req(text)
            self.assertIn("(전체 30개)", out["reply"], text)
            self.assertEqual(out["reply"].count("<https://x/"), 30)
            self.assertNotIn("file", out)

    def test_previous_month(self):
        self.pictures(12, at=datetime(2026, 10, 15, 9, 0))
        self.assertIn("(전체 12개)", self.req("저번 달 올린 사진 다 보여줘")["reply"])
        self.assertIn("찾지 못했어요", self.req("이번 달 올린 사진 다 보여줘")["reply"])

    def test_all_without_date_asks(self):
        self.pictures(3)
        self.assertEqual(self.req("사진 다 보여줘"), {"reply": C.ASK_DATE, "delete_ids": []})

    def test_over_100_goes_to_file(self):
        self.pictures(101)
        out = self.req("오늘 사진 다 보여줘")
        self.assertIn("파일로", out["reply"])
        self.assertNotIn("<https://x/", out["reply"])
        self.assertEqual(out["file"]["name"], "search-results.txt")
        self.assertEqual(out["file"]["text"].count("https://x/"), 101)

    def test_exactly_100_stays_in_message(self):
        self.pictures(100)
        self.assertNotIn("file", self.req("오늘 사진 다 보여줘"))

    def test_cap(self):
        with mock.patch.object(C, "SEARCH_CAP", 150):
            self.pictures(200)
            out = self.req("오늘 사진 다 보여줘")
        self.assertIn("150개 이상", out["reply"])
        self.assertIn("150개까지만", out["reply"])
        self.assertEqual(out["file"]["text"].count("https://x/"), 150)

    def test_bare_more_uses_memory_without_model(self):
        self.pictures(15)
        self.req("오늘 사진")
        count = len(self.asked)
        self.assertIn("(전체 15개)", self.req("다 보여줘")["reply"])
        self.assertEqual(len(self.asked), count)
        self.assertIn("(전체 15개)", self.req("더 보여줘")["reply"])

    def test_bare_more_needs_memory_with_date(self):
        self.assertEqual(self.req("다 보여줘")["reply"], C.ASK_DATE)
        self.pictures(5)
        self.req("사진 찾아줘")
        self.assertEqual(self.req("다 보여줘")["reply"], C.ASK_DATE)
        self.assertEqual(self.asked.count("다 보여줘"), 0)

    def test_memory_expires_and_is_per_person(self):
        self.pictures(5)
        self.req("오늘 사진", FRIEND)
        self.assertEqual(self.req("다 보여줘", ADMIN)["reply"], C.ASK_DATE)
        C._memory[("100", FRIEND)] = (C._memory[("100", FRIEND)][0], 0)
        self.assertEqual(self.req("다 보여줘", FRIEND)["reply"], C.ASK_DATE)


class Times(Base):
    def setUp(self):
        super().setUp()
        self.answer = {"동작": "찾기", "찾는말": None, "사람": None}
        C.store([row(i, "", at=NOW.replace(hour=hour, minute=0), files=[{"filename": f"t{hour}.png", "kind": "image", "size": 1}])
                 for i, hour in enumerate((9, 12, 13, 15), 1)])

    def names(self, text):
        return [n for n in ("t9.", "t12.", "t13.", "t15.") if n in self.req(text)["reply"]]

    def test_before_after_range(self):
        self.assertEqual(self.names("오늘 오후 1시 이전 올린 사진"), ["t9.", "t12."])
        self.assertEqual(self.names("오늘 오후 1시 이후 올린 사진"), ["t13.", "t15."])
        self.assertEqual(self.names("오늘 오후 1시까지 올린 사진"), ["t9.", "t12.", "t13."])
        self.assertEqual(self.names("오늘 오전 9시부터 오후 1시까지 올린 사진"), ["t9.", "t12.", "t13."])

    def test_condition_shown_and_no_am_pm(self):
        self.assertIn("오늘 13:00 이전", self.req("오늘 1시 이전 사진")["reply"])
        self.assertIn("오늘 09:30 이후", self.req("오늘 9시 반 이후 사진")["reply"])

    def test_without_date_is_today(self):
        self.assertEqual(self.names("오후 1시 이전 사진"), ["t9.", "t12."])

    def test_ignored_for_long_period(self):
        self.assertEqual(self.names("이번 달 오후 1시 이전 사진"), ["t9.", "t12.", "t13.", "t15."])

    def test_bad_time(self):
        self.assertIsNone(C.time_range("25시 전", NOW))
        self.assertIsNone(C.time_range("사진", NOW))

    def test_word_cleaned(self):
        f = C.conditions("오늘 오후 1시 이전 견적서", {"찾는말": "오늘 오후 1시 이전 견적서", "사람": None}, NOW)
        self.assertEqual(f["word"], "견적서")
        f = C.conditions("거래처 파일", {"찾는말": "거래처 파일", "사람": None}, NOW)
        self.assertEqual(f["word"], "거래처")


class Names(Base):
    def test_attachment_name(self):
        self.assertEqual(R.attachment_name(None, "10__.zip"), "10__.zip")
        self.assertEqual(R.attachment_name("", "10__.zip"), "10__.zip")
        self.assertEqual(R.attachment_name("劍道少女10.zip", "10__.zip"), "劍道少女10.zip")
        self.assertEqual(R.attachment_name("劍道少女10", "10__.zip"), "劍道少女10.zip")
        self.assertEqual(R.attachment_name("이름", "noext"), "이름")

    def test_find_by_original_name(self):
        C.store([row(1, "", files=[{"filename": R.attachment_name("見積書", "__.pdf"), "kind": "file", "size": 1}])])
        self.answer = {"동작": "찾기", "찾는말": "見積書", "사람": None}
        self.assertIn("見積書.pdf", self.req("見積書 찾아줘")["reply"])
        self.answer = {"동작": "찾기", "찾는말": None, "사람": None}
        self.assertIn("見積書.pdf", self.req("pdf 찾아줘")["reply"])


class ReadOnly(Base):
    def test_body_hidden_from_readonly_sql_and_db_view(self):
        from features import dbinfo
        C.store([row(1, "비밀 본문")])
        with db.connect() as c:
            self.assertEqual(db.readonly_columns(c, "channel_messages"),
                             ["message_id", "channel_id", "author_id", "author_name", "link", "at"])
            self.assertIn("filename", db.readonly_columns(c, "channel_files"))
        self.assertNotIn("비밀 본문", dbinfo.recent_rows("channel_messages"))
        for sql in ("SELECT text FROM channel_messages", "SELECT * FROM channel_messages"):
            self.assertNotIn("비밀 본문", dbinfo.run_sql(sql))

    def test_every_table_can_be_viewed(self):
        from features import dbinfo
        for table in db.TABLES:
            self.assertNotIn("Error", str(dbinfo.recent_rows(table)), table)


class Delete(Base):
    def setUp(self):
        super().setUp()
        C.store([row(i, f"광고 {i}", at=NOW - timedelta(minutes=i)) for i in range(1, 4)] + [row(9, "중요한 글")])
        self.answer = {"동작": "삭제", "찾는말": "광고", "사람": None}

    def test_non_admin_refused_without_pending(self):
        out = self.req("광고 지워줘", FRIEND)
        self.assertEqual(out, {"reply": "메시지 삭제는 관리자만 할 수 있어요.", "delete_ids": []})
        self.assertEqual(C._pending, {})

    def test_admin_confirm_then_ids(self):
        out = self.req("광고 지워줘", ADMIN)
        self.assertIn("3개를 지울까요", out["reply"])
        self.assertEqual(out["delete_ids"], [])
        done = self.req("네", ADMIN, mid="50")
        self.assertEqual(sorted(done["delete_ids"]), ["1", "2", "3"])
        self.assertIsNone(done["reply"])
        self.assertEqual(self.req("네", ADMIN, mid="51")["delete_ids"], [])  # 확인은 한 번만

    def test_no_or_other_cancels(self):
        self.req("광고 지워줘", ADMIN)
        self.assertEqual(self.req("아니오", ADMIN), {"reply": "삭제를 취소했어요.", "delete_ids": []})
        self.req("광고 지워줘", ADMIN)
        self.assertEqual(self.req("잠깐만", ADMIN)["delete_ids"], [])

    def test_other_person_cannot_confirm(self):
        self.req("광고 지워줘", ADMIN)
        self.answer = {"동작": "아님", "찾는말": None, "사람": None}
        self.assertEqual(self.req("네", FRIEND)["delete_ids"], [])
        self.assertIn(("100", ADMIN), C._pending)

    def test_admin_lost_role_before_confirm(self):
        self.req("광고 지워줘", ADMIN)
        with mock.patch.object(users, "is_admin", return_value=False):
            self.assertEqual(self.req("네", ADMIN)["delete_ids"], [])

    def test_confirm_expires(self):
        self.req("광고 지워줘", ADMIN)
        C._pending[("100", ADMIN)] = (["1"], 0)
        self.assertEqual(self.req("네", ADMIN)["delete_ids"], [])

    def test_no_condition_needs_all_word(self):
        self.answer = {"동작": "삭제", "찾는말": None, "사람": None}
        self.assertIn("어떤 메시지", self.req("지워줘", ADMIN)["reply"])
        self.assertIn("4개를 지울까요", self.req("글 다 지워줘", ADMIN)["reply"])

    def test_limit(self):
        C.store([row(100 + i, f"광고 {i}") for i in range(60)])
        out = self.req("광고 지워줘", ADMIN)
        self.assertIn("50개를 지울까요", out["reply"])
        self.assertIn("메시지 50개까지", out["reply"])
        self.assertEqual(len(C._pending[("100", ADMIN)][0]), 50)

    def test_model_cannot_delete_without_delete_word(self):
        out = self.req("광고 찾아줘", ADMIN)
        self.assertEqual(out["delete_ids"], [])
        self.assertEqual(C._pending, {})


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
        self.hosts = set(common.LOCAL_HOSTS)
        common.LOCAL_HOSTS.add("testclient")
        self.key = apikeys.create("owner", "디스코드 봇", kind="bot")
        self.client = TestClient(api_server.app)

    def tearDown(self):
        self.common.LOCAL_HOSTS.clear()
        self.common.LOCAL_HOSTS.update(self.hosts)
        super().tearDown()

    def post(self, path, body):
        return self.client.post(f"/internal/channels/{path}", json=body, headers={"Authorization": f"Bearer {self.key}"})

    def test_flow(self):
        self.assertEqual(self.post("list", {}).json()["items"], [{"id": "100", "latest": None}])
        at = "2026-11-04T01:00:00+00:00"
        rows = [{"id": "1", "channel_id": "100", "author_id": FRIEND, "author_name": "민수", "text": "튤립 사진", "link": "https://x/1",
                 "at": at, "files": [{"filename": "t.png", "kind": "image", "size": 3}]}]
        self.assertTrue(self.post("store", {"rows": rows, "live": True}).json()["needs_reply"])
        self.assertTrue(self.post("list", {}).json()["items"][0]["latest"].startswith("2026-11-04T10:00"))
        self.answer = {"동작": "찾기", "찾는말": "튤립", "사람": None}
        body = {"channel_id": "100", "message_id": "2", "author_id": FRIEND, "text": "튤립 찾아줘"}
        self.assertIn("t.png", self.post("request", body).json()["reply"])
        self.assertTrue(self.post("edit", {"id": "1", "text": "고침"}).json()["ok"])
        self.assertTrue(self.post("forget", {"ids": ["1"]}).json()["ok"])
        self.assertEqual(C._counts("100"), (0, 0))

    def test_message_passes_targets(self):
        body = {"source": "discord", "external_id": ADMIN, "text": "!채널관리 새방 등록",
                "targets": [{"id": "300", "name": "새방", "guild": "g"}, {"bad": 1}]}
        r = self.client.post("/internal/message", json=body, headers={"Authorization": f"Bearer {self.key}"})
        self.assertIn("지정했어요", r.json()["reply"])

    def test_bad_input(self):
        self.assertEqual(self.post("store", {"rows": "x"}).status_code, 400)
        self.assertEqual(self.post("store", {"rows": [{"id": "1", "channel_id": "100", "at": "어제"}]}).status_code, 400)
        self.assertEqual(self.post("request", {"channel_id": "100"}).status_code, 400)
        self.assertEqual(self.post("forget", {"ids": [1]}).status_code, 400)

    def test_needs_bot_key(self):
        for path in ("list", "store", "request", "edit", "forget"):
            self.assertIn(self.client.post(f"/internal/channels/{path}", json={}).status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
