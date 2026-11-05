"""앱 파일 동작과 앱 규약 v1: 파일 문장 거르기, 모델 응답 검증, client_action 만들기, 되묻기,
결과 보고, 서비스 제어 confirm, /v1 입구 (모델·네트워크 없음)"""
import json, os, subprocess, sys, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from core import apikeys, classifier, config, users
from clients import llm
from core import assistant as A
from features import files as F


def raw(**d):
    base = {"동작": "아님", "대상": None, "목적지": None, "새이름": None, "위치": None}
    base.update(d)
    return json.dumps(base, ensure_ascii=False)


class Gate(unittest.TestCase):
    def test_file_sentences(self):
        for t in ("보고서.pdf 옮겨줘", "견적서 파일 찾아줘", "문서에 2026 폴더 만들어줘", "가계부 백업해줘",
                  "다운로드 정리", "초안 이름 바꿔줘", "config.json 열어", "파일 test에서 목록 보여줘",
                  "my music 폴더 안의 파일들 보여줘", "문서 폴더에 뭐가 있어?"):
            self.assertTrue(F.looks_like_file(t), t)

    def test_not_file_sentences(self):
        for t in ("3번 메모 지워줘", "부산 날씨", "내일 3시 치과 일정 잡아줘", "안녕", "3.5도야?", "1.2.3 버전",
                  "파이썬 3.11 설치법", "www.naver.com 열어줘", "https://example.com/a.pdf 봐줘", "google.co.kr 접속",
                  "메모 목록 보여줘", "오늘 일정 목록 보여줘"):
            self.assertFalse(F.looks_like_file(t), t)


class Parse(unittest.TestCase):
    T = "다운로드에 있는 보고서.pdf를 문서 폴더로 옮겨줘"

    def test_ok_and_cleaned(self):
        r = F.parse(raw(동작="이동", 대상="보고서.pdf를", 목적지="문서 폴더로", 위치="다운로드에"), self.T)
        self.assertEqual(r, {"동작": "file.move", "대상": "보고서.pdf", "목적지": "문서", "새이름": None, "위치": "다운로드"})

    def test_invented_name_rejected(self):
        self.assertIsNone(F.parse(raw(동작="이동", 대상="계약서.pdf", 목적지="문서"), self.T))

    def test_bad_format(self):
        for r in ("아님", "[]", raw(동작="날리기"), raw(동작="이동", 대상=3)):
            self.assertIsNone(F.parse(r, self.T), r)

    def test_not_file_and_refuse(self):
        self.assertEqual(F.parse(raw(), "파일 압축하는 법")["동작"], "아님")
        self.assertEqual(F.parse(raw(동작="수정", 대상="메모.txt"), "메모.txt 고쳐줘")["동작"], "수정")

    def test_where_is_means_reveal(self):
        t = "이력서.docx 어디 있어? 폴더 열어줘"
        self.assertEqual(F.parse(raw(동작="열기", 대상="이력서.docx"), t)["동작"], "file.reveal")

    def test_search_drops_trailing_file_word(self):
        self.assertEqual(F.parse(raw(동작="찾기", 대상="작년 세금 파일"), "작년 세금 파일 찾아줘")["대상"], "작년 세금")

    def test_list_preserves_folder_path_and_rejects_invented_location(self):
        for folder in ("test", "테스트", "test/my music"):
            with self.subTest(folder=folder):
                text = f"파일 {folder}에서 목록 보여줘"
                r = F.parse(raw(동작="목록", 목적지=folder), text)
                self.assertEqual(r["동작"], "file.list")
                self.assertEqual(r["목적지"], folder)
                self.assertIsNone(F.parse(raw(동작="목록", 목적지="없는폴더"), text))

    def test_open_preserves_subdirectory_and_extensionless_target(self):
        for folder in ("my music", "마이 뮤직", "test/my music"):
            text = f"파일 {folder}에서 메모 파일 열어"
            parsed = F.parse(raw(동작="열기", 대상="메모 파일", 위치=folder), text)
            action, _ = F.build(parsed)
            self.assertEqual(action["args"], {"target": "메모", "root": folder})
            self.assertIsNone(F.parse(raw(동작="열기", 대상="메모", 위치="없는폴더"), text))

    def test_open_cannot_drop_spoken_search_location(self):
        for text in ("파일 my music에서 메모 열어", "파일 my music 에서 메모 열어", "파일 이 폴더에서 메모 열어", "my music 폴더 안의 메모 파일 열어"):
            parsed = F.parse(raw(동작="열기", 대상="메모"), text)
            action, say = F.build(parsed)
            self.assertIsNone(action, text)
            self.assertIn("어느 폴더", say)


