"""Ollama 앞 인증(OLLAMA_API_KEY): 키가 있으면 모든 Ollama 요청에 Bearer 헤더, 없으면 헤더 없음. 실제 서버는 부르지 않는다."""
import os, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

import requests

from core import classifier, config
from clients import llm
from features import sysadmin

REAL_CHAT, REAL_EMBED, REAL_ASK = llm.chat, llm.embed, classifier.ask_system  # 다른 시험이 바꿔 둔 채 끝나도 진짜 함수를 시험한다


def ok(data):
    return mock.Mock(json=lambda: data, raise_for_status=lambda: None)


def denied(status=401):
    resp = mock.Mock(status_code=status)
    err = requests.HTTPError(response=resp)

    def raise_():
        raise err
    return mock.Mock(raise_for_status=raise_, status_code=status)


class Headers(unittest.TestCase):
    def setUp(self):
        self.real = (config.OLLAMA_API_KEY, config.OLLAMA_URL)
        config.OLLAMA_URL = "http://127.0.0.1:9"

    def tearDown(self):
        config.OLLAMA_API_KEY, config.OLLAMA_URL = self.real

    def calls(self):
        """Ollama 를 부르는 곳 다섯 군데를 한 번씩 부르고 보낸 헤더를 모은다."""
        from entrances.api import ollama_compat
        sent = []
        post = lambda url, **kw: sent.append(kw.get("headers")) or ok(
            {"message": {"content": "답"}, "embeddings": [[0.1]]})
        get = lambda url, **kw: sent.append(kw.get("headers")) or ok({"models": []})
        with mock.patch("requests.post", side_effect=post), mock.patch("requests.get", side_effect=get):
            REAL_CHAT([{"role": "user", "content": "q"}])
            REAL_EMBED(["가"])
            REAL_ASK("지시문", "문장")
            sysadmin.ollama_models()
            ollama_compat._ollama_get("/api/tags")
        return sent

    def test_key_sends_bearer_everywhere(self):
        config.OLLAMA_API_KEY = "test-key"
        self.assertEqual(self.calls(), [{"Authorization": "Bearer test-key"}] * 5)

    def test_no_key_no_header(self):
        config.OLLAMA_API_KEY = ""
        self.assertEqual(self.calls(), [{}] * 5)

    def test_auth_failure_message_has_no_secret(self):
        config.OLLAMA_API_KEY = "test-key"
        for fn, args, exc in ((REAL_CHAT, ([{"role": "user", "content": "q"}],), llm.LLMError),
                              (REAL_EMBED, (["가"],), llm.LLMError),
                              (REAL_ASK, ("지시문", "문장"), classifier.ClassifierError)):
            for status in (401, 403):
                with mock.patch("requests.post", return_value=denied(status)):
                    with self.assertRaises(exc) as cm:
                        fn(*args)
                msg = str(cm.exception)
                self.assertIn("OLLAMA_API_KEY", msg)
                self.assertNotIn("test-key", msg)
                self.assertNotIn("127.0.0.1", msg)


if __name__ == "__main__":
    unittest.main()
