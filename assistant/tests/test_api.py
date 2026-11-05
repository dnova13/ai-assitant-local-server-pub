"""앱용 입구(FastAPI): 키 검사, Ollama 형식, 모델 전달, 관리자 제한, 봇 전용 주소 (모델·네트워크 없음)
fastapi 가 설치되어 있지 않으면 건너뛴다 (저장소 맨 위에서 pip install -r requirements.txt)"""
import json, os, sys, tempfile, unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

try:
    from fastapi.testclient import TestClient
except ImportError:  # fastapi/httpx 없음
    TestClient = None

from core import apikeys, classifier, config, users
from clients import llm


@unittest.skipIf(TestClient is None, "fastapi 가 없어서 건너뜀")
class Api(unittest.TestCase):
    def setUp(self):
        from entrances import api_server
        from entrances.api import common
        self.common = common
        self.api = api_server
        self.real = (config.DB_PATH, classifier.ask_model, llm.chat, requests.get, set(self.common.LOCAL_HOSTS))
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "a.db")
        users.bootstrap([])
        users.add("owner", "family1", "가족")
        users.link("owner", "owner", "discord", "111")
        users.link("owner", "family1", "discord", "222")
        self.models = []  # (분류 모델, 대화 모델) 기록

        def ask(text, model=None, timeout=120):
            self.models.append(("분류", model))
            return '{"의도": "일반대화"}', 0.0

        def chat(messages, model=None, timeout=300):
            self.models.append(("대화", model))
            return f"답:{messages[-1]['content']}"
        classifier.ask_model, llm.chat = ask, chat
        self.common.bot.history.clear()
        self.owner_key = apikeys.create("owner", "맥 앱")
        self.family_key = apikeys.create("family1", "가족 앱")
        self.bot_key = apikeys.create("owner", "디스코드 봇", kind="bot")
        self.client = TestClient(api_server.app)

    def tearDown(self):
        config.DB_PATH, classifier.ask_model, llm.chat, requests.get, hosts = self.real
        self.common.LOCAL_HOSTS.clear()
        self.common.LOCAL_HOSTS.update(hosts)

    def h(self, key):
        return {"Authorization": f"Bearer {key}"}

    def post_chat(self, key, text, **extra):
        body = {"model": "qwen2.5-coder:7b", "messages": [{"role": "system", "content": "앱 프롬프트"},
                                                         {"role": "user", "content": "이전 질문"},
                                                         {"role": "assistant", "content": "이전 답"},
                                                         {"role": "user", "content": text}], **extra}
        return self.client.post("/api/chat", json=body, headers=self.h(key))

    def test_confirmation_interpret_app_key_only_and_no_chat_history(self):
        from unittest.mock import patch
        with patch("core.classifier.ask_system", return_value=('{"decision":"yes"}', 0)) as model:
            body = {"question": "복사할까요?", "answer": "그래 진행해"}
            self.assertEqual(self.client.post("/v1/confirmation/interpret", json=body, headers=self.h(self.bot_key)).status_code, 403)
            model.assert_not_called()
            response = self.client.post("/v1/confirmation/interpret", json=body, headers=self.h(self.owner_key))
            self.assertEqual(response.json(), {"decision": "yes"})
            self.assertEqual(self.models, [])
            self.assertEqual(self.common.bot.history, {})
            self.assertEqual(self.client.post("/v1/confirmation/interpret", json={"question": "", "answer": "예"}, headers=self.h(self.owner_key)).status_code, 400)

    # --- 키 ---
    def test_root_needs_no_key(self):
        r = self.client.get("/")
        self.assertEqual((r.status_code, r.text), (200, "Ollama is running"))

    def test_key_required(self):
        for headers in ({}, self.h("lai_wrongkey"), {"Authorization": "Basic abc"}):
            r = self.client.get("/api/tags", headers=headers)
            self.assertEqual(r.status_code, 401)
            self.assertIn("error", r.json())

    def test_x_api_key_header_also_works(self):
        requests.get = lambda url, timeout=10, headers=None: FakeResp({"models": []})
        self.assertEqual(self.client.get("/api/tags", headers={"X-API-Key": self.owner_key}).status_code, 200)

    def test_revoked_key_rejected(self):
        apikeys.revoke(apikeys.list_keys()[0]["id"])
        self.assertEqual(self.post_chat(self.owner_key, "안녕", stream=False).status_code, 401)

    # --- 모델 목록 ---
    def test_tags_passthrough(self):
        seen = []
        data = {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5-coder:7b"}, {"name": "qwen2.5-coder:3b"}]}
        requests.get = lambda url, timeout=10, headers=None: seen.append(url) or FakeResp(data)
        r = self.client.get("/api/tags", headers=self.h(self.owner_key))
        self.assertEqual(r.json(), data)
        self.assertTrue(seen[0].endswith("/api/tags"))

    def test_tags_ollama_down(self):
        def down(url, timeout=10, headers=None):
            raise requests.ConnectionError()
        requests.get = down
        r = self.client.get("/api/tags", headers=self.h(self.owner_key))
        self.assertEqual(r.status_code, 502)

    # --- 대화 ---
    def test_chat_non_stream_uses_app_model_for_both(self):
        r = self.post_chat(self.owner_key, "파이썬 정렬 알려줘", stream=False)
        d = r.json()
        self.assertEqual(d["message"], {"role": "assistant", "content": "답:파이썬 정렬 알려줘"})
        self.assertTrue(d["done"])
        self.assertEqual(d["model"], "qwen2.5-coder:7b")
        self.assertEqual(self.models, [("분류", "qwen2.5-coder:7b"), ("대화", "qwen2.5-coder:7b")])

    def test_chat_stream_is_ndjson(self):
        r = self.post_chat(self.owner_key, "안녕")
        lines = [json.loads(x) for x in r.text.strip().split("\n")]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["message"]["content"], "답:안녕")
        self.assertFalse(lines[0]["done"])
        self.assertTrue(lines[1]["done"])

    def test_only_last_user_message_is_used(self):
        self.post_chat(self.owner_key, "새 질문", stream=False)
        hist = self.common.bot.history["owner"]
        self.assertNotIn("이전 질문", json.dumps(hist, ensure_ascii=False))

    def test_commands_work_without_model(self):
        d = self.post_chat(self.owner_key, "!도움말", stream=False).json()
        self.assertIn("사용법", d["message"]["content"])
        self.assertEqual(self.models, [])

    def test_server_commands_admin_only(self):
        out = self.post_chat(self.family_key, "!확인", stream=False).json()["message"]["content"]
        self.assertIn("관리자만", out)
        out = self.post_chat(self.owner_key, "!확인", stream=False).json()["message"]["content"]
        self.assertIn("대기 작업이 없습니다", out)

    def test_body_without_content_type(self):
        body = json.dumps({"model": "llama3.1:8b", "stream": False, "messages": [{"role": "user", "content": "!도움말"}]})
        r = self.client.post("/api/chat", content=body.encode(), headers=self.h(self.owner_key))
        self.assertIn("사용법", r.json()["message"]["content"])
        r = self.client.post("/api/chat", content=b"not json", headers=self.h(self.owner_key))
        self.assertEqual(r.status_code, 400)

    def test_bot_key_cannot_chat(self):
        self.assertEqual(self.post_chat(self.bot_key, "안녕", stream=False).status_code, 403)

    # --- 봇 전용 주소 ---
    def test_internal_only_from_server(self):
        r = self.client.post("/internal/message", json={"source": "discord", "external_id": "111", "text": "안녕"},
                             headers=self.h(self.bot_key))
        self.assertEqual(r.status_code, 403)  # 시험 클라이언트는 서버 자기 자신이 아님

    def test_internal_message(self):
        self.common.LOCAL_HOSTS.add("testclient")
        post = lambda ext, text, key=self.bot_key: self.client.post(
            "/internal/message", json={"source": "discord", "external_id": ext, "text": text}, headers=self.h(key))
        self.assertEqual(post("111", "안녕").json(), {"user": "owner", "reply": "답:안녕"})
        self.assertIn("관리자만", post("222", "!확인").json()["reply"])
        self.assertEqual(post("999", "안녕").json(), {"user": None, "reply": None})  # 연결 안 된 사람
        self.assertEqual(post("111", "안녕", key=self.owner_key).status_code, 403)  # 앱 키는 못 씀

    def test_file_channel_management_through_internal_api(self):
        from features import app_computers as C
        self.common.LOCAL_HOSTS.add("testclient")
        def post(text, dm=True, ext="111", key=None):
            return self.client.post("/internal/message", json={"source": "discord", "external_id": ext,
                "text": text, "dm": dm, "channel": {"id": "202", "name": "일반"}}, headers=self.h(key or self.bot_key))
        self.assertIn("사용법", post("!디스코드 채널").json()["reply"])
        self.assertIn("지정했어요", post("!디스코드 채널 등록 101").json()["reply"])
        self.assertTrue(C.is_designated("owner", "101"))
        self.assertFalse(C.is_designated("owner", "202"))  # 전달된 현재 채널이 아니라 명시 ID를 쓴다.
        self.assertIn("101", post("!디스코드 채널 목록").json()["reply"])
        self.assertIn("없음", post("!디스코드 채널 목록", ext="222").json()["reply"])
        self.assertEqual(post("!디스코드 채널 삭제 101", key=self.owner_key).status_code, 403)
        self.assertIsNone(post("!디스코드 채널 등록 303", ext="999").json()["reply"])
        self.assertFalse(C.is_designated("owner", "303"))
        self.assertIn("지정했어요", post("디스코드 채널 등록", dm=False).json()["reply"])
        self.assertTrue(C.is_designated("owner", "202"))
        self.assertIn("일반 (202)", post("!디스코드 채널 목록", dm=False).json()["reply"])
        self.assertIn("해제", post("!디스코드 채널 삭제", dm=False).json()["reply"])
        self.assertFalse(C.is_designated("owner", "202"))
        self.assertTrue(C.is_designated("owner", "101"))
        self.assertIn("해제", post("!디스코드 채널 해체 101").json()["reply"])
        self.assertFalse(C.is_designated("owner", "101"))
        self.assertEqual(self.models, [])


class FakeResp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


if __name__ == "__main__":
    unittest.main()