class Build(unittest.TestCase):
    def b(self, **d):
        base = {"동작": "file.open", "대상": None, "목적지": None, "새이름": None, "위치": None}
        base.update(d)
        return F.build(base)

    def test_move_with_root_and_confirm_note(self):
        a, say = self.b(동작="file.move", 대상="보고서.pdf", 목적지="바탕화면", 위치="다운로드")
        self.assertEqual((a["type"], a["action"], a["args"]),
                         ("client_action", "file.move", {"target": "보고서.pdf", "dest": "desktop", "root": "downloads"}))
        self.assertTrue(a["id"].startswith("a_"))
        self.assertIn("확인 창", say)
        self.assertEqual(a["say"], say)

    def test_reveal_and_rename_keep_optional_subdirectory(self):
        for action in ("file.reveal", "file.rename"):
            for where in (None, "my music", "test/my music"):
                out, say = self.b(동작=action, 대상="메모", 새이름="새 메모.txt", 위치=where)
                expected = {"target": "메모"}
                if action == "file.rename":
                    expected["name"] = "새 메모.txt"
                    self.assertIn("확인 창", say)
                if where:
                    expected["root"] = where
                self.assertEqual(out["args"], expected)
            self.assertIsNone(self.b(동작=action, 대상="메모", 새이름="새메모.txt", 위치="이")[0])

    def test_copy_same_folder_and_named_copy(self):
        for data, expected in (
            ({}, {"target": "코드2", "sameFolder": True}),
            ({"같은폴더": True, "새이름": "새 코드"}, {"target": "코드2", "sameFolder": True, "name": "새 코드"}),
            ({"위치": "test", "목적지": "my music"}, {"target": "코드2", "root": "test", "dest": "my music"}),
        ):
            action, say = self.b(동작="file.copy", 대상="코드2", **data)
            self.assertEqual(action["args"], expected)
            self.assertIn("확인 창", say)
        self.assertIsNone(self.b(동작="file.copy", 대상="코드2", 목적지="my music", 같은폴더=True)[0])
        self.assertIsNone(self.b(동작="file.move", 대상="코드2", 같은폴더=True)[0])

    def test_each_action_args(self):
        cases = [
            (dict(동작="file.search", 대상="견적서", 위치="문서"), "file.search", {"query": "견적서", "root": "documents"}),
            (dict(동작="file.open", 대상="사진.jpg"), "file.open", {"target": "사진.jpg"}),
            (dict(동작="file.reveal", 대상="a.pdf"), "file.reveal", {"target": "a.pdf"}),
            (dict(동작="file.copy", 대상="a.pdf", 목적지="문서"), "file.copy", {"target": "a.pdf", "dest": "documents"}),
            (dict(동작="file.rename", 대상="초안.txt", 새이름="최종.txt"), "file.rename", {"target": "초안.txt", "name": "최종.txt"}),
            (dict(동작="file.mkdir", 목적지="문서", 새이름="2026"), "file.mkdir", {"dest": "documents", "name": "2026"}),
            (dict(동작="file.mkdir", 목적지="프로젝트", 새이름="자료"), "file.mkdir", {"dest": "프로젝트", "name": "자료"}),
            (dict(동작="file.backup", 대상="가계부.xlsx"), "file.backup", {"target": "가계부.xlsx"}),
            (dict(동작="file.delete", 대상="old.zip"), "file.delete", {"target": "old.zip"}),
            (dict(동작="file.send", 대상="결과.pdf"), "file.send", {"target": "결과.pdf"}),
            (dict(동작="file.create", 목적지="문서", 새이름="메모"), "file.create", {"dest": "documents", "name": "메모"}),
            (dict(동작="file.create", 목적지="프로젝트", 새이름="메모.txt"), "file.create", {"dest": "프로젝트", "name": "메모.txt"}),
            (dict(동작="file.create", 대상="메모.TXT", 목적지="문서"), "file.create", {"dest": "documents", "name": "메모.TXT"}),
            (dict(동작="file.list", 목적지="test"), "file.list", {"dest": "test"}),
            (dict(동작="file.list", 목적지="문서"), "file.list", {"dest": "documents"}),
            (dict(동작="file.list", 위치="test"), "file.list", {"dest": "test"}),
            (dict(동작="file.list", 대상="test/my music"), "file.list", {"dest": "test/my music"}),
            (dict(동작="file.list", 목적지="test", 위치="문서"), "file.list", {"dest": "test", "root": "documents"}),
            (dict(동작="file.open", 대상="메모", 위치="my music"), "file.open", {"target": "메모", "root": "my music"}),
            (dict(동작="file.open", 대상="메모", 위치="test/my music"), "file.open", {"target": "메모", "root": "test/my music"}),
        ]
        for d, action, args in cases:
            a, _ = self.b(**d)
            self.assertEqual((a["action"], a["args"]), (action, args), d)
            self.assertIn(a["action"], F.ACTIONS)

    def test_delete_does_not_claim_done(self):
        _, say = self.b(동작="file.delete", 대상="old.zip")
        self.assertIn("삭제할게요", say)
        self.assertNotIn("삭제했", say)

    def test_missing_values_ask_without_action(self):
        for d, word in ((dict(동작="file.move", 대상="a.pdf"), "어느 폴더"), (dict(동작="file.move"), "어느 파일"),
                        (dict(동작="file.rename", 대상="a.txt"), "새 이름"), (dict(동작="file.mkdir", 새이름="2026"), "어디에"),
                        (dict(동작="file.mkdir", 목적지="문서"), "폴더 이름"), (dict(동작="file.search"), "무엇을 찾을지"),
                        (dict(동작="file.create", 목적지="문서"), "파일 이름"), (dict(동작="file.create", 새이름="메모"), "어디에"),
                        (dict(동작="file.create", 목적지="문서", 새이름="메모.md"), ".txt"),
                        (dict(동작="file.open", 대상="이"), "어느 파일"), (dict(동작="file.delete", 대상="그거"), "어느 파일")):
            a, say = self.b(**d)
            self.assertIsNone(a, d)
            self.assertIn(word, say, d)

    def test_prompt_treats_make_words_alike(self):
        for word in ("만들어", "생성해", "등록해", "추가해", "에서 할일 추가해", "할일.txt에 내용 추가해"):
            self.assertIn(word, F.FILE_PROMPT)

    def test_make_sentences_reach_the_file_classifier(self):
        for t in ("파일 test 에서 메모 추가해", "문서에 메모.txt 등록해줘", "바탕화면에 할일 파일 생성해", "test 폴더에 자료 폴더 추가해"):
            self.assertTrue(F.looks_like_file(t), t)

    def test_url_target_gets_no_action(self):
        a, say = self.b(동작="file.open", 대상="www.naver.com")
        self.assertIsNone(a)
        self.assertIn("웹 주소", say)

    def test_refuse_edit(self):
        a, say = self.b(동작="수정", 대상="메모.txt")
        self.assertIsNone(a)
        self.assertIn("하지 않아요", say)

    def test_list_asks_for_missing_or_vague_folder_without_broadening_scope(self):
        for field, value in (("목적지", None), ("목적지", "이"), ("대상", "그거"), ("위치", "저것")):
            with self.subTest(field=field, value=value):
                action, say = self.b(동작="file.list", **{field: value})
                self.assertIsNone(action)
                self.assertIn("어느 폴더", say)
        self.assertIsNone(self.b(동작="file.list", 목적지="test", 위치="그거")[0])

    def test_list_rejects_web_address_and_does_not_claim_a_result(self):
        for field in ("목적지", "대상", "위치"):
            action, say = self.b(동작="file.list", **{field: "https://example.com"})
            self.assertIsNone(action)
            self.assertIn("웹 주소", say)
        action, say = self.b(동작="file.list", 목적지="test")
        self.assertEqual(action["args"], {"dest": "test"})
        self.assertIn("확인할게요", say)
        self.assertNotIn("확인 창", say)

    def test_open_vague_location_asks_but_unspecified_location_does_not_invent_one(self):
        action, say = self.b(동작="file.open", 대상="메모", 위치="이")
        self.assertIsNone(action)
        self.assertIn("어느 폴더", say)
        action, _ = self.b(동작="file.open", 대상="메모")
        self.assertEqual(action["args"], {"target": "메모"})


