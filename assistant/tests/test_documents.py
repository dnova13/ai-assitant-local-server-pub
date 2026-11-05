"""문서 검색 (5_1_4 17절): Open WebUI 지식 기반 읽기(가짜 응답), 조각 나누기, 색인 동기화, 검색·답 흐름, !문서 명령, 입구 거르기.
실제 서버(Ollama, Open WebUI, Qdrant)는 부르지 않는다."""
import json, os, sys, tempfile, threading, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

import requests

from core import assistant as A
from core import config
from clients import llm, openwebui_client as OW, vector_store as V
from features import documents as D

CFG = {"all_knowledge": ["kb-extra"],
       "categories": [{"aliases": ["자주"], "name": "자주 보는 문서", "knowledge": ["kb-often"]},
                      {"aliases": ["기술"], "name": "기술 문서", "knowledge": ["kb-tech"]},
                      {"aliases": ["포트폴리오"], "name": "포트폴리오 문서", "knowledge": []}],
       "max_sources": 2, "min_score": 0.6, "refusals": ["찾을 수 없습니다"]}
ALL = ["kb-often", "kb-tech", "kb-extra"]
REAL_CHAT, REAL_EMBED = llm.chat, llm.embed  # 다른 시험이 바꿔 둔 채 끝나도 진짜 함수를 시험한다


def hit(file, score, text="조각", kb="kb-tech"):
    return {"kb": kb, "file": file, "score": score, "text": text}


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self.data = status, data
        self.ok = status < 400

    def json(self):
        if self.data is None:
            raise ValueError
        return self.data


class Client(unittest.TestCase):
    """Open WebUI 는 색인할 때 글을 가져오는 데만 쓴다 (생성 요청 없음)."""

    def setUp(self):
        self.real = (config.OPENWEBUI_API_KEY, config.OPENWEBUI_URL)
        config.OPENWEBUI_API_KEY, config.OPENWEBUI_URL = "sk-test", "http://localhost:3011"

    def tearDown(self):
        config.OPENWEBUI_API_KEY, config.OPENWEBUI_URL = self.real

    def get(self, fn, *args, resp=None, exc=None):
        with mock.patch("requests.get", return_value=resp, side_effect=exc) as p:
            return fn(*args), p

    def test_knowledge_list(self):
        for data in ({"items": [{"id": "k1", "name": "기술", "file_count": 2}], "total": 1},  # 지금 서버 버전 (2026-10-28)
                     [{"id": "k1", "name": "기술", "files": [{}, {}]}]):                      # 옛 모양
            r, p = self.get(OW.knowledge_list, resp=Resp(200, data))
            self.assertEqual(r, [{"id": "k1", "name": "기술", "files": 2}])
        args, kw = p.call_args
        self.assertTrue(args[0].endswith("/api/v1/knowledge/"))
        self.assertEqual(kw["headers"], {"Authorization": "Bearer sk-test"})

    def test_knowledge_files_paged(self):
        """지금 서버 버전: /knowledge/{id}/files 가 페이지 단위 (items, total)."""
        pages = {"1": {"items": [{"id": "f1", "filename": "docker.md", "meta": {"name": "docker.md"}, "hash": "x"},
                                 {"id": "f2", "filename": "b.pdf", "meta": {}}], "total": 3, "directories": []},
                 "2": {"items": [{"id": "f3", "filename": "c.md"}, {"x": 1}], "total": 3}}
        urls = []

        def fake_get(url, headers=None, timeout=None):
            urls.append(url)
            return Resp(200, pages[url.rsplit("page=", 1)[1]])
        with mock.patch("requests.get", side_effect=fake_get):
            r = OW.knowledge_files("k1")
        self.assertEqual(r, [{"id": "f1", "name": "docker.md"}, {"id": "f2", "name": "b.pdf"}, {"id": "f3", "name": "c.md"}])
        self.assertTrue(urls[0].endswith("/api/v1/knowledge/k1/files?page=1"))
        self.assertEqual(len(urls), 2)

    def test_knowledge_files_stops_on_empty_page(self):
        with mock.patch("requests.get", return_value=Resp(200, {"items": [], "total": 5})) as p:
            self.assertEqual(OW.knowledge_files("k1"), [])
        self.assertEqual(p.call_count, 1)

    def test_knowledge_files_old_shape(self):
        """옛 버전: /files 경로가 없으면(404) 상세의 files."""
        old = {"id": "k1", "files": [{"id": "f1", "meta": {"name": "docker.md"}}, {"id": "f2", "filename": "b.pdf"}, {"x": 1}, "이상한 값"]}
        with mock.patch("requests.get", side_effect=[Resp(404, {}), Resp(200, old)]) as p:
            r = OW.knowledge_files("k1")
        self.assertEqual(r, [{"id": "f1", "name": "docker.md"}, {"id": "f2", "name": "b.pdf"}])
        self.assertTrue(p.call_args[0][0].endswith("/api/v1/knowledge/k1"))
        with mock.patch("requests.get", side_effect=[Resp(404, {}), Resp(200, {"id": "k1", "files": None})]):
            with self.assertRaises(OW.OpenWebUIError):
                OW.knowledge_files("k1")  # 둘 다 없으면 형식 오류

    def test_knowledge_files_auth_error_is_not_hidden(self):
        with mock.patch("requests.get", return_value=Resp(401, {})):
            with self.assertRaises(OW.OpenWebUIError) as cm:
                OW.knowledge_files("k1")
        self.assertEqual(cm.exception.kind, "auth")

    def test_file_text(self):
        r, p = self.get(OW.file_text, "f1", resp=Resp(200, {"content": "본문"}))
        self.assertEqual(r, "본문")
        self.assertTrue(p.call_args[0][0].endswith("/api/v1/files/f1/data/content"))
        r, _ = self.get(OW.file_text, "f1", resp=Resp(200, {}))
        self.assertEqual(r, "")

    def test_errors(self):
        cases = [(Resp(401, {}), None, "auth"), (Resp(403, {}), None, "auth"), (None, requests.Timeout(), "connect"),
                 (None, requests.ConnectionError(), "connect"), (Resp(200, None), None, "format"),
                 (Resp(500, {}), None, "connect"), (Resp(404, {}), None, "format")]
        for resp, exc, kind in cases:
            with self.assertRaises(OW.OpenWebUIError, msg=kind) as cm:
                self.get(OW.knowledge_list, resp=resp, exc=exc)
            self.assertEqual(cm.exception.kind, kind)
            self.assertNotIn("localhost", str(cm.exception))  # 주소·키는 문구에 넣지 않는다
            self.assertNotIn("sk-test", str(cm.exception))

    def test_missing_key(self):
        config.OPENWEBUI_API_KEY = ""
        with self.assertRaises(OW.OpenWebUIError) as cm:
            OW.knowledge_list()
        self.assertEqual(cm.exception.kind, "auth")


