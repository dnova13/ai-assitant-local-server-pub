import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["ASSISTANT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from core import classifier
from clients import llm
from features import notes
from core.assistant import Assistant

REAL_ASK = classifier.ask_model


class Core(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def fake(messages, model=None, timeout=300):
            self.calls.append(messages)
            return "응답"
        llm.chat = fake
        # 자유 문장은 분류기를 거친다: 모델 없이 '일반대화'로 분류되게 바꿔 둔다
        classifier.ask_model = lambda text, model=None, timeout=120: ('{"의도": "일반대화"}', 0.0)
        self.bot = Assistant()

    def tearDown(self):
        classifier.ask_model = REAL_ASK

    def test_memo_flow(self):
        self.assertIn("저장", self.bot.handle("u", "!메모 내일 치과 3시"))
        self.bot.handle("u", "!메모 장보기 우유")
        self.assertIn("치과", self.bot.handle("u", "!메모 목록"))
        self.assertIn("우유", self.bot.handle("u", "!메모 찾기 우유"))
        self.assertNotIn("치과", self.bot.handle("u", "!메모 찾기 우유"))
        self.assertEqual(self.calls, [])  # 메모 기능은 모델을 쓰지 않음

    def test_user_isolation(self):
        self.bot.handle("a", "!메모 비밀")
        self.assertIn("없습니다", self.bot.handle("b", "!메모 찾기 비밀"))
        self.assertIn("없습니다", self.bot.handle("b", "!메모 삭제 1"))

    def test_delete(self):
        self.bot.handle("d", "!메모 지울것")
        n = notes.recent("d")[0][0]
        self.assertEqual(self.bot.handle("d", f"!메모 삭제 {n}"), "삭제했습니다.")
        self.assertIn("없습니다", self.bot.handle("d", "!메모 찾기 지울것"))

    def test_date_without_model(self):
        self.assertIn("요일", self.bot.handle("u", "!날짜"))
        self.assertEqual(self.calls, [])

    def test_chat_history_and_reset(self):
        self.bot.handle("c", "안녕")
        self.bot.handle("c", "내 이름은 철수")
        self.assertEqual(len(self.calls[-1]), 3)  # user, assistant, user
        self.bot.handle("c", "!초기화")
        self.bot.handle("c", "다시")
        self.assertEqual(len(self.calls[-1]), 1)

    def test_summary_and_error(self):
        self.assertEqual(self.bot.handle("u", "!요약 긴글"), "응답")
        def boom(messages, model=None, timeout=300):
            raise llm.LLMError("연결 실패")
        llm.chat = boom
        self.assertIn("⚠️", self.bot.handle("u", "질문이야"))
        self.assertEqual(self.bot.history["u"], [])  # 실패 시 기록 롤백... 

    def test_prompt_has_date(self):
        self.assertIn("현재 시각은", llm.system_prompt())


if __name__ == "__main__":
    unittest.main()