class Flow(unittest.TestCase):
    def setUp(self):
        self.real = (config.DB_PATH, F.classify, classifier.ask_model, llm.chat)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "f.db")
        self.file_answers, self.asked = [], []

        def fclassify(text, model=None, timeout=120):
            self.asked.append(("파일", text))
            a = self.file_answers.pop(0) if self.file_answers else raw()
            return F.parse(a, text)
        F.classify = fclassify

        def ask(text, model=None, timeout=120):
            self.asked.append(("분류", text))
            return '{"의도": "일반대화"}', 0.0
        classifier.ask_model = ask
        llm.chat = lambda messages, model=None, timeout=300: "대화 답"
        self.bot = A.Assistant()

    def tearDown(self):
        config.DB_PATH, F.classify, classifier.ask_model, llm.chat = self.real

    def app(self, text, *answers):
        self.file_answers.extend(answers)
        return self.bot.handle_ex("owner", text, client_actions=True)

    def test_app_gets_action(self):
        out = self.app("보고서.pdf 문서로 옮겨줘", raw(동작="이동", 대상="보고서.pdf", 목적지="문서"))
        self.assertEqual(out["action"]["action"], "file.move")
        self.assertEqual(out["text"], out["action"]["say"])
        self.assertIsNone(out["confirm"])
        self.assertEqual(self.asked, [("파일", "보고서.pdf 문서로 옮겨줘")])  # 파일이면 평소 분류는 하지 않는다

    def test_folder_list_does_not_fall_through_to_general_chat(self):
        text = "파일 test에서 목록 보여줘"
        out = self.app(text, raw(동작="목록", 목적지="test"))
        self.assertEqual(out["action"]["action"], "file.list")
        self.assertEqual(out["action"]["args"], {"dest": "test"})
        self.assertEqual(self.asked, [("파일", text)])
        self.assertIsNone(out["confirm"])

    def test_folder_list_missing_location_asks_instead_of_chatting(self):
        text = "파일 목록 보여줘"
        out = self.app(text, raw(동작="목록"))
        self.assertIsNone(out["action"])
        self.assertIn("어느 폴더", out["text"])
        self.assertEqual(self.asked, [("파일", text)])

    def test_file_command_usage_list_remains_a_question(self):
        out = self.app("파일 명령어 목록 알려줘", raw())
        self.assertIsNone(out["action"])
        self.assertEqual([kind for kind, _ in self.asked], ["파일", "분류"])

    def test_open_with_and_without_search_location(self):
        for text, folder, expected in (
            ("파일 my music에서 메모 파일 열어", "my music", {"target": "메모", "root": "my music"}),
            ("파일 메모 파일 열어", None, {"target": "메모"}),
        ):
            out = self.app(text, raw(동작="열기", 대상="메모", 위치=folder))
            self.assertEqual(out["action"]["args"], expected)
        self.assertEqual([kind for kind, _ in self.asked], ["파일", "파일"])

    def test_reveal_and_rename_retain_scope_or_ask_when_scope_was_dropped(self):
        for verb, tail, name in (("위치", "위치 보여줘", None), ("이름변경", "이름을 새메모.txt로 바꿔줘", "새메모.txt")):
            text = f"파일 my music에서 메모 {tail}"
            out = self.app(text, raw(동작=verb, 대상="메모", 새이름=name, 위치="my music"))
            self.assertEqual(out["action"]["args"]["root"], "my music")
            out = self.app(text, raw(동작=verb, 대상="메모", 새이름=name))
            self.assertIsNone(out["action"])
            self.assertIn("어느 폴더", out["text"])

    def test_copy_source_destination_and_same_folder_examples(self):
        for text, answer, expected in (
            ("파일 test 폴더에서 코드2를 my music 폴더로 복사해줘", raw(동작="복사", 대상="코드2", 위치="test", 목적지="my music"), {"target": "코드2", "root": "test", "dest": "my music"}),
            ("파일 코드2를 같은 폴더에 하나 더 복사해줘", raw(동작="복사", 대상="코드2", 같은폴더=True), {"target": "코드2", "sameFolder": True}),
            ("파일 test에서 코드2를 같은 폴더에 새코드라는 이름으로 복사해줘", raw(동작="복사", 대상="코드2", 위치="test", 새이름="새코드", 같은폴더=True), {"target": "코드2", "root": "test", "sameFolder": True, "name": "새코드"}),
        ):
            out = self.app(text, answer)
            self.assertEqual(out["action"]["args"], expected)
            self.assertEqual(out["action"]["action"], "file.copy")

    def test_transfer_does_not_drop_spoken_source_or_destination(self):
        for verb in ("복사", "이동"):
            source = self.app("파일 test에서 코드2를 my music 폴더로 복사해줘", raw(동작=verb, 대상="코드2", 목적지="my music"))
            self.assertIsNone(source["action"])
            self.assertIn("어느 폴더", source["text"])
            dest = self.app("파일 코드2를 my music 폴더로 복사해줘", raw(동작=verb, 대상="코드2"))
            self.assertIsNone(dest["action"])
            self.assertIn("도착 폴더", dest["text"])

    def test_same_folder_placeholder_and_flag_are_validated(self):
        text = "파일 코드2를 같은 폴더에 복사해줘"
        out = self.app(text, raw(동작="복사", 대상="코드2", 목적지="같은 폴더"))
        self.assertEqual(out["action"]["args"], {"target": "코드2", "sameFolder": True})
        self.assertIsNone(F.parse(raw(동작="복사", 대상="코드2", 목적지="my music", 같은폴더=True), "파일 코드2를 my music 폴더로 복사해줘"))
        self.assertIsNone(F.parse(raw(동작="복사", 대상="코드2", 같은폴더="true"), text))
        parsed = F.parse(raw(동작="복사", 대상="코드2", 같은폴더=None), "파일 코드2 복사해줘")
        self.assertEqual(F.build(parsed)[0]["args"], {"target": "코드2", "sameFolder": True})

    def test_other_entrances_get_guidance(self):
        self.file_answers.append(raw(동작="이동", 대상="보고서.pdf", 목적지="문서"))
        out = self.bot.handle("owner", "보고서.pdf 문서로 옮겨줘")
        self.assertIn("데스크톱 앱", out)

    def test_not_file_goes_to_normal_classifier(self):
        out = self.app("파일 압축하는 법 알려줘", raw())
        self.assertEqual(out["text"], "대화 답")
        self.assertIsNone(out["action"])
        self.assertEqual([k for k, _ in self.asked], ["파일", "분류"])

    def test_bad_file_format_goes_to_normal_classifier(self):
        out = self.app("보고서.pdf 어때", "형식 오류")
        self.assertEqual(out["text"], "대화 답")

    def test_ask_back_has_no_action(self):
        out = self.app("보고서.pdf 옮겨줘", raw(동작="이동", 대상="보고서.pdf"))
        self.assertIsNone(out["action"])
        self.assertIn("어느 폴더", out["text"])

    def test_commands_still_work_in_app(self):
        out = self.bot.handle_ex("owner", "!메모 보고서.pdf 확인", client_actions=True)
        self.assertIn("저장했습니다", out["text"])
        self.assertEqual(self.asked, [])

    def test_result_goes_to_memory_once(self):
        a = self.app("a.pdf 백업해줘", raw(동작="백업", 대상="a.pdf"))["action"]
        self.assertFalse(self.bot.record_result("family1", a["id"], "done", "x"))  # 다른 사람
        self.assertTrue(self.bot.record_result("owner", a["id"], "done", "문서/backup/a 2026-10-21.pdf"))
        self.assertIn("파일 작업 결과: 완료 - 문서/backup", self.bot.history["owner"][-1]["content"])
        self.assertFalse(self.bot.record_result("owner", a["id"], "done"))  # 한 번만

    def test_result_expires(self):
        a = self.app("a.pdf 열어줘", raw(동작="열기", 대상="a.pdf"))["action"]
        u, act, _ = self.bot.actions[a["id"]]
        self.bot.actions[a["id"]] = (u, act, time.time() - 1)
        self.assertFalse(self.bot.record_result("owner", a["id"], "done"))