class Embed(unittest.TestCase):
    def test_request_and_errors(self):
        with mock.patch("requests.post", return_value=mock.Mock(json=lambda: {"embeddings": [[0.1], [0.2]]},
                                                                raise_for_status=lambda: None)) as p:
            self.assertEqual(REAL_EMBED(["가", "나"]), [[0.1], [0.2]])
        self.assertTrue(p.call_args[0][0].endswith("/api/embed"))
        self.assertEqual(p.call_args[1]["json"], {"model": config.EMBED_MODEL, "input": ["가", "나"]})
        with mock.patch("requests.post", side_effect=requests.ConnectionError()):
            with self.assertRaises(llm.LLMError) as cm:
                REAL_EMBED(["가"])
        self.assertNotIn(config.OLLAMA_URL, str(cm.exception))
        with mock.patch("requests.post", return_value=mock.Mock(json=lambda: {"embeddings": [[0.1]]}, raise_for_status=lambda: None)):
            with self.assertRaises(llm.LLMError):
                REAL_EMBED(["가", "나"])  # 개수가 다르면 오류

    def test_chat_system_and_length(self):
        with mock.patch("requests.post", return_value=mock.Mock(json=lambda: {"message": {"content": " 답 "}},
                                                                raise_for_status=lambda: None)) as p:
            self.assertEqual(REAL_CHAT([{"role": "user", "content": "q"}], system="문서 지시문", max_tokens=400), "답")
        body = p.call_args[1]["json"]
        self.assertEqual(body["messages"][0], {"role": "system", "content": "문서 지시문"})
        self.assertEqual(body["options"]["num_predict"], 400)


class Chunk(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(D.chunk("한 문단", 100, 10), ["한 문단"])
        self.assertEqual(D.chunk("  \n\n ", 100, 10), [])

    def test_paragraphs_are_packed(self):
        text = "\n\n".join(["가" * 30, "나" * 30, "다" * 30])
        chunks = D.chunk(text, 70, 0)
        self.assertEqual(chunks, ["가" * 30 + "\n\n" + "나" * 30, "다" * 30])

    def test_long_paragraph_is_cut_with_overlap(self):
        chunks = D.chunk("".join(str(i % 10) for i in range(250)), 100, 20)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual(chunks[0][-20:], chunks[1][:20])  # 이웃 조각은 겹친다
        self.assertEqual("".join(c if i == 0 else c[20:] for i, c in enumerate(chunks)),
                         "".join(str(i % 10) for i in range(250)))  # 빠진 글자 없음

    def test_overlap_between_paragraph_chunks(self):
        chunks = D.chunk("\n\n".join(["가" * 40, "나" * 40]), 60, 10)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[1].startswith("가" * 10))


