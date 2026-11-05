"""비서 봇 `!파일`: 컴퓨터 고르기, 전달, 답 대기, 인증 코드 (가짜 연결, 네트워크 없음)."""
import asyncio, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from core import config, users
from entrances.api import file_remote as R
from features import app_computers as C


class FakeLink:
    """앱 대신 답한다. replies 를 차례로 쓰고, 없으면 답하지 않는다."""

    def __init__(self, computer_id, replies=()):
        self.computer_id, self.replies, self.sent = computer_id, list(replies), []

    async def send_json(self, message):
        self.sent.append(message)
        if self.replies:
            reply = self.replies.pop(0)
            R.on_reply(self.computer_id, {"id": message["id"], **reply})


class Remote(unittest.TestCase):
    def setUp(self):
        self.real = (config.DB_PATH, R.TURN_SECONDS)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "r.db")
        users.bootstrap([])
        users.add("owner", "family1", "가족")
        C._links.clear()
        R._waiting.clear()
        self.sony = C.register("owner", "소니")
        self.mac = C.register("owner", "맥북")

    def tearDown(self):
        config.DB_PATH, R.TURN_SECONDS = self.real
        C._links.clear()
        R._waiting.clear()

    def link(self, computer, *replies):
        link = FakeLink(computer["id"], replies)
        C._links[computer["id"]] = link
        return link

    def say(self, text, user="owner", dm=True, channel=None):
        return asyncio.run(R.handle(user, text, dm, channel))

    def test_prompt_metadata_and_stale_button_are_bound_to_owner_place_and_question(self):
        import time
        prompt = {"id": "question1", "kind": "confirm", "expiresAt": (time.time() + 50) * 1000, "destructive": False}
        link = self.link(self.sony, {"text": "복사할까요?", "waiting": True, "prompt": prompt}, {"text": "완료"})
        out = self.say("파일 복사 메모")
        self.assertEqual(out["file_prompt"], prompt)
        stale = asyncio.run(R.handle("owner", "예", True, prompt_id="old"))
        self.assertIn("끝났거나", stale["reply"])
        self.assertEqual(stale["file_prompt"], prompt)
        self.assertTrue(stale["file_waiting"])
        self.assertIn("끝났거나", asyncio.run(R.handle("family1", "예", True, prompt_id="question1"))["reply"])
        self.assertIn("끝났거나", asyncio.run(R.handle("owner", "예", False, "101", prompt_id="question1"))["reply"])
        self.assertEqual(len(link.sent), 1)
        self.assertEqual(asyncio.run(R.handle("owner", "예", True, prompt_id="question1"))["reply"], "완료")
        self.assertEqual(link.sent[-1]["promptId"], "question1")
        self.assertIn("끝났거나", asyncio.run(R.handle("owner", "예", True, prompt_id="question1"))["reply"])

    def test_prompt_expiry_and_invalid_shape(self):
        import time
        self.assertIsNone(R.clean_prompt({"id": "q", "kind": "confirm", "expiresAt": 0}))
        self.assertIsNone(R.clean_prompt({"id": "q", "kind": "choice", "expiresAt": (time.time() + 30) * 1000, "options": [True]}))
        prompt = {"id": "q", "kind": "confirm", "expiresAt": (time.time() + 30) * 1000}
        link = self.link(self.sony, {"text": "확인", "waiting": True, "prompt": prompt})
        self.say("파일 복사 메모")
        R._waiting["owner"]["until"] = 0
        self.assertIn("끝났거나", asyncio.run(R.handle("owner", "예", True, prompt_id="q"))["reply"])
        self.assertEqual(len(link.sent), 1)

    def test_other_text_is_not_mine(self):
        for text in ("안녕", "!메모 파일", "!파일함"):
            self.assertIsNone(self.say(text), text)

    def test_auth_help_and_revoke_go_to_default_app_without_model(self):
        link = self.link(self.sony, *({"text": "인증 안내"} for _ in range(4)))
        for text in ("인증", "인증 도움말", "인증 폐기", "!인증 폐기"):
            self.assertEqual(self.say(text)["reply"], "인증 안내")
            self.assertEqual(link.sent[-1]["text"], text.lstrip("!"))
        C.designate("owner", "101", "시험")
        before = len(link.sent)
        self.assertTrue(self.say("인증 폐기", dm=False, channel="101")["delete_source"])
        self.assertEqual(len(link.sent), before)

    def test_auth_revoke_is_not_consumed_as_confirmation_answer(self):
        link = self.link(self.sony, {"text": "예/아니오", "waiting": True}, {"text": "잠김"})
        self.say("!파일 이름 메모.txt 다른.txt")
        self.assertEqual(self.say("인증 폐기")["reply"], "잠김")
        self.assertEqual(link.sent[-1]["type"], "command")
        self.assertEqual(link.sent[-1]["text"], "인증 폐기")

    def test_file_prefixes_have_same_route(self):
        link = self.link(self.sony, *({"text": "됨"} for _ in range(6)))
        for rest in ("이동 a.txt 문서", "테스트에 메모ㄴㄹㄴ 만들어줘", "소니 테스트에 메모 추가해"):
            for prefix in ("파일", "!파일"):
                self.assertEqual(self.say(f"{prefix} {rest}")["reply"], "됨")
            self.assertEqual(link.sent[-2]["text"], link.sent[-1]["text"])
        self.assertEqual(link.sent[2]["text"], "!파일 테스트에 메모ㄴㄹㄴ 만들어줘")
        self.assertEqual(self.say("파일")["reply"], self.say("!파일")["reply"])

    def test_plain_file_channel_rules_and_codes(self):
        link = self.link(self.sony)
        self.assertIn("지정되지", self.say("파일 테스트에 메모 만들어줘", dm=False, channel="101")["reply"])
        self.assertIn("지정했어요", self.say("디스코드 채널 지정 101")["reply"])
        for text in ("파일 인증 123456", "파일 소니 인증 123456", "파일 복구 ABC"):
            self.assertTrue(self.say(text, dm=False, channel="101")["delete_source"])
        self.assertEqual(link.sent, [])
        self.assertIsNone(self.say("파일 찾기 a", "family1", dm=False, channel="101")["reply"])

    def test_plain_file_does_not_become_answer_to_previous_question(self):
        link = self.link(self.sony, {"text": "확인?", "waiting": True}, {"text": "검색 결과"})
        self.say("!파일 이름 a b")
        self.say("파일 찾기 a")
        self.assertEqual(link.sent[-1]["type"], "command")
        self.assertEqual(link.sent[-1]["text"], "!파일 찾기 a")

    def test_default_computer(self):
        link = self.link(self.sony, {"text": "옮겼어요"})
        self.assertEqual(self.say("!파일 이동 a.txt 문서")["reply"], "옮겼어요")
        self.assertEqual(link.sent[0]["text"], "!파일 이동 a.txt 문서")
        self.assertEqual(link.sent[0]["via"], "비서 봇")

    def test_named_computer_keeps_quotes(self):
        other, link = self.link(self.sony), self.link(self.mac, {"text": "됨"})
        self.say('!파일 맥북 복사 "회의 자료.pdf" 문서')
        self.assertEqual(link.sent[0]["text"], '!파일 복사 "회의 자료.pdf" 문서')
        self.assertEqual(other.sent, [])

    def test_offline(self):
        self.assertIn("꺼져", self.say("!파일 이동 a b")["reply"])

    def test_no_computer(self):
        self.assertIn("등록", self.say("!파일 이동 a b", "family1")["reply"])
        self.assertIsNone(self.say("인증 123456", "family1"))

    def test_only_own_computer(self):
        link = self.link(self.mac)
        C.register("family1", "내폰")
        self.assertIn("내폰 앱이", self.say("!파일 맥북 이동 a b", "family1")["reply"])
        self.assertEqual(link.sent, [])

    def test_bare_shows_help_and_state(self):
        self.link(self.sony)
        out = self.say("!파일")["reply"]
        self.assertIn("파일 명령", out)
        self.assertIn("- 소니: 파일 연결됨", out)
        self.assertIn("- 맥북: 파일 앱 꺼짐", out)

    def test_control_words_go_as_they_are(self):
        link = self.link(self.sony, {"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"})
        for text, sent in (("!파일 잠금", "잠금"), ("!파일 소니 상태", "상태"), ("!파일 원격 중지", "원격 중지"), ("!파일 소니 복구 ABC", "복구 ABC")):
            self.say(text)
            self.assertEqual(link.sent[-1]["text"], sent)

    def test_code_goes_to_default_in_dm(self):
        link = self.link(self.sony, {"text": "인증했습니다"})
        self.assertEqual(self.say("인증 123456")["reply"], "인증했습니다")
        self.assertEqual(link.sent[0]["text"], "인증 123456")

    def test_code_refused_and_deleted_in_designated_channel(self):
        link = self.link(self.sony)
        C.designate("owner", "c1", "일반")
        for text in ("인증 123456", "!파일 인증 123456", "!파일 소니 인증 123456", "!파일 복구 ABC"):
            out = self.say(text, dm=False, channel="c1")
            self.assertTrue(out["delete_source"], text)
        self.assertEqual(link.sent, [])

    def test_code_in_other_channel_is_ignored_and_not_forwarded(self):
        link = self.link(self.sony)
        out = self.say("인증 123456", dm=False, channel="c9")
        self.assertIsNone(out["reply"])
        self.assertNotIn("delete_source", out)
        self.assertEqual(link.sent, [])

    def test_channel_commands(self):
        for prefix in ("!디스코드", "디스코드"):
            for verb in ("지정", "등록"):
                self.assertIn("지정했어요", self.say(f"{prefix} 채널 {verb} 101")["reply"])
                self.assertIn("이미", self.say(f"{prefix} 채널 {verb} 101")["reply"])
                self.assertTrue(C.is_designated("owner", "101"))
                self.assertIn("101", self.say(f"{prefix} 채널 목록")["reply"])
                for remove in ("해체", "삭제"):
                    self.say(f"{prefix} 채널 등록 101")
                    self.assertIn("해제", self.say(f"{prefix} 채널 {remove} 101")["reply"])
                    self.assertFalse(C.is_designated("owner", "101"))
                self.assertIn("아니에요", self.say(f"{prefix} 채널 삭제 101")["reply"])
        for text in ("!디스코드", "디스코드", "!디스코드 채널", "디스코드 채널 도움말"):
            help_text = self.say(text)["reply"]
            self.assertIn("사용법", help_text)
            for word in ("지정", "등록", "목록", "해체", "삭제", "채널 ID"):
                self.assertIn(word, help_text)

    def test_channel_name_and_id_are_listed_for_existing_records(self):
        C.designate("owner", "101", "일반")
        self.assertEqual(C.channels_of("owner"), ["일반"])
        self.assertIn("일반 (101)", self.say("!디스코드 채널 목록")["reply"])

    def test_channel_can_use_current_id_or_explicit_id(self):
        for prefix in ("!디스코드", "디스코드"):
            for register in ("지정", "등록"):
                self.assertIn("지정했어요", self.say(f"{prefix} 채널 {register}", dm=False, channel="101")["reply"])
                self.assertTrue(C.is_designated("owner", "101"))
                self.assertIn("101", self.say(f"{prefix} 채널 목록", dm=False, channel="101")["reply"])
                self.assertIn("해제", self.say(f"{prefix} 채널 삭제", dm=False, channel="101")["reply"])
        self.assertIn("지정했어요", self.say("!디스코드 채널 지정 202", dm=False, channel="101")["reply"])
        self.assertFalse(C.is_designated("owner", "101"))
        self.assertTrue(C.is_designated("owner", "202"))
        self.assertIn("해제", self.say("!디스코드 채널 해체 202", dm=False, channel="101")["reply"])
        for action in ("지정", "등록", "해체", "삭제"):
            self.assertIn("DM에서는 채널 ID", self.say("!디스코드 채널 " + action)["reply"])

    def test_old_file_channel_only_explains_new_namespace(self):
        for text in ("!파일 채널 지정 101", "파일 채널 등록 101", "!파일 채널 목록", "!파일 채널 삭제 101"):
            self.assertIn("옮겼습니다", self.say(text)["reply"])
        self.assertEqual(C.channel_entries("owner"), [])

    def test_current_channel_name_is_saved_and_other_id_keeps_its_id(self):
        out = asyncio.run(R.handle("owner", "디스코드 채널 지정", False, "101", "자료실"))
        self.assertIn("지정했어요", out["reply"])
        self.assertEqual(C.channel_entries("owner"), [{"id": "101", "name": "자료실"}])
        asyncio.run(R.handle("owner", "!디스코드 채널 등록 202", False, "101", "자료실"))
        self.assertEqual({row["id"]: row["name"] for row in C.channel_entries("owner")}["202"], "202")

    def test_channel_request_has_auth_flag_but_dm_does_not(self):
        link = self.link(self.sony, {"text": "먼저 인증"}, {"text": "DM 검색"})
        self.say("!디스코드 채널 지정 101")
        self.say("!파일 찾기 a", dm=False, channel="101")
        self.assertIs(link.sent[-1]["channel"], True)
        self.say("!파일 찾기 a")
        self.assertNotIn("channel", link.sent[-1])

    def test_channel_ids_and_argument_counts_are_validated(self):
        for verb in ("지정", "등록", "해체", "삭제"):
            for tail in ("", "0", "-1", "abc", "１２３", "18446744073709551616", "1 extra", "<#101>"):
                out = self.say(f"!디스코드 채널 {verb} {tail}")
                self.assertIn("사용법", out["reply"])
        self.assertEqual(C.channel_entries("owner"), [])
        self.assertIn("사용법", self.say("!디스코드 채널 목록 extra")["reply"])

    def test_channel_management_is_per_user_and_works_without_online_app(self):
        self.say("!디스코드 채널 지정 101")
        self.assertIn("없음", self.say("!디스코드 채널 목록", user="family1")["reply"])
        self.assertIn("아니에요", self.say("!디스코드 채널 삭제 101", user="family1")["reply"])
        self.assertTrue(C.is_designated("owner", "101"))
        self.assertIn("지정했어요", self.say("!디스코드 채널 등록 202", user="family1")["reply"])
        self.assertTrue(C.is_designated("family1", "202"))

    def test_dm_id_enables_only_that_channel_and_removal_disables_it(self):
        link = self.link(self.sony, {"text": "검색 결과"})
        self.say("!디스코드 채널 지정 101")
        self.assertEqual(self.say("!파일 찾기 a", dm=False, channel="101")["reply"], "검색 결과")
        self.assertIn("지정되지", self.say("!파일 찾기 a", dm=False, channel="202")["reply"])
        self.say("!디스코드 채널 삭제 101")
        self.assertIn("지정되지", self.say("!파일 찾기 a", dm=False, channel="101")["reply"])
        self.assertEqual(len(link.sent), 1)

    def test_removal_clears_pending_answer_from_that_channel(self):
        link = self.link(self.sony, {"text": "확인?", "waiting": True})
        self.say("!디스코드 채널 등록 101")
        self.say("!파일 이동 a b", dm=False, channel="101")
        self.assertIn("owner", R._waiting)
        self.say("!디스코드 채널 삭제 101")
        self.assertNotIn("owner", R._waiting)
        self.assertIsNone(self.say("예", dm=False, channel="101"))
        self.assertEqual(len(link.sent), 1)

    def test_channel_management_does_not_consume_pending_dm_answer(self):
        link = self.link(self.sony, {"text": "확인?", "waiting": True}, {"text": "취소됨"})
        self.say("!파일 이동 a b")
        self.assertTrue(self.say("!디스코드 채널 지정 101")["file_waiting"])
        self.assertTrue(self.say("!디스코드 채널 삭제 101")["file_waiting"])
        self.assertIn("owner", R._waiting)
        self.assertEqual(len(link.sent), 1)
        self.assertEqual(self.say("아니오")["reply"], "취소됨")

    def test_managed_channel_refused(self):
        from clients import db
        with db.connect() as c:
            c.execute("INSERT INTO managed_channels(channel_id, guild, name, created) VALUES (?,?,?,?)", ("101", "g", "n", users._now()))
        self.assertIn("채널관리", self.say("!디스코드 채널 지정 101")["reply"])
        self.assertFalse(C.is_designated("owner", "101"))

    def test_undesignated_channel_hints_owner_only(self):
        link = self.link(self.sony)
        self.assertIn("지정되지 않았어요", self.say("!파일 이동 a b", dm=False, channel="c1")["reply"])
        self.assertEqual(link.sent, [])

    def test_stranger_in_channel_gets_silence(self):
        C.designate("owner", "c1", "일반")
        out = self.say("!파일 이동 a b", "family1", dm=False, channel="c1")
        self.assertIsNone(out["reply"])

    def test_designated_is_per_person(self):
        self.link(self.sony)
        C.register("family1", "내폰")
        C.designate("owner", "c1", "일반")
        self.assertIn("지정되지 않았어요", self.say("!파일 이동 a b", "family1", dm=False, channel="c1")["reply"])

    def test_channel_runs_and_waits_for_same_person_in_same_place(self):
        link = self.link(self.sony, {"text": "?", "waiting": True}, {"text": "됨"})
        C.designate("owner", "c1", "일반")
        self.assertTrue(self.say("!파일 이동 a b", dm=False, channel="c1")["file_waiting"])
        self.assertIsNone(self.say("예", dm=False, channel="c2"))  # 다른 곳의 말은 답이 아니다
        self.assertEqual(len(link.sent), 1)

    def test_channel_answer(self):
        link = self.link(self.sony, {"text": "?", "waiting": True}, {"text": "됨"})
        C.designate("owner", "c1", "일반")
        self.say("!파일 이동 a b", dm=False, channel="c1")
        self.assertEqual(self.say("예", dm=False, channel="c1")["reply"], "됨")
        self.assertEqual(link.sent[1]["text"], "예")

    def test_code_as_answer_in_channel_is_deleted(self):
        link = self.link(self.sony, {"text": "?", "waiting": True})
        C.designate("owner", "c1", "일반")
        self.say("!파일 이동 a b", dm=False, channel="c1")
        self.assertTrue(self.say("123456", dm=False, channel="c1")["delete_source"])
        self.assertEqual(len(link.sent), 1)

    def test_waiting_passes_next_text_as_answer(self):
        link = self.link(self.sony, {"text": "이동할까요?", "waiting": True}, {"text": "옮겼어요"})
        out = self.say("!파일 이동 a b")
        self.assertTrue(out["file_waiting"])
        self.assertEqual(self.say("예")["reply"], "옮겼어요")
        self.assertEqual((link.sent[1]["type"], link.sent[1]["text"]), ("answer", "예"))
        self.assertNotIn("owner", R._waiting)

    def test_waiting_is_for_named_computer(self):
        self.link(self.sony)
        link = self.link(self.mac, {"text": "?", "waiting": True}, {"text": "됨"})
        self.say("!파일 맥북 이동 a b")
        self.say("1")
        self.assertEqual(link.sent[1]["text"], "1")

    def test_digit_answer_with_bang(self):
        link = self.link(self.sony, {"text": "?", "waiting": True}, {"text": "됨"})
        self.say("!파일 이동 a b")
        self.say("!2")
        self.assertEqual(link.sent[1]["text"], "2")

    def test_other_command_cancels_waiting(self):
        link = self.link(self.sony, {"text": "?", "waiting": True}, {"text": "취소했습니다"})
        self.say("!파일 이동 a b")
        self.assertIsNone(self.say("!메모목록"))
        self.assertEqual(link.sent[1]["text"], "취소")
        self.assertNotIn("owner", R._waiting)

    def test_expired_waiting_is_not_an_answer(self):
        link = self.link(self.sony, {"text": "?", "waiting": True})
        self.say("!파일 이동 a b")
        R._waiting["owner"]["until"] = 0
        self.assertIsNone(self.say("예"))
        self.assertEqual(len(link.sent), 1)

    def test_no_reply_says_so(self):
        R.TURN_SECONDS = 0.01
        self.link(self.sony)
        self.assertIn("답하지 않아요", self.say("!파일 이동 a b")["reply"])
        self.assertEqual(R._turns, {})

    def test_reply_from_other_computer_ignored(self):
        R.TURN_SECONDS = 0.05
        link = self.link(self.sony)

        async def run():
            task = asyncio.ensure_future(R.handle("owner", "!파일 이동 a b", True))
            while not link.sent:
                await asyncio.sleep(0.001)
            R.on_reply(self.mac["id"], {"id": link.sent[0]["id"], "text": "가짜"})
            return await task
        self.assertIn("답하지 않아요", asyncio.run(run())["reply"])

    def test_disconnect_ends_turn_and_waiting(self):
        link = self.link(self.sony, {"text": "?", "waiting": True})
        self.say("!파일 이동 a b")
        R.on_disconnect(self.sony["id"])
        self.assertNotIn("owner", R._waiting)
        self.assertEqual(len(link.sent), 1)