class Result:
    def __init__(self, out="", err="", code=0):
        self.stdout, self.stderr, self.returncode = out, err, code


class Confirm(unittest.TestCase):
    def setUp(self):
        self.real = (subprocess.run, subprocess.Popen, config.SYSADMIN_CONTROL)
        self.spawned = []
        subprocess.run = lambda cmd, **kw: Result("ok")
        subprocess.Popen = lambda cmd, **kw: self.spawned.append(cmd)
        config.SYSADMIN_CONTROL = True
        self.bot = A.Assistant()

    def tearDown(self):
        subprocess.run, subprocess.Popen, config.SYSADMIN_CONTROL = self.real

    def ask(self, user="owner"):
        return self.bot.handle_ex(user, "!컴퓨터 절전", client_actions=True)["confirm"]

    def test_sleep_approved_once(self):
        c = self.ask()
        self.assertEqual((c["type"], c["expires_in"]), ("confirm", 30))
        self.assertIn("절전", c["message"])
        self.assertEqual(self.spawned, [])
        ok, text, second = self.bot.confirm_v1("owner", c["id"], True)
        self.assertTrue(ok)
        self.assertIsNone(second)
        self.assertIn("절전합니다", text)
        self.assertEqual(len(self.spawned), 1)
        self.assertFalse(self.bot.confirm_v1("owner", c["id"], True)[0])

    def test_restart_approved_twice(self):
        c = self.bot.handle_ex("owner", "!컴퓨터 재시작", client_actions=True)["confirm"]
        self.assertIn("재시작", c["message"])
        ok, text, second = self.bot.confirm_v1("owner", c["id"], True)
        self.assertTrue(ok)
        self.assertIn("정말", second["message"])
        self.assertNotEqual(second["id"], c["id"])
        self.assertEqual(self.spawned, [])
        self.assertFalse(self.bot.confirm_v1("owner", c["id"], True)[0])  # 첫 창은 한 번만
        ok, text, third = self.bot.confirm_v1("owner", second["id"], True)
        self.assertTrue(ok)
        self.assertIsNone(third)
        self.assertEqual(len(self.spawned), 1)
        self.assertFalse(self.bot.confirm_v1("owner", second["id"], True)[0])
    def test_app_can_type_confirm(self):
        self.ask()
        out = self.bot.handle_ex("owner", "!확인", client_actions=True)
        self.assertIn("절전합니다", out["text"])
        self.assertEqual(len(self.spawned), 1)
    def test_reject(self):
        c = self.ask()
        self.assertEqual(self.bot.confirm_v1("owner", c["id"], False), (True, "취소했습니다.", None))
        self.assertNotIn("owner", self.bot.pending)
        c = self.bot.handle_ex("owner", "!컴퓨터 재시작", client_actions=True)["confirm"]
        _, _, second = self.bot.confirm_v1("owner", c["id"], True)
        self.assertEqual(self.bot.confirm_v1("owner", second["id"], False), (True, "취소했습니다.", None))
        self.assertEqual(self.spawned, [])
    def test_other_user_and_expired(self):
        c = self.ask()
        self.assertFalse(self.bot.confirm_v1("family1", c["id"], True)[0])
        c = self.ask()
        self.bot.pending["owner"] = (1, 0, None)
        ok, text, _ = self.bot.confirm_v1("owner", c["id"], True)
        self.assertFalse(ok)
        self.assertIn("시간이 지나", text)
        self.assertEqual(self.spawned, [])

    def test_no_confirm_event_outside_app(self):
        out = self.bot.handle_ex("owner", "!컴퓨터 절전")
        self.assertIsNone(out["confirm"])
        self.assertIn("`!확인`", out["text"])