class FakeStore:
    def __init__(self, hits=None, count=5, other=False):
        self.hits, self.n, self.other, self.searches = hits or [], count, other, []
        self.names = {}

    def count(self, kbs=None):
        return self.n

    def other_model(self, model):
        return self.other

    def search(self, vector, kbs=None, k=5):
        self.searches.append((vector, kbs, k))
        return self.hits

    def file_names(self, kbs=None):
        self.asked = kbs
        return self.names


class Search(unittest.TestCase):
    def setUp(self):
        self.real = D.load_config
        D.load_config = lambda: json.loads(json.dumps(CFG))
        self.store = FakeStore([hit("a.md", 0.7), hit("a.md", 0.65), hit("b.md", 0.62), hit("c.md", 0.61)])
        self.embeds, self.answers = [], []
        self.reply = "답입니다"

    def tearDown(self):
        D.load_config = self.real

    def embed(self, texts):
        self.embeds.append(texts)
        return [[1.0, 0.0]] * len(texts)

    def answer(self, system, question):
        self.answers.append((system, question))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def go(self, text, classify_fn=None):
        return D.handle(text, classify_fn, answer=self.answer, embed=self.embed, store=self.store)

    def test_category_by_first_word(self):
        out = self.go("기술 도커 설정")
        self.assertEqual(self.embeds, [["도커 설정"]])
        self.assertEqual(self.store.searches[0][1:], (["kb-tech"], config.DOC_TOP_K))
        self.assertTrue(out["found"])
        self.assertIn("📎 출처: [기술 문서] a.md, b.md", out["text"])  # 같은 파일은 한 번, max_sources 2개
        self.assertNotIn("c.md", out["text"])

    def test_answer_gets_chunks_and_question(self):
        self.go("기술 도커 설정")
        system, question = self.answers[0]
        self.assertIn("문서에 없습니다", system)
        self.assertIn("(1) 파일: a.md", question)
        self.assertTrue(question.endswith("[질문]\n도커 설정"))

    def test_alias_with_josa(self):
        self.go("기술에서 도커 설정")
        self.assertEqual(self.store.searches[-1][1], ["kb-tech"])

    def test_all_when_no_alias(self):
        out = self.go("환불 규정 찾아줘")
        self.assertEqual(self.store.searches[-1][1], ALL)
        self.assertIn("[전체]", out["text"])

    def test_alias_must_be_whole_word(self):
        self.go("기술문서 작성법")
        self.assertEqual(self.store.searches[-1][1], ALL)

    def test_alias_at_the_start_ignores_spaces(self):
        for aliases in (["기술문서"], ["기술 문서"]):
            cfg = json.loads(json.dumps(CFG))
            cfg["categories"][1]["aliases"] = aliases
            D.load_config = lambda: cfg
            for text, kbs, target in (("기술문서 도커 설정", ["kb-tech"], "도커 설정"),
                                      ("기술 문서 도커 설정", ["kb-tech"], "도커 설정"),
                                      ("기술 문서에서 도커 설정", ["kb-tech"], "도커 설정"),
                                      ("기술 스택이 뭐야?", ALL, "기술 스택이 뭐야?"),
                                      ("기술문서화 방법", ALL, "기술문서화 방법"),
                                      ("내가 쓰는 기술 문서는?", ALL, "내가 쓰는 기술 문서는?")):
                self.go(text)
                self.assertEqual((self.store.searches[-1][1], self.embeds[-1]), (kbs, [target]), text)
            self.assertIn("무엇을 찾을지", self.go("기술 문서")["text"])

    def test_low_scores_are_not_found_without_model(self):
        self.store.hits = [hit("a.md", 0.55)]
        self.assertEqual(self.go("오늘 기분"), {"kind": "not_found", "found": False, "text": D.NOT_FOUND})
        self.assertEqual(self.answers, [])  # 못 찾으면 모델을 부르지 않는다

    def test_no_hits_is_not_found(self):
        self.store.hits = []
        self.assertFalse(self.go("피자 반죽")["found"])
        self.assertEqual(self.answers, [])

    def test_only_good_sources_go_to_model_and_shown(self):
        self.store.hits = [hit("good.md", 0.7, "좋은 조각"), hit("weak.md", 0.5, "약한 조각")]
        out = self.go("질문")["text"]
        self.assertIn("good.md", out)
        self.assertNotIn("weak.md", out)
        self.assertNotIn("약한 조각", self.answers[0][1])

    def test_refusal_is_not_found(self):
        for r in ("문서에 없습니다.", "찾을 수 없습니다", "  "):
            self.reply = r
            self.assertEqual(self.go("질문")["kind"], "not_found", r)

    def test_refusal_words_later_in_answer_are_ok(self):
        self.reply = "Top K는 5입니다. " * 10 + "다른 내용은 문서에 없습니다."
        self.assertTrue(self.go("질문")["found"])

    def test_category_not_ready(self):
        out = self.go("포트폴리오 이력서")
        self.assertIn("아직 준비되지 않았어요", out["text"])
        self.assertEqual((self.embeds, self.answers), ([], []))

    def test_nothing_indexed(self):
        self.store.n = 0
        out = self.go("도커")
        self.assertEqual(out["kind"], "info")
        self.assertIn("doc_index", out["text"])
        self.assertEqual(self.embeds, [])

    def test_embed_model_changed(self):
        self.store.other = True
        with self.assertRaises(D.DocumentError) as cm:
            self.go("도커")
        self.assertIn("다시 색인", str(cm.exception))

    def test_empty_and_alias_only(self):
        self.assertIn("사용법", self.go("")["text"])
        self.assertIn("무엇을 찾을지", self.go("기술")["text"])
        self.assertEqual(self.embeds, [])

    def test_no_knowledge_configured(self):
        D.load_config = lambda: {**json.loads(json.dumps(CFG)), "all_knowledge": [],
                                 "categories": [{"aliases": ["기술"], "name": "기술 문서", "knowledge": []}]}
        with self.assertRaises(D.DocumentError):
            self.go("도커")

    def test_embed_and_store_errors(self):
        def bad_embed(texts):
            raise llm.LLMError("문서 검색용 임베딩 모델에 연결할 수 없어요.")
        with self.assertRaises(D.DocumentError):
            D.handle("도커", answer=self.answer, embed=bad_embed, store=self.store)

        class BadStore(FakeStore):
            def count(self, kbs=None):
                raise V.StoreError("문서 저장소(Qdrant)에 연결할 수 없어요.")
        with self.assertRaises(D.DocumentError):
            D.handle("도커", answer=self.answer, embed=self.embed, store=BadStore())

    def test_example_config_is_valid(self):
        real_path = config.DOCUMENTS_CONFIG
        config.DOCUMENTS_CONFIG = os.path.join(tempfile.mkdtemp(), "없는파일.json")
        try:
            cfg = self.real()  # documents.json 이 없으면 example 을 읽는다
        finally:
            config.DOCUMENTS_CONFIG = real_path
        self.assertNotIn("all_model", cfg)
        self.assertTrue(all("knowledge" in c for c in cfg["categories"]))
        self.assertTrue(all(a != "문서" for c in cfg["categories"] for a in c["aliases"]))  # '문서' 하나만은 별칭으로 쓰지 않는다