class Files(unittest.TestCase):
    URL = "https://cdn.discordapp.com/attachments/1/2/a.txt?ex=1"

    def setUp(self):
        self.real = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "f.db")
        users.bootstrap([])
        users.add("owner", "family1", "가족")
        C._links.clear()
        self.sony = C.register("owner", "소니")
        self.mac = C.register("owner", "맥북")

    def tearDown(self):
        config.DB_PATH = self.real
        C._links.clear()

    def link(self, computer, *replies):
        link = FakeLink(computer["id"], replies)
        C._links[computer["id"]] = link
        return link

    def att(self, url=None):
        return [{"id": "9", "name": "a.txt", "size": 3, "url": url or self.URL}]

    def recv(self, text="", files=None, user="owner", dm=True):
        return asyncio.run(R.handle(user, text, dm, None if dm else "c1", "", self.att() if files is None else files))

    def test_clean_files_keeps_discord_https_only(self):
        for url in ("http://cdn.discordapp.com/a", "https://evil.test/a", "https://cdn.discordapp.com.evil.test/a", "file:///etc/passwd"):
            self.assertEqual(R.clean_files(self.att(url)), [], url)
        self.assertEqual(len(R.clean_files(self.att())), 1)
        self.assertEqual(R.clean_files("x"), [])

    def test_receive_goes_to_default(self):
        link = self.link(self.sony, {"text": "파일을 받았습니다"})
        self.assertIn("받았습니다", self.recv()["reply"])
        self.assertEqual(link.sent[0]["files"][0]["name"], "a.txt")

    def test_receive_to_named_computer(self):
        link = self.link(self.mac, {"text": "받음"})
        self.recv("!파일 맥북")
        self.assertEqual(len(link.sent[0]["files"]), 1)

    def test_receive_when_app_is_off(self):
        self.assertIn("꺼져", self.recv()["reply"])

    def test_receive_silent_without_computer_or_bad_url(self):
        self.assertIsNone(self.recv(user="family1")["reply"])
        self.link(self.sony)
        self.assertIsNone(self.recv(files=self.att("https://evil.test/a"))["reply"])

    def test_receive_only_in_dm(self):
        link = self.link(self.sony)
        self.assertIsNone(self.recv("안녕", dm=False))
        self.assertEqual(link.sent, [])

    def test_command_with_attachment_stays_a_command(self):
        link = self.link(self.sony, {"text": "됨"})
        self.recv("!파일 이동 a 문서")
        self.assertEqual(link.sent[0]["text"], "!파일 이동 a 문서")
        self.assertNotIn("files", link.sent[0])

    def test_attach_adds_file_to_reply_and_forgets(self):
        link = self.link(self.sony)

        async def run():
            task = asyncio.ensure_future(R.handle("owner", "!파일 보내기 a", True))
            while not link.sent:
                await asyncio.sleep(0.001)
            turn = link.sent[0]["id"]
            self.assertFalse(R.attach(self.mac["id"], turn, "a", b"x", "", 7))  # 다른 컴퓨터
            self.assertTrue(R.attach(self.sony["id"], turn, "a.txt", b"abc", "note", 3))
            R.on_reply(self.sony["id"], {"id": turn, "text": "DM 으로 보냅니다"})
            return await task
        out = asyncio.run(run())
        self.assertEqual(out["file"], {"name": "a.txt", "note": "note", "days": 3, "data": "YWJj"})
        self.assertEqual(R._files, {})

    def test_attach_needs_live_turn(self):
        self.assertFalse(R.attach(self.sony["id"], "t_none", "a", b"x", "", 7))

    def test_sent_due_and_forget(self):
        C.record_sent("owner", "c", "m1", "a.txt", 3)
        self.assertEqual(C.due_sent(), [])
        real = users._now
        users._now = lambda: real() + __import__("datetime").timedelta(days=4)
        self.addCleanup(setattr, users, "_now", real)
        self.assertEqual(C.due_sent(), [{"message_id": "m1", "channel_id": "c"}])
        C.forget_sent(["m1"])
        self.assertEqual(C.due_sent(), [])

    def test_run_sent_files(self):
        from entrances import reminders

        class Gone(Exception):
            pass
        acked = []

        async def fetch():
            return [{"message_id": "1"}, {"message_id": "2"}, {"message_id": "3"}]

        async def delete(item):
            if item["message_id"] == "2":
                raise Gone()
            if item["message_id"] == "3":
                raise OSError("잠시 실패")

        async def ack(ids):
            acked.extend(ids)
        asyncio.run(reminders.run_sent_files(fetch, delete, ack, Gone))
        self.assertEqual(acked, ["1", "2"])


class Rules(unittest.TestCase):
    def test_file_text(self):
        from entrances import discord_rules as D
        self.assertEqual(D.file_text(" 인증 123 456 ", False), "인증 123 456")
        self.assertIsNone(D.file_text("안녕", False))
        self.assertEqual(D.file_text("예", True), "예")
        self.assertIsNone(D.file_text("  ", True))

    def test_waits_expire_and_are_per_person(self):
        from entrances import discord_rules as D
        w = D.FileWaits(60)
        w.set(1, 2, 100)
        self.assertTrue(w.active(1, 2, 159))
        self.assertFalse(w.active(1, 2, 161))
        self.assertFalse(w.active(1, 3, 120))
        w.clear(1, 2)
        self.assertFalse(w.active(1, 2, 120))


if __name__ == "__main__":
    unittest.main()
