"""봇이 비서 입구를 부르는 부분 (네트워크 없음, requests 를 가짜로 바꿔 시험)"""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import requests
from core import config
from clients import api_client


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data, self.ok = status, data, status < 400

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class Client(unittest.TestCase):
    def setUp(self):
        self.old = (config.BOT_API_KEY, config.API_URL)
        config.BOT_API_KEY, config.API_URL = "lai_test", "http://127.0.0.1:8050"

    def tearDown(self):
        config.BOT_API_KEY, config.API_URL = self.old

    def test_non_ascii_key_and_bad_url_are_api_errors(self):
        config.BOT_API_KEY = "lai_틀린키"
        with self.assertRaises(api_client.ApiError) as cm:
            api_client.send("discord", 1, "x")
        self.assertIn("BOT_API_KEY", str(cm.exception))
        config.BOT_API_KEY, config.API_URL = "lai_test", "주소아님"
        with self.assertRaises(api_client.ApiError) as cm:
            api_client.send("discord", 1, "x")
        self.assertNotIn("주소아님", str(cm.exception))

    def test_sends_key_and_body(self):
        with mock.patch("requests.post", return_value=Resp(200, {"user": "owner", "reply": "안녕"})) as p:
            self.assertEqual(api_client.send("discord", 123, "!날짜"), "안녕")
        args, kw = p.call_args
        self.assertEqual(args[0], "http://127.0.0.1:8050/internal/message")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer lai_test")
        self.assertEqual(kw["json"], {"source": "discord", "external_id": "123", "text": "!날짜"})

    def test_unlinked_user_gives_none(self):
        with mock.patch("requests.post", return_value=Resp(200, {"user": None, "reply": None})):
            self.assertIsNone(api_client.send("discord", 1, "안녕"))

    def test_errors_hide_url_and_key(self):
        cases = [(Resp(401), "봇 키"), (Resp(403), "봇 키"), (Resp(500), "500"), (Resp(200), "읽을 수")]
        for resp, word in cases:
            with mock.patch("requests.post", return_value=resp):
                with self.assertRaises(api_client.ApiError) as cm:
                    api_client.send("discord", 1, "x")
            msg = str(cm.exception)
            self.assertIn(word, msg)
            self.assertNotIn("127.0.0.1", msg)
            self.assertNotIn("lai_test", msg)

    def test_connection_and_timeout(self):
        for exc in (requests.ConnectionError("http://127.0.0.1:8050 lai_test"), requests.Timeout()):
            with mock.patch("requests.post", side_effect=exc):
                with self.assertRaises(api_client.ApiError) as cm:
                    api_client.send("discord", 1, "x")
            self.assertNotIn("127.0.0.1", str(cm.exception))
            self.assertNotIn("lai_test", str(cm.exception))

    def test_no_key(self):
        config.BOT_API_KEY = ""
        with self.assertRaises(api_client.ApiError):
            api_client.send("discord", 1, "x")


if __name__ == "__main__":
    unittest.main()