class Handle(Search):
    def must_not_classify(self, text):
        raise AssertionError("모델에 묻지 않아야 한다")

    def test_category_word_needs_no_model(self):
        out = self.go(" 카테고리 ", self.must_not_classify)
        self.assertEqual(out["kind"], "info")
        self.assertIn("기술 문서", out["text"])
        self.assertIn("포트폴리오 문서 (아직 준비 안 됨)", out["text"])
        self.assertEqual((self.embeds, self.answers, self.store.searches), ([], [], []))

    def test_list_word_shows_file_names_by_category(self):
        self.store.names = {"kb-tech": ["b.md", "a.md"], "kb-often": ["c.md"], "kb-extra": ["d.md"]}
        out = self.go("목록", self.must_not_classify)
        self.assertEqual(out["kind"], "info")
        self.assertEqual(out["text"], "📄 색인된 문서 (4개)\n[자주 보는 문서] c.md\n[기술 문서] a.md, b.md\n[기타] d.md")
        self.assertEqual(self.store.asked, ALL)
        self.assertEqual((self.embeds, self.answers), ([], []))

    def test_list_when_nothing_indexed(self):
        self.assertIn("아직 색인된 문서가 없어요", self.go("목록")["text"])

    def test_list_store_error(self):
        def bad(kbs=None):
            raise V.StoreError("문서 저장소(Qdrant)에 연결할 수 없어요.")
        self.store.file_names = bad
        with self.assertRaises(D.DocumentError):
            self.go("목록")

    def test_list_words_in_a_question_are_a_search(self):
        for text in ("목록 보여줘", "카테고리 나누는 법", "어떤 문서들이 있어?"):
            self.embeds = []
            self.assertEqual(self.go(text, lambda t: None)["kind"], "found", text)
            self.assertEqual(self.embeds, [[text]])

    def test_model_saying_list_still_searches(self):
        text = "내가 한 개인 프로젝트는 뭐야?"
        classify = lambda t: D.parse('{"동작": "목록", "검색어": null}', t)
        out = self.go(text, classify)
        self.assertEqual(out["kind"], "found")
        self.assertEqual(self.store.searches[-1][1], ALL)

    def test_alias_inside_a_sentence_is_not_a_category(self):
        seen = []
        def classify(t):
            seen.append(t)
            return {"동작": "검색", "카테고리": "기술", "검색어": None}  # 모델이 지어내도 쓰지 않는다
        text = "선착순 쿠폰 프로젝트는 어떤 기술을 썼어?"
        self.go(text, classify)
        self.assertEqual(self.store.searches[-1][1], ALL)
        self.assertEqual(seen, [text])

    def test_model_sees_the_sentence_without_the_alias(self):
        seen = []
        self.go("기술 도커 설정", lambda t: seen.append(t))
        self.assertEqual(seen, ["도커 설정"])
        self.assertEqual(self.store.searches[-1][1], ["kb-tech"])

    def test_alias_alone_does_not_call_the_model(self):
        self.assertIn("무엇을 찾을지", self.go("기술", self.must_not_classify)["text"])

    def test_summary_question(self):
        self.go("계약서 A 요약해줘", lambda t: {"동작": "요약", "검색어": "계약서 A"})
        self.assertEqual(self.embeds[-1], ["계약서 A"])  # 검색은 대상 이름으로
        self.assertTrue(self.answers[-1][1].endswith("계약서 A 내용을 문서에 있는 것만으로 짧게 요약해 줘."))

    def test_classifier_failure_uses_default_path(self):
        self.go("기술 도커", lambda t: None)
        self.assertEqual((self.embeds[-1], self.store.searches[-1][1]), (["도커"], ["kb-tech"]))


