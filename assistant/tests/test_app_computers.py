"""앱으로 등록한 컴퓨터: 이름 규칙, 등록·삭제, 연결 인증 (모델·네트워크 없음)."""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

try:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
except ImportError:
    TestClient = None

from core import apikeys, config, users
from features import app_computers as C


class Names(unittest.TestCase):
    def test_bad_names(self):
        for name in ("", "  ", "내 컴퓨터", "a/b", "@집", "!집", "이동", "채널", "등록", "추가", "만들어", "x" * 31, None, 3):
            with self.assertRaises(C.ComputerError, msg=name):
                C.clean_name(name)

    def test_good_names(self):
        self.assertEqual(C.clean_name(" 소니 "), "소니")
        self.assertEqual(C.clean_name("my-pc_2"), "my-pc_2")

    def test_decomposed_hangul_is_normalized(self):
        self.assertEqual(C.clean_name("소니"), "소니")


@unittest.skipIf(TestClient is None, "fastapi 가 없어서 건너뜀")
class Api(unittest.TestCase):
    def setUp(self):
        from entrances import api_server
        self.real = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "c.db")
        users.bootstrap([])
        users.add("owner", "family1", "가족")
        self.key = apikeys.create("owner", "앱")
        self.other = apikeys.create("family1", "앱")
        self.bot = apikeys.create("owner", "봇", kind="bot")
        self.client = TestClient(api_server.app)
        C._links.clear()

    def tearDown(self):
        config.DB_PATH = self.real
        C._links.clear()

    def h(self, key=None):
        return {"Authorization": f"Bearer {key or self.key}"}

    def add(self, name="소니", key=None):
        return self.client.post("/v1/computers", json={"name": name}, headers=self.h(key))

    def hello(self, ws, computer_id, key=None):
        ws.send_json({"type": "hello", "key": key or self.key, "computer_id": computer_id})
        return ws.receive_json()

    def connect(self):
        return self.client.websocket_connect("/v1/computers/connect")

    def test_register_list_remove(self):
        r = self.add()
        self.assertEqual(r.status_code, 201)
        cid = r.json()["id"]
        self.assertEqual(self.client.get("/v1/computers", headers=self.h()).json()["computers"][0]["name"], "소니")
        self.assertEqual(self.client.delete(f"/v1/computers/{cid}", headers=self.h()).status_code, 204)
        self.assertEqual(self.client.get("/v1/computers", headers=self.h()).json()["computers"], [])
        self.assertEqual(self.client.delete(f"/v1/computers/{cid}", headers=self.h()).status_code, 404)

    def test_unregister_releases_name_and_keeps_discord_identity(self):
        users.link("owner", "owner", "discord", "333")
        old = self.add("시험컴퓨터").json()["id"]
        other = self.add("다른컴퓨터", self.other).json()["id"]
        self.assertEqual(self.client.delete(f"/v1/computers/{old}", headers=self.h()).status_code, 204)
        again = self.add("시험컴퓨터")
        self.assertEqual(again.status_code, 201)
        self.assertNotEqual(again.json()["id"], old)
        self.assertEqual(users.resolve("discord", "333"), "owner")
        self.assertIsNotNone(C.get(other))

    def test_resume_same_id_reuses_row_and_rename_keeps_identity(self):
        old = self.add("소니").json()["id"]
        original = C.get(old)
        def resume(name):
            return self.client.post("/v1/computers", json={"name": name, "computer_id": old}, headers=self.h())
        self.assertEqual(resume("소니").json(), {"id": old, "name": "소니"})
        self.assertEqual(resume("작업용").json(), {"id": old, "name": "작업용"})
        self.assertEqual(C.get(old)["created"], original["created"])
        self.assertEqual(len(C.list_for("owner")), 1)
        self.assertIsNone(C.by_name("owner", "소니"))

    def test_resume_rejects_other_owner_name_collision_and_invalid_ids(self):
        old = self.add("소니").json()["id"]
        other = self.add("작업용").json()["id"]
        foreign = self.client.post("/v1/computers", json={"name": "소니", "computer_id": old}, headers=self.h(self.other))
        self.assertEqual(foreign.status_code, 403)
        conflict = self.client.post("/v1/computers", json={"name": "작업용", "computer_id": old}, headers=self.h())
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(C.get(old)["name"], "소니")
        self.assertEqual(C.get(other)["name"], "작업용")
        for invalid in ("", "bad", 1, {"id": old}):
            response = self.client.post("/v1/computers", json={"name": "소니", "computer_id": invalid}, headers=self.h())
            self.assertEqual(response.status_code, 400)

    def test_resume_deleted_id_gets_new_server_issued_id_and_respects_limit(self):
        from unittest.mock import patch
        old = self.add("소니").json()["id"]
        with patch.object(C, "MAX_PER_USER", 1):
            same = self.client.post("/v1/computers", json={"name": "작업용", "computer_id": old}, headers=self.h())
            self.assertEqual(same.json()["id"], old)
            self.assertEqual(self.add("추가컴퓨터").status_code, 409)
        self.client.delete(f"/v1/computers/{old}", headers=self.h())
        again = self.client.post("/v1/computers", json={"name": "작업용", "computer_id": old}, headers=self.h())
        self.assertEqual(again.status_code, 201)
        self.assertNotEqual(again.json()["id"], old)
        self.assertIsNone(C.get(old))
        self.assertEqual(len(C.list_for("owner")), 1)

    def test_needs_app_key(self):
        self.assertEqual(self.client.post("/v1/computers", json={"name": "소니"}).status_code, 401)
        self.assertEqual(self.add(key=self.bot).status_code, 403)

    def test_duplicate_and_reserved_names(self):
        self.add()
        r = self.add()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "conflict")
        self.assertEqual(self.add("이동").status_code, 400)

    def test_duplicate_ignores_case_and_is_per_account(self):
        self.add("Sony")
        self.assertEqual(self.add("sony").status_code, 409)
        self.assertEqual(self.add("Sony", key=self.other).status_code, 201)

    def test_limit_per_user(self):
        for i in range(C.MAX_PER_USER):
            self.add(f"pc{i}")
        self.assertEqual(self.add("넘침").status_code, 409)

    def test_cannot_remove_others(self):
        cid = self.add().json()["id"]
        self.assertEqual(self.client.delete(f"/v1/computers/{cid}", headers=self.h(self.other)).status_code, 404)
        self.assertIsNotNone(C.get(cid))

    def test_connect_ping_and_online(self):
        cid = self.add().json()["id"]
        with self.connect() as ws:
            self.assertEqual(self.hello(ws, cid), {"type": "welcome", "name": "소니"})
            ws.send_json({"type": "ping"})
            self.assertEqual(ws.receive_json(), {"type": "pong"})
            self.assertTrue(self.client.get("/v1/computers", headers=self.h()).json()["computers"][0]["online"])
        row = self.client.get("/v1/computers", headers=self.h()).json()["computers"][0]
        self.assertFalse(row["online"])
        self.assertIsNotNone(row["last_seen"])

    def test_bad_key_rejected(self):
        cid = self.add().json()["id"]
        for key in ("lai_wrong", self.bot):
            with self.connect() as ws:
                self.assertEqual(self.hello(ws, cid, key), {"type": "rejected", "reason": "bad_key"})
        self.assertEqual(C._links, {})

    def test_unknown_or_others_computer_rejected(self):
        cid = self.add().json()["id"]
        with self.connect() as ws:
            self.assertEqual(self.hello(ws, "pc_none"), {"type": "rejected", "reason": "unknown_computer"})
        with self.connect() as ws:
            self.assertEqual(self.hello(ws, cid, self.other), {"type": "rejected", "reason": "unknown_computer"})

    def test_first_message_must_be_hello(self):
        with self.connect() as ws:
            ws.send_json({"type": "ping"})
            with self.assertRaises(WebSocketDisconnect) as e:
                ws.receive_json()
            self.assertEqual(e.exception.code, 1008)
        with self.connect() as ws:
            ws.send_text("not json")
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()

    def test_silent_connection_is_closed(self):
        from entrances.api import app_v1
        real = app_v1.HELLO_SECONDS
        app_v1.HELLO_SECONDS = 0.05
        self.addCleanup(setattr, app_v1, "HELLO_SECONDS", real)
        with self.connect() as ws:
            with self.assertRaises(WebSocketDisconnect) as e:
                ws.receive_json()
            self.assertEqual(e.exception.code, 1008)

    def test_new_connection_replaces_old(self):
        cid = self.add().json()["id"]
        with self.connect() as first:
            self.hello(first, cid)
            with self.connect() as second:
                self.assertEqual(self.hello(second, cid)["type"], "welcome")
                self.assertEqual(first.receive_json(), {"type": "rejected", "reason": "replaced"})
                self.assertIs(type(C.link_of(cid)).__name__, "WebSocket")

    def test_remove_closes_live_connection(self):
        cid = self.add().json()["id"]
        with self.connect() as ws:
            self.hello(ws, cid)
            self.client.delete(f"/v1/computers/{cid}", headers=self.h())
            self.assertEqual(ws.receive_json(), {"type": "rejected", "reason": "unknown_computer"})

    def test_revoked_key_rejected_on_ping(self):
        cid = self.add().json()["id"]
        key_id = [k for k in apikeys.list_keys() if k["user"] == "owner" and k["name"] == "앱"][0]["id"]
        with self.connect() as ws:
            self.hello(ws, cid)
            apikeys.revoke(key_id)
            ws.send_json({"type": "ping"})
            self.assertEqual(ws.receive_json(), {"type": "rejected", "reason": "bad_key"})

    def test_upload_needs_owner_live_turn_and_size(self):
        from entrances.api import file_remote as R
        cid = self.add().json()["id"]
        url = f"/v1/computers/{cid}/files?turn=t1"
        hdr = {**self.h(), "x-file-name": "%EB%A9%94%EB%AA%A8.txt", "x-file-note": "n", "x-delete-days": "3"}
        self.assertEqual(self.client.post(url, content=b"abc", headers=hdr).status_code, 404)  # 진행 중이 아님
        R._turns["t1"] = (cid, None)
        self.addCleanup(R._turns.pop, "t1", None)
        self.addCleanup(R._files.clear)
        self.assertEqual(self.client.post(url, content=b"abc", headers={**hdr, **self.h(self.other)}).status_code, 404)
        self.assertEqual(self.client.post(url, content=b"abc", headers={**hdr, "x-file-name": "..%2Fa"}).status_code, 404)
        self.assertEqual(self.client.post(url, content=b"abc", headers=hdr).status_code, 204)
        self.assertEqual(R._files["t1"], {"name": "메모.txt", "data": b"abc", "note": "n", "days": 3})
        real = R.MAX_FILE_BYTES
        R.MAX_FILE_BYTES = 2
        self.addCleanup(setattr, R, "MAX_FILE_BYTES", real)
        self.assertEqual(self.client.post(url, content=b"abc", headers=hdr).status_code, 413)

    def test_health_says_computer_link(self):
        self.assertTrue(self.client.get("/v1/health", headers=self.h()).json()["features"]["computer_link"])