try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi 가 없어서 건너뜀")
class V1(unittest.TestCase):
    def setUp(self):
        from entrances import api_server
        from entrances.api import common
        self.common = common
        self.api = api_server
        self.real = (config.DB_PATH, F.classify, classifier.ask_model, llm.chat, subprocess.run, config.SYSADMIN_CONTROL)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "v.db")
        users.bootstrap([])
        users.add("owner", "family1", "가족")
        self.file_answers = []
        F.classify = lambda text, model=None, timeout=120: F.parse(self.file_answers.pop(0) if self.file_answers else raw(), text)
        classifier.ask_model = lambda text, model=None, timeout=120: ('{"의도": "일반대화"}', 0.0)
        llm.chat = lambda messages, model=None, timeout=300: "대화 답"
        subprocess.run = lambda cmd, **kw: Result("ok")
        config.SYSADMIN_CONTROL = True
        self.common.bot.history.clear()
        self.owner = apikeys.create("owner", "맥 앱")
        self.family = apikeys.create("family1", "가족 앱")
        self.botkey = apikeys.create("owner", "디스코드 봇", kind="bot")
        self.client = TestClient(api_server.app)

    def tearDown(self):
        (config.DB_PATH, F.classify, classifier.ask_model, llm.chat, subprocess.run, config.SYSADMIN_CONTROL) = self.real

    def h(self, key):
        return {"Authorization": f"Bearer {key}"}

    def chat(self, text, key=None):
        r = self.client.post("/v1/chat", json={"text": text, "input": "text", "session": "sample"}, headers=self.h(key or self.owner))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("application/x-ndjson", r.headers["content-type"])
        return [json.loads(x) for x in r.text.splitlines() if x.strip()]

    def test_health(self):
        r = self.client.get("/v1/health", headers=self.h(self.owner)).json()
        self.assertEqual((r["api"], r["server"]), ("1", "local-ai-server"))
        self.assertEqual(r["features"]["client_actions"], list(F.ACTIONS))
        self.assertTrue(r["features"]["server_confirm"])
        self.assertIn("file.list", r["features"]["client_actions"])

    def test_auth_errors_use_v1_format(self):
        r = self.client.get("/v1/health")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "unauthorized")
        self.assertEqual(self.client.get("/v1/health", headers=self.h(self.botkey)).status_code, 403)
        self.assertEqual(self.client.get("/api/tags").json(), {"error": "API 키가 없거나 맞지 않습니다."})  # Ollama 형식은 그대로

    def test_plain_chat(self):
        ev = self.chat("안녕")
        self.assertEqual(ev, [{"type": "delta", "text": "대화 답"}, {"type": "done", "text": "대화 답"}])

    def test_file_action_then_result(self):
        self.file_answers.append(raw(동작="이동", 대상="보고서.pdf", 목적지="문서", 위치="다운로드"))
        ev = self.chat("다운로드 보고서.pdf 문서로 옮겨줘")
        self.assertEqual([e["type"] for e in ev], ["delta", "client_action", "done"])
        a = ev[1]
        self.assertEqual(a["args"], {"target": "보고서.pdf", "dest": "documents", "root": "downloads"})
        url = f"/v1/actions/{a['id']}/result"
        self.assertEqual(self.client.post(url, json={"status": "done", "detail": "다운로드/보고서.pdf → 문서/보고서.pdf"},
                                          headers=self.h(self.family)).status_code, 404)  # 다른 사람
        r = self.client.post(url, json={"status": "done", "detail": "다운로드/보고서.pdf → 문서/보고서.pdf"}, headers=self.h(self.owner))
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.client.post(url, json={"status": "done"}, headers=self.h(self.owner)).status_code, 404)
        self.assertEqual(self.client.post(url, json={"status": "ok"}, headers=self.h(self.owner)).status_code, 400)

    def test_folder_list_action_and_missing_location_over_v1(self):
        self.file_answers.append(raw(동작="목록", 목적지="test"))
        events = self.chat("파일 test에서 목록 보여줘")
        self.assertEqual([e["type"] for e in events], ["delta", "client_action", "done"])
        self.assertEqual(events[1]["action"], "file.list")
        self.assertEqual(events[1]["args"], {"dest": "test"})
        self.file_answers.append(raw(동작="목록"))
        missing = self.chat("파일 목록 보여줘")
        self.assertNotIn("client_action", [e["type"] for e in missing])
        self.assertIn("어느 폴더", missing[-1]["text"])

    def test_open_scope_over_v1_and_guard_missing_scope(self):
        self.file_answers.append(raw(동작="열기", 대상="메모", 위치="my music"))
        events = self.chat("파일 my music에서 메모 파일 열어")
        self.assertEqual(events[1]["action"], "file.open")
        self.assertEqual(events[1]["args"], {"target": "메모", "root": "my music"})
        self.file_answers.append(raw(동작="열기", 대상="메모"))
        events = self.chat("파일 my music에서 메모 열어")
        self.assertNotIn("client_action", [e["type"] for e in events])
        self.assertIn("어느 폴더", events[-1]["text"])

    def test_reveal_and_rename_scope_over_v1(self):
        for verb, action, tail, name in (("위치", "file.reveal", "위치 보여줘", None), ("이름변경", "file.rename", "이름을 새메모.txt로 바꿔줘", "새메모.txt")):
            self.file_answers.append(raw(동작=verb, 대상="메모", 새이름=name, 위치="test/my music"))
            events = self.chat(f"파일 test/my music에서 메모 {tail}")
            self.assertEqual(events[1]["action"], action)
            self.assertEqual(events[1]["args"]["root"], "test/my music")
            if name:
                self.assertEqual(events[1]["args"]["name"], name)

    def test_copy_same_folder_and_name_over_v1(self):
        self.file_answers.append(raw(동작="복사", 대상="코드2", 새이름="새코드", 같은폴더=True))
        events = self.chat("파일 코드2를 같은 폴더에 새코드라는 이름으로 복사해줘")
        self.assertEqual(events[1]["action"], "file.copy")
        self.assertEqual(events[1]["args"], {"target": "코드2", "name": "새코드", "sameFolder": True})

    def test_suspend_confirm_once(self):
        spawned = []
        real = subprocess.Popen
        subprocess.Popen = lambda cmd, **kw: spawned.append(cmd)
        self.addCleanup(setattr, subprocess, "Popen", real)
        ev = self.chat("!컴퓨터 절전")
        self.assertEqual([e["type"] for e in ev], ["confirm", "done"])
        self.assertEqual(spawned, [])
        r = self.client.post("/v1/confirm", json={"id": ev[0]["id"], "approve": True}, headers=self.h(self.owner))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("confirm", r.json())
        self.assertEqual(len(spawned), 1)
        r = self.client.post("/v1/confirm", json={"id": ev[0]["id"], "approve": True}, headers=self.h(self.owner))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (410, "expired"))

    def test_restart_confirm_twice(self):
        spawned = []
        real = subprocess.Popen
        subprocess.Popen = lambda cmd, **kw: spawned.append(cmd)
        self.addCleanup(setattr, subprocess, "Popen", real)
        ev = self.chat("!컴퓨터 재시작")
        r = self.client.post("/v1/confirm", json={"id": ev[0]["id"], "approve": True}, headers=self.h(self.owner))
        second = r.json()["confirm"]
        self.assertEqual(second["type"], "confirm")
        self.assertEqual(spawned, [])
        r = self.client.post("/v1/confirm", json={"id": second["id"], "approve": True}, headers=self.h(self.owner))
        self.assertNotIn("confirm", r.json())
        self.assertEqual(len(spawned), 1)
    def test_confirm_needs_admin_and_valid_body(self):
        self.assertEqual(self.client.post("/v1/confirm", json={"id": "c_1", "approve": True}, headers=self.h(self.family)).status_code, 403)
        self.assertEqual(self.client.post("/v1/confirm", json={"id": "c_1", "approve": "yes"}, headers=self.h(self.owner)).status_code, 400)

    def test_chat_needs_text(self):
        r = self.client.post("/v1/chat", json={}, headers=self.h(self.owner))
        self.assertEqual(r.status_code, 400)