class Index(unittest.TestCase):
    """색인 동기화: 실제 sqlite 저장소(메모리) + 가짜 Open WebUI·임베딩."""

    def setUp(self):
        self.store = V.SqliteStore(":memory:")
        self.kb = {"kb-often": [{"id": "f1", "name": "a.md"}], "kb-tech": [{"id": "f2", "name": "b.md"}], "kb-extra": []}
        self.texts = {"f1": "가 문서\n\n둘째 문단", "f2": "나 문서"}
        self.embedded = []
        self.cfg = json.loads(json.dumps(CFG))

    def embed(self, texts):
        self.embedded += texts
        return [[1.0, float(len(t)), 0.5] for t in texts]

    def run_index(self, **kw):
        return D.index(self.cfg, self.store, list_files=lambda kb: self.kb[kb], get_text=lambda f: self.texts[f],
                       embed=self.embed, report=lambda *_: None, **kw)

    def test_first_then_unchanged(self):
        s = self.run_index()
        self.assertEqual((s["added"], s["skipped"], s["deleted"]), (2, 0, 0))
        self.assertEqual(self.store.count(), 2)
        self.embedded = []
        s = self.run_index()
        self.assertEqual((s["added"], s["skipped"]), (0, 2))
        self.assertEqual(self.embedded, [])  # 글이 같으면 다시 임베딩하지 않는다

    def test_changed_and_removed_files(self):
        self.run_index()
        self.texts["f1"] = "바뀐 내용"
        self.kb["kb-tech"] = []
        s = self.run_index()
        self.assertEqual((s["added"], s["skipped"], s["deleted"]), (1, 0, 1))
        self.assertEqual(self.store.files("kb-tech"), {})
        self.assertEqual(self.store.search([1, 5, 0.5], k=1)[0]["text"], "바뀐 내용")

    def test_rebuild_and_model_change(self):
        self.run_index()
        self.assertEqual(self.run_index(rebuild=True)["added"], 2)
        real = config.EMBED_MODEL
        config.EMBED_MODEL = "다른-모델"
        try:
            self.assertEqual(self.run_index()["added"], 2)  # 임베딩 모델이 바뀌면 전부 다시
            self.assertFalse(self.store.other_model("다른-모델"))
        finally:
            config.EMBED_MODEL = real

    def test_batches_are_sequential(self):
        self.texts["f1"] = "\n\n".join(f"문단 {i} " + "가" * 700 for i in range(D.EMBED_BATCH + 3))
        calls = []
        self.embed_orig = self.embed
        self.embed = lambda texts: calls.append(len(texts)) or self.embed_orig(texts)
        self.run_index()
        self.assertEqual(calls[:2], [D.EMBED_BATCH, 3])

    def test_error_stops(self):
        def boom(texts):
            raise llm.LLMError("문서 검색용 임베딩 모델에 연결할 수 없어요.")
        self.embed = boom
        with self.assertRaises(D.DocumentError):
            self.run_index()
        self.assertEqual(self.store.count(), 0)

    def test_owui_error(self):
        def bad(kb):
            raise OW.OpenWebUIError("auth", "문서 색인 설정에 문제가 있어요.")
        with self.assertRaises(D.DocumentError):
            D.index(self.cfg, self.store, list_files=bad, get_text=lambda f: "", embed=self.embed, report=lambda *_: None)


