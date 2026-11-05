"""벡터 저장소: sqlite 와 qdrant 에 같은 시험을 돌린다 (qdrant 는 서버 없이 메모리 모드, qdrant-client 가 없으면 건너뜀)."""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core import config
from clients import vector_store as V

try:
    import qdrant_client  # noqa: F401
    HAS_QDRANT = True
except ImportError:
    HAS_QDRANT = False

# 3차원 벡터로 충분하다: 방향이 비슷할수록 점수(코사인)가 높다
DOCKER, DISCORD, PIZZA = [1, 0, 0], [0, 1, 0], [0, 0, 1]


class StoreCases:
    def make(self):
        raise NotImplementedError

    def setUp(self):
        self.s = self.make()

    def fill(self):
        self.s.replace_file("kb-tech", "f1", "docker.md", "h1", "bge-m3", ["도커 설정", "도커 재시작"], [DOCKER, [0.9, 0.1, 0]])
        self.s.replace_file("kb-often", "f2", "discord.md", "h2", "bge-m3", ["디스코드 봇"], [DISCORD])

    def test_empty(self):
        self.assertEqual(self.s.count(), 0)
        self.assertEqual(self.s.search(DOCKER), [])
        self.assertEqual(self.s.files("kb-tech"), {})
        self.assertFalse(self.s.other_model("bge-m3"))

    def test_file_names(self):
        self.assertEqual(self.s.file_names(), {})
        self.fill()
        self.assertEqual(self.s.file_names(), {"kb-tech": ["docker.md"], "kb-often": ["discord.md"]})
        self.assertEqual(self.s.file_names(["kb-often"]), {"kb-often": ["discord.md"]})

    def test_search_order_and_score(self):
        self.fill()
        hits = self.s.search(DOCKER, k=2)
        self.assertEqual([h["text"] for h in hits], ["도커 설정", "도커 재시작"])
        self.assertAlmostEqual(hits[0]["score"], 1.0, places=4)  # 같은 방향이면 1
        self.assertEqual((hits[0]["kb"], hits[0]["file"]), ("kb-tech", "docker.md"))
        self.assertGreater(hits[0]["score"], hits[1]["score"])

    def test_scale_does_not_matter(self):
        self.fill()
        self.assertAlmostEqual(self.s.search([10, 0, 0], k=1)[0]["score"], 1.0, places=4)  # 코사인: 길이와 무관

    def test_filter_by_knowledge(self):
        self.fill()
        hits = self.s.search(DOCKER, kbs=["kb-often"], k=5)
        self.assertEqual([h["file"] for h in hits], ["discord.md"])
        self.assertAlmostEqual(hits[0]["score"], 0.0, places=4)  # 관련 없는 방향이면 0
        self.assertEqual(self.s.count(["kb-tech"]), 2)
        self.assertEqual(self.s.count(), 3)
        self.assertEqual(self.s.search(DOCKER, kbs=["없는-kb"]), [])

    def test_replace_and_delete_file(self):
        self.fill()
        self.s.replace_file("kb-tech", "f1", "docker.md", "h9", "bge-m3", ["새 내용"], [PIZZA])
        self.assertEqual(self.s.count(["kb-tech"]), 1)  # 파일의 조각을 통째로 바꾼다
        self.assertEqual(self.s.files("kb-tech"), {"f1": "h9"})
        self.s.delete_file("kb-tech", "f1")
        self.assertEqual(self.s.count(["kb-tech"]), 0)
        self.assertEqual(self.s.count(), 1)

    def test_other_model(self):
        self.fill()
        self.assertFalse(self.s.other_model("bge-m3"))
        self.assertTrue(self.s.other_model("nomic-embed-text"))


class Sqlite(StoreCases, unittest.TestCase):
    def make(self):
        return V.SqliteStore(":memory:")


@unittest.skipUnless(HAS_QDRANT, "qdrant-client 없음")
class Qdrant(StoreCases, unittest.TestCase):
    def make(self):
        return V.QdrantStore(url=":memory:", collection="t")


class Choose(unittest.TestCase):
    def setUp(self):
        self.real = (config.VECTOR_STORE, config.VECTOR_DB_PATH, V._store)
        V._store = None

    def tearDown(self):
        config.VECTOR_STORE, config.VECTOR_DB_PATH, V._store = self.real

    def test_sqlite_default(self):
        config.VECTOR_STORE, config.VECTOR_DB_PATH = "sqlite", ":memory:"
        self.assertIsInstance(V.open_store(), V.SqliteStore)
        self.assertIs(V.open_store(), V.open_store())  # 한 번 만들어 재사용

    def test_bad_value(self):
        config.VECTOR_STORE = "chroma"
        with self.assertRaises(V.StoreError):
            V.open_store()

    def test_qdrant_not_installed(self):
        config.VECTOR_STORE = "qdrant"
        with mock.patch.dict(sys.modules, {"qdrant_client": None}):
            with self.assertRaises(V.StoreError) as cm:
                V.open_store()
        self.assertIn("qdrant-client", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