class Merged(unittest.TestCase):
    """!컴퓨터: 전원과 파일을 합쳐 보여 주고, 양쪽에 있는 이름은 어느 쪽인지 묻는다."""

    def setUp(self):
        from core import assistant as A
        from features import sysadmin
        self.sys = sysadmin
        self.real = (config.DB_PATH, sysadmin.config.SYSADMIN_CONTROL, sysadmin.computer_up)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "m.db")
        sysadmin.config.SYSADMIN_CONTROL = True
        sysadmin.computer_up = lambda name: False
        users.bootstrap([])
        users.add("owner", "family1", "가족")
        self.bot = A.Assistant()

    def tearDown(self):
        config.DB_PATH, self.sys.config.SYSADMIN_CONTROL, self.sys.computer_up = self.real

    def say(self, text, user="owner", admin=True):
        return self.bot.handle(user, text, allow_admin=admin)

    def test_list_merges_same_name_and_marks_default(self):
        self.sys.add_computer("소니", "u@10.0.0.9")
        C.register("owner", "소니")
        C.register("owner", "맥북")
        out = self.say("!컴퓨터")
        line = [x for x in out.splitlines() if x.startswith("- 소니")][0]
        self.assertIn("응답 없음", line)
        self.assertIn("파일 앱 꺼짐 (기본)", line)
        self.assertIn("- 맥북: 파일 앱 꺼짐\n", out + "\n")
        self.assertNotIn("10.0.0.9", out)

    def test_user_sees_only_own_file_computers(self):
        self.sys.add_computer("서버실", "u@10.0.0.9")
        C.register("owner", "소니")
        C.register("family1", "내폰")
        out = self.say("!컴퓨터 목록", "family1", False)
        self.assertIn("내폰", out)
        self.assertNotIn("서버실", out)
        self.assertNotIn("소니", out)

    def test_user_cannot_use_power(self):
        self.sys.add_computer("서버실", "u@10.0.0.9")
        for text in ("!컴퓨터 절전", "!컴퓨터 서버실 깨워", "!컴퓨터 삭제 서버실"):
            self.assertIn("관리자만", self.say(text, "family1", False), text)
        self.assertIn("서버실", self.sys.computers())

    def test_default(self):
        C.register("owner", "소니")
        macbook = C.register("owner", "맥북")
        self.assertEqual(C.default_of("owner")["name"], "소니")
        self.assertIn("기본 컴퓨터", self.say("!컴퓨터 기본 맥북"))
        self.assertEqual(C.default_of("owner")["id"], macbook["id"])
        self.assertIn("이름을 적어", self.say("!컴퓨터 기본 없음"))
        C.remove("owner", macbook["id"])
        self.assertEqual(C.default_of("owner")["name"], "소니")

    def test_edit_menu_address_needs_private_and_rename_file(self):
        self.sys.add_computer("소니", "u@10.0.0.9")
        C.register("owner", "소니")
        self.assertIn("1. 전원 주소", self.say("!컴퓨터 소니 수정"))
        self.assertIn("새 이름", self.say("3"))
        self.assertIn("파일 쪽 이름", self.say("작업실"))
        self.assertIsNotNone(C.by_name("owner", "작업실"))
        self.assertIn("소니", self.sys.computers())

    def test_edit_menu_both_names(self):
        self.sys.add_computer("소니", "u@10.0.0.9")
        C.register("owner", "소니")
        self.say("!컴퓨터 소니 수정")
        self.say("4")
        self.assertIn("전원과 파일", self.say("작업실"))
        self.assertIn("작업실", self.sys.computers())
        self.assertIsNotNone(C.by_name("owner", "작업실"))

    def test_rename_both_stops_when_file_refuses(self):
        self.sys.add_computer("소니", "u@10.0.0.9")
        C.register("owner", "소니")
        C.register("owner", "맥북")
        self.say("!컴퓨터 이름변경 소니 맥북")
        self.assertIn("이미 있는", self.say("3"))
        self.assertIn("소니", self.sys.computers())

    def test_delete_menu_file_only_keeps_power(self):
        self.sys.add_computer("소니", "u@10.0.0.9")
        C.register("owner", "소니")
        self.assertIn("1. 전원만", self.say("!컴퓨터 삭제 소니"))
        self.assertIn("파일 등록을 지웠어요", self.say("2"))
        self.assertIsNone(C.by_name("owner", "소니"))
        self.assertIn("소니", self.sys.computers())

    def test_delete_menu_both_asks_power_again(self):
        self.sys.add_computer("소니", "u@10.0.0.9")
        C.register("owner", "소니")
        self.say("!컴퓨터 소니 삭제")
        out = self.say("3")
        self.assertIn("파일 등록을 지웠어요", out)
        self.assertIn("저장한 MAC", out)
        self.assertIsNone(C.by_name("owner", "소니"))
        self.assertIn("소니", self.sys.computers())

    def test_user_deletes_own_file_computer_after_asking(self):
        C.register("family1", "내폰")
        self.assertIn("1. 지우기", self.say("!컴퓨터 삭제 내폰", "family1", False))
        self.assertIn("지웠어요", self.say("1", "family1", False))
        self.assertIsNone(C.by_name("family1", "내폰"))

    def test_other_word_closes_menu(self):
        self.sys.add_computer("소니", "u@10.0.0.9")
        C.register("owner", "소니")
        self.say("!컴퓨터 삭제 소니")
        self.say("!컴퓨터")
        self.assertNotIn("owner", self.bot.computer_menu)
        self.assertIsNotNone(C.by_name("owner", "소니"))

    def test_power_commands_still_work(self):
        self.assertIn("등록했어요", self.bot.handle("owner", "!컴퓨터 등록 프록시 u@10.0.0.9", allow_admin=True, private=True))
        self.assertIn("저장한 MAC", self.say("!컴퓨터 삭제 프록시"))


if __name__ == "__main__":
    unittest.main()