class Patched(unittest.TestCase):
    """본체 시험용: 저장소·임베딩·답 모델을 가짜로 바꾼다."""

    def setUp(self):
        from core import classifier
        self.real = (D.load_config, D.classify, V._store, llm.embed, llm.chat, classifier.ask_model)
        D.load_config = lambda: json.loads(json.dumps(CFG))
        D.classify = lambda text, model=None, timeout=120: None  # 분류기 실패 -> 코드 기본 경로
        self.store = FakeStore([hit("discord-setup.md", 0.65)])
        V._store = self.store
        llm.embed = lambda texts, model=None, timeout=120: [[1.0]] * len(texts)
        self.chats = []

        def chat(messages, model=None, timeout=300, system=None, max_tokens=None):
            self.chats.append((model, system, max_tokens))
            if system:
                return self.doc_reply
            return "대화 답"
        llm.chat = chat
        self.doc_reply = "Restart=always"
        self.bot = A.Assistant()

    def tearDown(self):
        from core import classifier
        D.load_config, D.classify, V._store, llm.embed, llm.chat, classifier.ask_model = self.real


class Command(Patched):
    def test_command(self):
        out = self.bot.handle("owner", "!문서 디스코드 봇 재시작 설정")
        self.assertIn("Restart=always", out)
        self.assertIn("discord-setup.md", out)
        self.assertEqual(self.store.searches[0][1], ALL)
        self.assertEqual(self.chats, [(None, D.ANSWER_PROMPT, config.DOC_MAX_TOKENS)])  # 비서 모델(ASSISTANT_MODEL)로 답

    def test_app_model_is_used(self):
        out = self.bot.handle_ex("owner", "!문서 디스코드 봇", model="qwen2.5-coder:7b")
        self.assertIn("Restart=always", out["text"])
        self.assertEqual(self.chats[-1][0], "qwen2.5-coder:7b")

    def test_error_message(self):
        class Bad(FakeStore):
            def count(self, kbs=None):
                raise V.StoreError("문서 저장소(Qdrant)에 연결할 수 없어요.")
        V._store = Bad()
        self.assertEqual(self.bot.handle("owner", "!문서 도커"), "⚠️ 문서 저장소(Qdrant)에 연결할 수 없어요.")


