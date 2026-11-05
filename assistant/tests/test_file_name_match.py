"""기존 이름 비교: 실제 AI·파일·운영 DB를 부르지 않고 경계와 API를 확인한다."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core import apikeys, classifier, config, users
from features import files


class Match(unittest.TestCase):
    CANDIDATES = [{"id": "c1", "name": "test"}, {"id": "c2", "name": "테스트"}, {"id": "c3", "name": "sample"}]

    def test_candidates_are_sent_without_paths_or_history(self):
        with mock.patch.object(classifier, "ask_system", return_value=('{"ids":["c1","c2"]}', 0)) as ask:
            self.assertEqual(files.match_names("테스트", self.CANDIDATES), ["c1", "c2"])
        system, text = ask.call_args.args
        self.assertEqual(json.loads(text), {"query": "테스트", "candidates": self.CANDIDATES})
        self.assertIn("뜻이 같은 단어로 번역하지 않는다", system)

    def test_no_match_is_empty_not_a_guessed_name(self):
        with mock.patch.object(classifier, "ask_system", return_value=('{"ids":[]}', 0)):
            self.assertEqual(files.match_names("없는이름", self.CANDIDATES), [])

    def test_model_cannot_invent_ids_or_return_paths(self):
        for raw in ('{"ids":["unknown"]}', '{"ids":["/tmp/test"]}', '{"ids":["c1","c1"]}',
                    '{"ids":[1]}', '{"ids":"c1"}', '[]', 'null', '잘못된 응답'):
            with self.subTest(raw=raw), mock.patch.object(classifier, "ask_system", return_value=(raw, 0)):
                with self.assertRaises(files.NameMatchError):
                    files.match_names("테스트", self.CANDIDATES)

    def test_bad_requests_never_call_the_model(self):
        cases = [(None, self.CANDIDATES), ("../테스트", self.CANDIDATES), ("테스트", []),
                 ("테스트", [{"id": "c1", "name": "/tmp/test"}]),
                 ("테스트", [{"id": "c1", "name": "a"}, {"id": "c1", "name": "b"}]),
                 ("테스트", [{"id": "c" + str(n), "name": "a"} for n in range(129)]),
                 ("테스트", [{"id": "c" + str(n), "name": "a" * 120} for n in range(60)])]
        with mock.patch.object(classifier, "ask_system") as ask:
            for query, candidates in cases:
                with self.subTest(query=query, size=len(candidates)), self.assertRaises(ValueError):
                    files.match_names(query, candidates)
            ask.assert_not_called()

    def test_network_failure_is_not_an_empty_success(self):
        with mock.patch.object(classifier, "ask_system", side_effect=classifier.ClassifierError("연결 실패")):
            with self.assertRaises(classifier.ClassifierError):
                files.match_names("테스트", self.CANDIDATES)


class Api(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from entrances import api_server
        self.previous = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "match.db")
        users.bootstrap([])
        self.key = apikeys.create("owner", "시험 앱")
        self.bot_key = apikeys.create("owner", "시험 봇", kind="bot")
        self.client = TestClient(api_server.app)
        self.body = {"query": "테스트", "candidates": Match.CANDIDATES}

    def tearDown(self):
        self.client.close()
        config.DB_PATH = self.previous

    def post(self, key=None, body=None):
        return self.client.post("/v1/files/match", json=body if body is not None else self.body,
                                headers={"Authorization": f"Bearer {key or self.key}"})

    def test_app_key_and_valid_ids(self):
        with mock.patch.object(classifier, "ask_system", return_value=('{"ids":["c1","c2"]}', 0)):
            result = self.post()
        self.assertEqual((result.status_code, result.json()), (200, {"ids": ["c1", "c2"]}))

    def test_bad_request_returns_400_without_model(self):
        with mock.patch.object(classifier, "ask_system") as ask:
            self.assertEqual(self.post(body={"query": "테스트", "candidates": []}).status_code, 400)
            ask.assert_not_called()

    def test_model_error_returns_502(self):
        with mock.patch.object(classifier, "ask_system", return_value=('{"ids":["outside"]}', 0)):
            self.assertEqual(self.post().status_code, 502)

    def test_bot_and_missing_key_are_rejected(self):
        with mock.patch.object(classifier, "ask_system") as ask:
            self.assertEqual(self.post(key=self.bot_key).status_code, 403)
            self.assertEqual(self.client.post("/v1/files/match", json=self.body).status_code, 401)
            ask.assert_not_called()

    def test_health_advertises_name_matching(self):
        result = self.client.get("/v1/health", headers={"Authorization": f"Bearer {self.key}"})
        self.assertIs(result.json()["features"]["file_name_match"], True)


if __name__ == "__main__":
    unittest.main()