class ScopeRules(unittest.TestCase):
    def test_location_and_path_synonyms_preserve_scope(self):
        for verb in ("위치", "경로", "열기"):
            text = "파일 test/my music에서 메모 경로 보여줘"
            parsed = F.parse(raw(동작=verb, 대상="메모", 위치="test/my music"), text)
            action, _ = F.build(parsed)
            self.assertEqual(action["action"], "file.reveal")
            self.assertEqual(action["args"], {"target": "메모", "root": "test/my music"})

    def test_spoken_scope_cannot_be_dropped(self):
        for verb in ("찾기", "위치", "열기", "이름변경", "백업", "삭제", "보내기", "복사", "이동"):
            text = "파일 my music에서 메모 찾아줘" if verb == "찾기" else f"파일 my music에서 메모 {verb}해줘"
            parsed = F.parse(raw(동작=verb, 대상="메모"), text)
            action, say = F.build(parsed)
            self.assertIsNone(action)
            self.assertIn("어느 폴더", say)

    def test_scope_is_preserved_for_every_existing_target_action(self):
        for verb in ("찾기", "위치", "열기", "백업", "삭제", "보내기", "복사"):
            for where in ("my music", "test/my music", "/허용폴더/test/my music"):
                text = f"파일 {where}에서 메모 {verb}해줘"
                parsed = F.parse(raw(동작=verb, 대상="메모", 위치=where), text)
                action, _ = F.build(parsed)
                self.assertEqual(action["args"]["root"], where)
                self.assertNotIn("recursive", action["args"])

    def test_creation_uses_existing_location_and_keeps_new_name(self):
        parsed = F.parse(raw(동작="생성", 목적지="my music", 새이름="메모"), "파일 my music에서 메모 생성")
        action, _ = F.build(parsed)
        self.assertEqual(action["args"], {"dest": "my music", "name": "메모"})


if __name__ == "__main__":
    unittest.main()