class IndexCommand(Patched):
    """!문서 색인: 관리자만, 뒤에 말이 없을 때만. 가짜 Open WebUI + 메모리 sqlite."""

    def setUp(self):
        super().setUp()
        V._store = self.index_store = V.SqliteStore(":memory:")
        self.kb = {"kb-often": [{"id": "f1", "name": "a.md"}], "kb-tech": [], "kb-extra": []}
        self.listed = []
        def files(kb):
            self.listed.append(kb)
            return self.kb[kb]
        for p in (mock.patch.object(OW, "knowledge_files", files),
                  mock.patch.object(OW, "file_text", lambda f: "가 문서")):
            p.start()
            self.addCleanup(p.stop)

    def test_admin_indexes_then_only_changes(self):
        self.assertRegex(self.bot.handle("owner", "!문서 색인"), r"^색인 완료 \d+초: 새로 1개\(조각 1개\), 그대로 0개, 지움 0개$")
        self.assertEqual(self.index_store.count(), 1)
        self.assertRegex(self.bot.handle("owner", "!문서 색인"), r"새로 0개\(조각 0개\), 그대로 1개, 지움 0개$")
        self.kb["kb-often"] = []
        self.assertRegex(self.bot.handle("owner", "!문서 색인"), r"새로 0개\(조각 0개\), 그대로 0개, 지움 1개$")

    def test_not_admin_is_refused(self):
        self.assertEqual(self.bot.handle("guest", "!문서 색인", allow_admin=False), "이 명령은 관리자만 쓸 수 있어요.")
        self.assertEqual(self.listed, [])

    def test_only_the_exact_command_indexes(self):
        for text in ("!문서 색인 규칙 알려줘", "문서 색인", "!문서 색인해줘"):
            self.bot.handle("owner", text)
            self.assertEqual(self.listed, [], text)

    def test_not_admin_can_still_search(self):
        V._store = self.store
        self.assertIn("Restart=always", self.bot.handle("guest", "!문서 색인 규칙 알려줘", allow_admin=False))

    def test_error_message(self):
        def fail(kb):
            raise OW.OpenWebUIError("connect", "Open WebUI 에 연결할 수 없어요.")
        with mock.patch.object(OW, "knowledge_files", fail):
            self.assertEqual(self.bot.handle("owner", "!문서 색인"), "⚠️ Open WebUI 에 연결할 수 없어요.")

    def test_no_knowledge(self):
        D.load_config = lambda: {"categories": [], "all_knowledge": []}
        self.assertIn("지식 기반이 없어요", self.bot.handle("owner", "!문서 색인"))

    def test_holds_the_gpu_lock(self):
        seen = []
        def files(kb):
            t = threading.Thread(target=lambda: seen.append(self.bot._lock.acquire(blocking=False)))
            t.start()
            t.join()
            return []
        with mock.patch.object(OW, "knowledge_files", files):
            self.bot.handle("owner", "!문서 색인")
        self.assertEqual(set(seen), {False})  # 색인 중에는 다른 요청이 GPU 를 잡지 못한다


class EvalRoute(Patched):
    def go(self, text, want):
        from scripts import doc_eval
        return doc_eval.matches(doc_eval.route(text), "문서", want)

    def test_alias_order_and_spaces_do_not_matter(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["categories"][1]["aliases"] = ["기술 문서", "tech"]
        D.load_config = lambda: cfg
        for text in ("!문서 기술문서 도커", "!문서 기술 문서 도커", "!문서 tech 도커"):
            self.assertTrue(self.go(text, "기술문서"), text)
            self.assertFalse(self.go(text, "포트폴리오"), text)
            self.assertFalse(self.go(text, None), text)

    def test_other_results_compare_as_they_are(self):
        self.assertTrue(self.go("!문서 환불 규정", None))
        self.assertFalse(self.go("!문서 환불 규정", "기술문서"))
        self.assertTrue(self.go("!문서 목록", "목록"))


class ListCommands(Patched):
    def test_anyone_can_see_them_without_the_model(self):
        self.store.names = {"kb-tech": ["a.md"]}
        for text, want in (("!문서 카테고리", "기술 문서"), ("!문서 목록", "[기술 문서] a.md"), ("문서 목록", "[기술 문서] a.md")):
            self.assertIn(want, self.bot.handle("guest", text, allow_admin=False), text)
        self.assertEqual(self.chats, [])
        self.assertEqual(self.store.searches, [])


class DocClassifier(unittest.TestCase):
    def p(self, d, text):
        return D.parse(json.dumps(d, ensure_ascii=False), text)

    def test_ok(self):
        r = self.p({"동작": "검색", "검색어": "도커 설정"}, "도커 설정 찾아줘")
        self.assertEqual(r, {"동작": "검색", "검색어": "도커 설정"})

    def test_prompt_does_not_ask_for_a_category(self):
        self.assertNotIn("카테고리", D.DOC_PROMPT)

    def test_summary_needs_the_word(self):
        self.assertEqual(self.p({"동작": "요약"}, "계약서 A 내용이 뭐야?")["동작"], "검색")
        self.assertEqual(self.p({"동작": "요약"}, "계약서 A 정리해줘")["동작"], "요약")

    def test_list_is_not_an_action(self):
        self.assertIsNone(self.p({"동작": "목록"}, "어떤 문서들이 있어?"))
        self.assertNotIn("목록", D.DOC_PROMPT)

    def test_answer_prompt_rules(self):
        self.assertIn("B로 답한다", D.ANSWER_PROMPT)
        self.assertIn(f'"{D.NOT_IN_DOC}."', D.ANSWER_PROMPT)

    def test_invented_query_dropped(self):
        self.assertIsNone(self.p({"동작": "검색", "검색어": "세금 신고"}, "환불 규정 찾아줘")["검색어"])

    def test_bad_format(self):
        self.assertIsNone(D.parse("아님", "x"))
        self.assertIsNone(self.p({"동작": "삭제"}, "x"))

    def test_start_word(self):
        for t in ("문서 환불 규정", "문서에서 도커 찾아줘", "문서를 기술 도커", "  문서 기술", "문서"):
            self.assertTrue(D.starts_with_document(t), t)
        for t in ("문서화가 왜 중요해?", "기술 문서에서 도커 찾아줘", "문서작성 요령", "내 문서 보여줘"):
            self.assertFalse(D.starts_with_document(t), t)
        self.assertEqual(D.strip_start("문서에서 도커 설정"), "도커 설정")


class Routing(Patched):
    """5_1_4 12절의 시험 문장: 입구에서 코드가 파일 / 문서 / 평소로 가른다."""

    def setUp(self):
        super().setUp()
        from core import classifier
        from features import files as F
        self.F, self.real_file = F, F.classify
        self.route = []
        real_search = self.store.search
        self.store.search = lambda v, kbs=None, k=5: self.route.append(("문서", kbs)) or (
            real_search(v, kbs, k) if self.doc_found else [])
        self.doc_found = True

        def fake_file(text, model=None, timeout=120):
            self.route.append(("파일", text))
            verb = "폴더만들기" if "만들어" in text else ("이동" if "옮겨" in text else "아님")
            return F.parse(json.dumps({"동작": verb, "대상": "보고서.pdf" if "보고서" in text else None,
                                       "목적지": "문서" if verb != "아님" else None, "새이름": "2026" if verb == "폴더만들기" else None,
                                       "위치": None}, ensure_ascii=False), text)
        F.classify = fake_file
        classifier.ask_model = lambda text, model=None, timeout=120: self.route.append(("평소", text)) or ('{"의도": "일반대화"}', 0.0)

    def tearDown(self):
        self.F.classify = self.real_file
        super().tearDown()

    def go(self, text):
        self.route = []
        out = self.bot.handle_ex("owner", text, client_actions=True)
        return [r[0] for r in self.route], out

    def test_file_first(self):
        kinds, out = self.go("문서 폴더에 2026 폴더 만들어줘")
        self.assertEqual(kinds, ["파일"])
        self.assertEqual(out["action"]["action"], "file.mkdir")
        kinds, out = self.go("문서에 있는 보고서.pdf 옮겨줘")
        self.assertEqual(kinds, ["파일"])

    def test_document_start(self):
        kinds, out = self.go("문서 기술 도커 설정 찾아줘")
        self.assertEqual(kinds, ["문서"])
        self.assertEqual(self.route[0][1], ["kb-tech"])
        kinds, _ = self.go("문서 환불 규정 찾아줘")
        self.assertEqual(self.route[0][1], ALL)

    def test_not_found_falls_back_with_notice(self):
        self.doc_found = False
        kinds, out = self.go("문서 작성하는 법 알려줘")
        self.assertEqual(kinds, ["문서", "평소"])
        self.assertTrue(out["text"].startswith(D.NOT_FOUND_PREFIX))
        self.assertIn("대화 답", out["text"])

    def test_not_document_start(self):
        for t in ("문서화가 왜 중요해?", "기술 문서에서 도커 설정 찾아줘"):
            kinds, _ = self.go(t)
            self.assertEqual(kinds, ["평소"], t)

    def test_file_word_but_document_request(self):
        kinds, _ = self.go("문서 기술 백업 설정 찾아줘")  # '백업' 때문에 파일 분류를 거치지만 '아님'이면 문서로
        self.assertEqual(kinds, ["파일", "문서"])

    def test_bang_commands(self):
        self.doc_found = False
        _, out = self.go("!문서 포트폴리오 이력서")
        self.assertIn("아직 준비되지 않았어요", out["text"])
        _, out = self.go("!문서 피자")
        self.assertEqual(out["text"], D.NOT_FOUND)  # !문서 는 일반 대화로 넘기지 않는다

    def test_server_error_is_not_hidden(self):
        def boom(texts, model=None, timeout=120):
            raise llm.LLMError("문서 검색용 임베딩 모델에 연결할 수 없어요.")
        llm.embed = boom
        _, out = self.go("문서 환불 규정")
        self.assertEqual(out["text"], "⚠️ 문서 검색용 임베딩 모델에 연결할 수 없어요.")


if __name__ == "__main__":
    unittest.main()
