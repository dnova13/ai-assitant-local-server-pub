"""뉴스: 명령, 권한, 저장, 기사 고르기, 구독 시각, 내부 주소 차단, 입구 (모델·네트워크·디스코드 없음)"""
import asyncio, os, sys, tempfile, unittest
from datetime import datetime, timedelta
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

import requests

from clients import db, llm
from core import apikeys, assistant as A, config, users
from entrances import reminders
from features import briefing, news as N, schedule

NOW = datetime(2026, 11, 3, 7, 10)
CHANNEL = {"id": "500", "name": "뉴스-it"}
RSS = """<?xml version="1.0"?><rss><channel><title>피드 이름 - 설명</title>
<item><title>첫 기사 - 한겨레</title><link>https://a.example/1?utm_source=rss&amp;id=7</link><source url="https://h">한겨레</source></item>
<item><title><![CDATA[둘째 기사 &amp; 끝]]></title><link>https://a.example/2</link></item>
<item><title></title><link>https://a.example/3</link></item>
</channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>GeekNews - 개발 뉴스</title>
<entry><title>아톰 기사</title><link rel="self" href="https://b.example/self"/><link href="https://b.example/1"/></entry></feed>"""


def articles(prefix, n, press=""):
    return [(f"{prefix}{i}", f"https://x.example/{prefix}{i}", press) for i in range(1, n + 1)]


class Base(unittest.TestCase):
    def setUp(self):
        self.real = (config.DB_PATH, schedule.now, N.fetch_feed)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "n.db")
        N._retry_at.clear()
        self.now = NOW
        schedule.now = lambda: self.now
        self.feeds, self.calls = {}, []

        def fetch(url, timeout=10):
            self.calls.append(url)
            found = self.feeds.get(url, [])
            if isinstance(found, Exception):
                raise found
            return found
        N.fetch_feed = fetch
        users.bootstrap([])
        self.bot = A.Assistant()
        llm_patch = mock.patch.object(llm, "chat", side_effect=AssertionError("뉴스는 모델을 부르지 않는다"))
        llm_patch.start()
        self.addCleanup(llm_patch.stop)

    def tearDown(self):
        config.DB_PATH, schedule.now, N.fetch_feed = self.real

    def say(self, text, admin=True, channel=None):
        return self.bot.handle("owner", text, allow_admin=admin, channel=channel)

    def clear_sources(self):
        N.categories()
        with db.connect() as c:
            c.execute("DELETE FROM news_sources")

    def add_raw(self, category, *urls):
        with db.connect() as c:
            for url in urls:
                c.execute("INSERT INTO news_sources(category, url, fails, created) VALUES (?,?,0,?)", (category, url, NOW))


class Feed(unittest.TestCase):
    def test_rss_press_and_title(self):
        self.assertEqual(N.parse_feed(RSS), [("첫 기사", "https://a.example/1?id=7", "한겨레"),
                                             ("둘째 기사 & 끝", "https://a.example/2", "피드 이름")])

    def test_atom_skips_self_link(self):
        self.assertEqual(N.parse_feed(ATOM), [("아톰 기사", "https://b.example/1", "GeekNews")])

    def test_bad_xml(self):
        self.assertEqual(N.parse_feed("<html>"), [])

    def test_clean_link(self):
        self.assertEqual(N.clean_link("https://x/a?utm_source=r&utm_medium=s"), "https://x/a")
        self.assertEqual(N.clean_link("https://x/a?id=1&utm_campaign=c"), "https://x/a?id=1")

    def test_search_url_editions(self):
        self.assertIn("hl=ko&gl=KR&ceid=KR:ko", N.search_url("보안"))
        self.assertIn("when%3A7d", N.search_url("보안"))
        jp = N.search_url("일본:アニメ 新作")
        self.assertIn("hl=ja&gl=JP&ceid=JP:ja", jp)
        self.assertNotIn("%EC%9D%BC%EB%B3%B8", jp)  # '일본:'은 검색어에 넣지 않는다
        self.assertIn("hl=en-US&gl=US&ceid=US:en", N.search_url("미국:Billboard Hot 100"))
        self.assertIn("site%3Aexample.com", N.search_url("site:example.com"))


def addr(ip):
    return [(2, 1, 6, "", (ip, 443))]


class Url(unittest.TestCase):
    def test_blocks_scheme_and_credentials(self):
        for url in ("ftp://a.example/x", "file:///etc/passwd", "gopher://a.example", "http://", "https://u:p@a.example/"):
            with self.assertRaises(N.NewsError, msg=url):
                N.check_url(url)

    def test_blocks_internal(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.0.2", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1"):
            with mock.patch.object(N.socket, "getaddrinfo", return_value=addr(ip)):
                with self.assertRaises(N.NewsError, msg=ip):
                    N.check_url("https://a.example/")
        for ip in ("::1", "fe80::1%en0", "fc00::1", "::ffff:127.0.0.1"):
            with mock.patch.object(N.socket, "getaddrinfo", return_value=[(30, 1, 6, "", (ip, 443, 0, 0))]):
                with self.assertRaises(N.NewsError, msg=ip):
                    N.check_url("https://a.example/")

    def test_blocks_if_any_ip_is_internal(self):
        with mock.patch.object(N.socket, "getaddrinfo", return_value=addr("93.184.216.34") + addr("10.0.0.1")):
            with self.assertRaises(N.NewsError):
                N.check_url("https://a.example/")

    def test_public_ok(self):
        with mock.patch.object(N.socket, "getaddrinfo", return_value=addr("93.184.216.34")):
            N.check_url("https://a.example/feed")


class FakeResponse:
    def __init__(self, status=200, body=b"", location=None):
        self.status_code, self.body = status, body
        self.is_redirect = location is not None
        self.headers = {"Location": location} if location else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        yield self.body


class Fetch(unittest.TestCase):
    def resolve(self, host, port, proto=0):
        return addr("10.0.0.1" if host == "inner.example" else "93.184.216.34")

    def run_fetch(self, responses):
        with mock.patch.object(N.socket, "getaddrinfo", side_effect=self.resolve), \
                mock.patch.object(N.requests, "get", side_effect=responses) as get:
            try:
                return N.fetch_feed("https://a.example/feed"), get
            except N.NewsError as e:
                return e, get

    def test_follow_redirect_checks_again(self):
        result, get = self.run_fetch([FakeResponse(location="https://inner.example/x")])
        self.assertIsInstance(result, N.NewsError)
        self.assertEqual(get.call_count, 1)  # 내부 주소로는 접속하지 않는다
        self.assertIs(get.call_args.kwargs["allow_redirects"], False)

    def test_relative_redirect(self):
        result, get = self.run_fetch([FakeResponse(location="/rss"), FakeResponse(body=RSS.encode())])
        self.assertEqual(len(result), 2)
        self.assertEqual(get.call_args.args[0], "https://a.example/rss")

    def test_too_many_redirects(self):
        result, get = self.run_fetch([FakeResponse(location="/n")] * 9)
        self.assertIsInstance(result, N.NewsError)
        self.assertEqual(get.call_count, N.MAX_REDIRECTS + 1)

    def test_too_big(self):
        with mock.patch.object(N, "MAX_BYTES", 10):
            result, _ = self.run_fetch([FakeResponse(body=b"x" * 11)])
        self.assertIsInstance(result, N.NewsError)


class Permission(unittest.TestCase):
    def test_needs_admin(self):
        for rest in ("카테고리 추가 음악", "카테고리 삭제 음악", "구독 전체", "구독 it", "구독 it 07:00", "구독 it 개수 10", "구독 it 취소",
                     "출처 추가 it https://a", "출처 삭제 it 1", "관심어 추가 it 보안", "관심어 삭제 it 보안",
                     "제외 추가 어딘가", "제외 삭제 어딘가"):
            self.assertTrue(N.needs_admin(rest), rest)
        for rest in ("", "10", "일반", "음악 10", "도움말", "사용법", "카테고리", "구독", "출처", "관심어", "제외"):
            self.assertFalse(N.needs_admin(rest), rest)


class Commands(Base):
    def test_defaults(self):
        out = self.say("!뉴스 카테고리")
        self.assertIn("it (기술)", out)
        self.assertIn("일반", out)
        self.assertEqual(len(N.sources("it")), 4)
        self.assertEqual(len(N.sources("일반")), 1)

    def test_help(self):
        self.assertIn("뉴스 사용법", self.say("!뉴스 도움말"))
        self.assertIn("뉴스 사용법", self.say("!뉴스 사용법"))
        self.assertIn("뉴스 [카테고리] [개수]", self.say("!도움말"))

    def test_non_admin_refused(self):
        self.assertIn("관리자만", self.say("!뉴스 출처 추가 it https://a.example/", admin=False))
        self.assertIn("관리자만", self.say("!뉴스 구독 it 07:00", admin=False, channel=CHANNEL))
        self.assertIn("관리자만", self.say("!뉴스 카테고리 삭제 일반", admin=False))
        self.assertIn("뉴스 카테고리", self.say("!뉴스 카테고리", admin=False))

    def test_unknown_category_does_not_search(self):
        out = self.say("!뉴스 경제")
        self.assertIn("'경제' 카테고리는 없어요", out)
        self.assertIn("it (기술)", out)
        self.assertEqual(self.calls, [])

    def test_count_limit(self):
        self.assertIn("1~20", self.say("!뉴스 21"))
        self.assertIn("1~20", self.say("!뉴스 0"))
        self.assertIn("1~20", self.say("!뉴스 일반 99"))
        self.assertIn("사용법", self.say("!뉴스 일반 많이"))

    def test_category_add_rules(self):
        self.assertIn("추가했어요", self.say("!뉴스 카테고리 추가 음악"))
        self.assertIn("이미 있어요", self.say("!뉴스 카테고리 추가 음악"))
        self.assertIn("이미 있어요", self.say("!뉴스 카테고리 추가 기술"))  # 별칭
        for bad in ("구독", "관심어", "출처", "제외", "카테고리", "도움말", "사용법", "123"):
            self.assertIn("쓸 수 없어요", self.say(f"!뉴스 카테고리 추가 {bad}"))

    def test_category_delete_asks_once(self):
        self.say("!뉴스 카테고리 추가 음악")
        self.say("!뉴스 관심어 추가 음악 빌보드")
        self.assertIn("지울까요", self.say("!뉴스 카테고리 삭제 음악"))
        self.assertIn("취소", self.say("아니요"))
        self.assertEqual(N.resolve("음악"), "음악")
        self.say("!뉴스 카테고리 삭제 음악")
        self.assertIn("지웠어요", self.say("네"))
        self.assertIsNone(N.resolve("음악"))
        self.assertEqual(N.keywords("음악"), [])

    def test_delete_defaults_refused(self):
        self.assertIn("지울 수 없어요", self.say("!뉴스 카테고리 삭제 it"))
        self.assertIn("지울 수 없어요", self.say("!뉴스 카테고리 삭제 기술"))
        self.assertIn("지울 수 없어요", self.say("!뉴스 카테고리 삭제 일반"))

    def test_delete_subscribed_refused(self):
        self.say("!뉴스 카테고리 추가 음악")
        self.say("!뉴스 구독 음악 09:00", channel=CHANNEL)
        self.assertIn("구독을 먼저 취소", self.say("!뉴스 카테고리 삭제 음악"))
        self.say("!뉴스 구독 음악 취소", channel=CHANNEL)
        self.assertIn("지울까요", self.say("!뉴스 카테고리 삭제 음악"))

    def test_source_add_reads_first(self):
        url = "https://a.example/feed"
        self.assertIn("찾지 못해", self.say(f"!뉴스 출처 추가 it {url}"))
        self.feeds[url] = requests.ConnectionError()
        self.assertIn("읽지 못해", self.say(f"!뉴스 출처 추가 it {url}"))
        self.feeds[url] = articles("가", 2)
        self.assertIn("기사 2건", self.say(f"!뉴스 출처 추가 it {url}"))
        self.assertIn("이미 등록", self.say(f"!뉴스 출처 추가 it {url}"))
        self.assertIn(url, self.say("!뉴스 출처"))
        self.assertIn("지웠어요", self.say(f"!뉴스 출처 삭제 it {url}"))
        self.assertIn("없어요", self.say(f"!뉴스 출처 삭제 it {url}"))

    def test_source_site_and_delete_by_number(self):
        self.feeds[N.search_url("site:example.com")] = articles("가", 1)
        self.assertIn("추가했어요", self.say("!뉴스 출처 추가 일반 site:Example.com"))
        self.assertIn("형식이 틀렸어요", self.say("!뉴스 출처 추가 일반 site:없음"))
        self.assertIn("site:example.com", self.say("!뉴스 출처 삭제 일반 2"))

    def test_source_internal_refused(self):
        N.fetch_feed = self.real[2]
        with mock.patch.object(N.requests, "get", side_effect=AssertionError("접속하면 안 된다")):
            out = self.say("!뉴스 출처 추가 it http://127.0.0.1:6333")
        self.assertIn("내부 주소", out)
        self.assertNotIn("127.0.0.1", self.say("!뉴스 출처"))

    def test_source_limit(self):
        self.add_raw("일반", *[f"https://s{i}.example/" for i in range(N.MAX_SOURCES)])
        self.assertIn("까지예요", self.say("!뉴스 출처 추가 일반 https://new.example/"))

    def test_keywords(self):
        self.assertIn("추가했어요", self.say("!뉴스 관심어 추가 it 보안"))
        self.assertIn("일본:オリコン ランキング", self.say("!뉴스 관심어 추가 it 일본:オリコン ランキング"))
        self.assertIn("이미 등록", self.say("!뉴스 관심어 추가 it 보안"))
        self.assertIn("알 수 없어요", self.say("!뉴스 관심어 추가 it 일본:"))
        self.assertIn("보안", self.say("!뉴스 관심어"))
        self.assertIn("보안", self.say("!뉴스 관심어 삭제 it 1"))
        self.assertIn("없어요", self.say("!뉴스 관심어 삭제 it 없는말"))
        for i in range(N.MAX_KEYWORDS):
            self.say(f"!뉴스 관심어 추가 일반 단어{i}")
        self.assertIn("까지예요", self.say("!뉴스 관심어 추가 일반 넘침"))

    def test_keywords_many_at_once(self):
        out = self.say("!뉴스 관심어 추가 it LLM, 오픈소스,클라우드 ,, 개봉  영화, LLM")
        self.assertIn("LLM, 오픈소스, 클라우드, 개봉 영화", out)
        self.assertEqual([r[1] for r in N.keywords("it")], ["LLM", "오픈소스", "클라우드", "개봉 영화"])
        out = self.say("!뉴스 관심어 추가 it 보안, LLM")
        self.assertIn("추가했어요: 보안", out)
        self.assertIn("이미 등록한 관심어예요: LLM", out)
        self.assertNotIn("추가했어요", self.say("!뉴스 관심어 추가 it 보안"))
        self.assertIn("알 수 없어요", self.say("!뉴스 관심어 추가 it , ,"))

    def test_keywords_many_all_or_nothing(self):
        self.assertIn("알 수 없어요", self.say("!뉴스 관심어 추가 it 보안, 일본:"))
        self.assertEqual(N.keywords("it"), [])
        words = ", ".join(f"단어{i}" for i in range(N.MAX_KEYWORDS + 1))
        self.assertIn("까지예요", self.say(f"!뉴스 관심어 추가 it {words}"))
        self.assertEqual(N.keywords("it"), [])

    def test_keywords_delete_many(self):
        self.say("!뉴스 관심어 추가 it 가, 나, 다, 라")
        out = self.say("!뉴스 관심어 삭제 it 1, 다, 없는말")
        self.assertIn("지웠어요: 가, 다", out)
        self.assertIn("없는 관심어: 없는말", out)
        self.assertEqual([r[1] for r in N.keywords("it")], ["나", "라"])
        self.say("!뉴스 관심어 삭제 it 1, 2")  # 번호는 지우기 전 목록 기준
        self.assertEqual(N.keywords("it"), [])

    def test_excluded(self):
        self.assertIn("없어요", self.say("!뉴스 제외"))
        self.assertIn("뺄게요", self.say("!뉴스 제외 추가 아무 신문"))
        self.assertIn("이미 제외", self.say("!뉴스 제외 추가 아무 신문"))
        self.assertIn("아무 신문", self.say("!뉴스 제외"))
        self.assertIn("풀었어요", self.say("!뉴스 제외 삭제 아무 신문"))
        self.assertIn("목록에 없어요", self.say("!뉴스 제외 삭제 아무 신문"))

    def test_subscribe_only_in_channel(self):
        self.assertIn("디스코드 채널에서만", self.say("!뉴스 구독"))
        self.assertIn("디스코드 채널에서만", self.say("!뉴스 구독 it 07:00"))
        self.assertIn("구독한 채널이 없어요", self.say("!뉴스 구독 전체"))

    def test_subscribe_change_cancel(self):
        self.assertIn("구독한 뉴스가 없어요", self.say("!뉴스 구독", channel=CHANNEL))
        self.assertIn("!뉴스 구독 <카테고리>", self.say("!뉴스 구독", channel=CHANNEL))
        self.assertIn("07:00, 12:00", self.say("!뉴스 구독 기술 12:00,7:00", channel=CHANNEL))
        self.assertIn("19:00", self.say("!뉴스 구독 it 19:00 22:00", channel=CHANNEL))
        self.assertEqual(len(N._sub_rows()), 1)  # 다시 치면 시각만 바뀐다
        self.assertIn("it: 19:00, 22:00 / 5개", self.say("!뉴스 구독", channel=CHANNEL))
        self.assertIn("#뉴스-it it", self.say("!뉴스 구독 전체"))
        self.assertIn("시각을 알 수 없어요", self.say("!뉴스 구독 it 25:00", channel=CHANNEL))
        self.assertIn("없어요", self.say("!뉴스 구독 없는것 07:00", channel=CHANNEL))
        self.assertIn("사용법", self.say("!뉴스 구독 취소 it", channel=CHANNEL))
        self.assertIn("취소했어요", self.say("!뉴스 구독 it 취소", channel=CHANNEL))
        self.assertIn("구독하지 않았어요", self.say("!뉴스 구독 it 취소", channel=CHANNEL))

    def test_subscribe_add_then_times(self):
        self.assertIn("시각을 정해야", self.say("!뉴스 구독 일반", channel=CHANNEL))
        self.assertIn("일반: 시각 없음", self.say("!뉴스 구독", channel=CHANNEL))
        self.assertIn("이미 구독", self.say("!뉴스 구독 일반", channel=CHANNEL))
        self.assertEqual(len(N._sub_rows()), 1)
        self.assertIn("없어요", self.say("!뉴스 구독 경제", channel=CHANNEL))
        self.say("!뉴스 구독 일반 07:00", channel=CHANNEL)
        self.assertIn("일반: 07:00 / 5개", self.say("!뉴스 구독 일반", channel=CHANNEL))

    def test_subscribe_count(self):
        self.assertIn("먼저", self.say("!뉴스 구독 일반 개수 10", channel=CHANNEL))
        self.say("!뉴스 구독 일반 07:00", channel=CHANNEL)
        self.assertIn("10개씩", self.say("!뉴스 구독 일반 개수 10", channel=CHANNEL))
        self.assertIn("일반: 07:00 / 10개", self.say("!뉴스 구독", channel=CHANNEL))
        self.say("!뉴스 구독 일반 19:00", channel=CHANNEL)  # 시각을 바꿔도 개수는 그대로
        self.assertIn("일반: 19:00 / 10개", self.say("!뉴스 구독", channel=CHANNEL))
        self.assertIn("1~20", self.say("!뉴스 구독 일반 개수 21", channel=CHANNEL))
        self.assertIn("사용법", self.say("!뉴스 구독 일반 개수", channel=CHANNEL))
        self.assertIn("사용법", self.say("!뉴스 구독 개수 10", channel=CHANNEL))

    def test_channels_are_separate(self):
        other = {"id": "600", "name": "음악"}
        self.say("!뉴스 구독 it 07:00", channel=CHANNEL)
        self.say("!뉴스 구독 일반 08:00", channel=other)
        self.assertNotIn("일반", self.say("!뉴스 구독", channel=CHANNEL))
        self.assertNotIn("it:", self.say("!뉴스 구독", channel=other))


class Pick(Base):
    def setUp(self):
        super().setUp()
        self.clear_sources()
        self.say("!뉴스 카테고리 추가 시험")

    def pick(self, count=5, **kw):
        return [a[0] for a in N.collect("시험", count, **kw)[0]]

    def test_three_sources_two_keywords(self):
        self.add_raw("시험", "https://a/", "https://b/")
        self.feeds = {"https://a/": articles("가", 5), "https://b/": articles("나", 5)}
        self.say("!뉴스 관심어 추가 시험 보안")
        self.feeds[N.search_url("보안")] = articles("키", 5)
        self.assertEqual(self.pick(), ["가1", "나1", "가2", "키1", "키2"])

    def test_quota(self):
        self.assertEqual(N.split_quota(5, True, True), (3, 2))
        self.assertEqual(N.split_quota(10, True, True), (6, 4))
        self.assertEqual(N.split_quota(1, True, True), (1, 0))
        self.assertEqual(N.split_quota(5, True, False), (5, 0))
        self.assertEqual(N.split_quota(5, False, True), (0, 5))

    def test_short_side_filled_by_other(self):
        self.add_raw("시험", "https://a/")
        self.feeds["https://a/"] = articles("가", 1)
        self.say("!뉴스 관심어 추가 시험 보안")
        self.feeds[N.search_url("보안")] = articles("키", 9)
        self.assertEqual(self.pick(), ["가1", "키1", "키2", "키3", "키4"])
        self.feeds["https://a/"] = articles("가", 9)
        self.feeds[N.search_url("보안")] = articles("키", 1)
        self.assertEqual(self.pick(), ["가1", "가2", "가3", "키1", "가4"])

    def test_empty_uses_category_name(self):
        self.feeds[N.search_url("시험")] = articles("이름", 2)
        self.assertEqual(self.pick(), ["이름1", "이름2"])

    def test_name_not_used_if_source_exists(self):
        self.add_raw("시험", "https://a/")
        self.feeds["https://a/"] = articles("가", 1)
        self.pick()
        self.assertEqual(self.calls, ["https://a/"])

    def test_same_title_once_ignoring_case(self):
        self.add_raw("시험", "https://a/", "https://b/")
        self.feeds = {"https://a/": [("Same  Title", "l1", "")], "https://b/": [("same title", "l2", "")]}
        self.assertEqual(self.pick(), ["Same  Title"])

    def test_excluded_press_exact(self):
        self.add_raw("시험", "https://a/")
        self.feeds["https://a/"] = [("가", "l", "뺄 신문"), ("나", "l", "뺄 신문사"), ("다", "l", "")]
        self.say("!뉴스 제외 추가 뺄 신문")
        self.assertEqual(self.pick(), ["나", "다"])

    def test_one_source_fails(self):
        self.add_raw("시험", "https://a/", "https://b/")
        self.feeds = {"https://a/": requests.ConnectionError(), "https://b/": articles("나", 2)}
        self.assertEqual(self.pick(), ["나1", "나2"])

    def test_all_fail(self):
        self.add_raw("시험", "https://a/")
        self.feeds["https://a/"] = requests.Timeout()
        with self.assertRaises(N.NewsError):
            self.pick()
        self.assertIn("가져오지 못했어요", self.say("!뉴스 시험"))

    def test_fail_mark_and_reset(self):
        self.add_raw("시험", "https://a/")
        self.feeds["https://a/"] = requests.Timeout()
        for _ in range(N.FAIL_MARK - 1):
            self.say("!뉴스 시험")
        self.assertNotIn("연속", self.say("!뉴스 출처"))
        self.say("!뉴스 시험")
        self.assertIn("연속 3회 실패", self.say("!뉴스 출처"))
        self.feeds["https://a/"] = articles("가", 1)
        self.say("!뉴스 시험")
        self.assertNotIn("연속", self.say("!뉴스 출처"))

    def test_keyword_rotation(self):
        for word in ("하나", "둘", "셋"):
            self.say(f"!뉴스 관심어 추가 시험 {word}")
            self.feeds[N.search_url(word)] = articles(word, 3)
        self.assertEqual(self.pick(2), ["하나1", "둘1"])
        self.assertEqual(self.pick(2, turn=1), ["둘1", "셋1"])
        self.assertEqual(self.pick(2, turn=3), ["하나1", "둘1"])

    def test_format(self):
        out = N.format_articles("it", [("제목", "https://x/", "언론사"), ("둘", "https://y/", "")], NOW)
        self.assertEqual(out, "📰 11월 3일 it 뉴스\n1. 제목 (언론사)\n   <https://x/>\n2. 둘\n   <https://y/>")


class View(Base):
    def setUp(self):
        super().setUp()
        self.clear_sources()
        self.add_raw("it", "https://it/")
        self.add_raw("일반", "https://gen/")
        self.feeds = {"https://it/": articles("아", 20), "https://gen/": articles("일", 20)}

    def test_default_five_each(self):
        out = self.say("!뉴스")
        self.assertIn("it 뉴스", out)
        self.assertIn("일반 뉴스", out)
        self.assertIn("아5", out)
        self.assertNotIn("아6", out)
        self.assertIn("일5", out)
        self.assertNotIn("일6", out)

    def test_total_split(self):
        out = self.say("!뉴스 20")
        self.assertIn("아10", out)
        self.assertNotIn("아11", out)
        self.assertIn("일10", out)
        out = self.say("!뉴스 7")
        self.assertIn("아4", out)
        self.assertNotIn("아5", out)
        self.assertIn("일3", out)
        self.assertNotIn("일4", out)
        out = self.say("!뉴스 1")
        self.assertIn("아1", out)
        self.assertNotIn("일반", out)

    def test_category_view(self):
        out = self.say("!뉴스 일반")
        self.assertIn("일5", out)
        self.assertNotIn("일6", out)
        self.assertNotIn("아1", out)
        self.assertIn("아10", self.say("!뉴스 IT 10"))
        self.assertIn("아1", self.say("!뉴스 기술"))

    def test_one_default_fails_other_shown(self):
        self.feeds["https://gen/"] = requests.Timeout()
        out = self.say("!뉴스")
        self.assertIn("아1", out)
        self.assertIn("⚠️ 뉴스를 가져오지 못했어요", out)

    def test_view_does_not_mark_sent(self):
        self.say("!뉴스 구독 it 08:00", channel=CHANNEL)
        self.say("!뉴스 it")
        self.now = datetime(2026, 11, 3, 8, 0)
        self.assertEqual(N.due_posts()[0]["titles"][0], "아1")


class Slot(unittest.TestCase):
    def test_window(self):
        times = ["07:00", "12:00"]
        self.assertEqual(N.due_slot(times, None, datetime(2026, 11, 3, 7, 0)), "2026-11-03 07:00")
        self.assertEqual(N.due_slot(times, None, datetime(2026, 11, 3, 7, 59)), "2026-11-03 07:00")
        self.assertIsNone(N.due_slot(times, None, datetime(2026, 11, 3, 8, 0)))  # 1시간이 지나면 건너뜀
        self.assertIsNone(N.due_slot(times, None, datetime(2026, 11, 3, 6, 59)))

    def test_once(self):
        self.assertIsNone(N.due_slot(["07:00"], "2026-11-03 07:00", datetime(2026, 11, 3, 7, 30)))
        self.assertEqual(N.due_slot(["07:00"], "2026-11-03 07:00", datetime(2026, 11, 4, 7, 0)), "2026-11-04 07:00")

    def test_latest_of_two_in_window(self):
        self.assertEqual(N.due_slot(["07:00", "07:30"], None, datetime(2026, 11, 3, 7, 40)), "2026-11-03 07:30")
        self.assertIsNone(N.due_slot(["07:00", "07:30"], "2026-11-03 07:30", datetime(2026, 11, 3, 7, 40)))

    def test_over_midnight(self):
        self.assertEqual(N.due_slot(["23:30"], None, datetime(2026, 11, 4, 0, 10)), "2026-11-03 23:30")

    def test_month_end_with_day_31(self):
        self.assertEqual(N.due_slot(["23:30"], None, datetime(2026, 12, 1, 0, 10)), "2026-11-30 23:30")

    def test_timezone_kept(self):
        from zoneinfo import ZoneInfo
        current = datetime(2026, 11, 3, 7, 10, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(N.due_slot(["07:00"], None, current), "2026-11-03 07:00")

    def test_parse_times(self):
        self.assertEqual(N.parse_times("12:00,7:00 7:00"), ["07:00", "12:00"])
        for bad in ("24:00", "7시", "07:60", ""):
            with self.assertRaises(N.NewsError, msg=bad):
                N.parse_times(bad)


class Posts(Base):
    def setUp(self):
        super().setUp()
        self.clear_sources()
        self.add_raw("it", "https://it/")
        self.feeds["https://it/"] = articles("아", 12)
        self.now = datetime(2026, 11, 3, 6, 0)
        self.say("!뉴스 구독 it 07:00,12:00", channel=CHANNEL)

    def at(self, hour, minute=0):
        self.now = datetime(2026, 11, 3, hour, minute)

    def test_not_yet(self):
        self.assertEqual(N.due_posts(), [])

    def test_no_times_never_posts(self):
        self.say("!뉴스 구독 일반", channel=CHANNEL)
        self.at(7)
        self.assertEqual([i["channel_id"] for i in N.due_posts()], ["500"])  # it 만
        self.assertEqual(len(N.due_posts()), 1)

    def test_count_per_channel(self):
        self.say("!뉴스 구독 it 개수 8", channel=CHANNEL)
        self.say("!뉴스 구독 it 07:00", channel={"id": "600", "name": "다른"})
        self.at(7)
        got = {i["channel_id"]: len(i["titles"]) for i in N.due_posts()}
        self.assertEqual(got, {"500": 8, "600": 5})

    def test_subscribe_does_not_post_passed_slot(self):
        self.at(7, 10)
        self.say("!뉴스 구독 일반 07:00", channel=CHANNEL)
        self.feeds = {"https://it/": []}
        self.assertEqual([p["slot"] for p in N.due_posts() if "일반" in p["text"]], [])

    def test_post_then_ack_excludes_sent(self):
        self.at(7)
        item = N.due_posts()[0]
        self.assertEqual((item["channel_id"], item["slot"]), ("500", "2026-11-03 07:00"))
        self.assertEqual(item["titles"], ["아1", "아2", "아3", "아4", "아5"])
        self.assertTrue(N.mark_posted(item["id"], item["slot"], item["titles"], item["turn"]))
        self.assertEqual(N.due_posts(), [])  # 같은 시각은 한 번만
        self.at(12)
        self.assertEqual(N.due_posts()[0]["titles"], ["아6", "아7", "아8", "아9", "아10"])

    def test_no_ack_means_retry_and_not_sent(self):
        self.at(7)
        first = N.due_posts()[0]
        self.assertEqual(N.due_posts()[0]["titles"], first["titles"])

    def test_no_new_articles_skips_slot(self):
        self.feeds["https://it/"] = articles("아", 2)
        self.at(7)
        item = N.due_posts()[0]
        N.mark_posted(item["id"], item["slot"], item["titles"], item["turn"])
        self.at(12)
        self.assertEqual(N.due_posts(), [])
        self.assertEqual(N._sub_rows()[0][5], "2026-11-03 12:00")

    def test_all_fail_retries_after_five_minutes(self):
        self.feeds["https://it/"] = requests.Timeout()
        self.at(7)
        self.assertEqual(N.due_posts(), [])
        self.assertNotEqual(N._sub_rows()[0][5], "2026-11-03 07:00")  # 올린 것으로 치지 않는다
        count = len(self.calls)
        self.at(7, 3)
        N.due_posts()
        self.assertEqual(len(self.calls), count)  # 5분 전에는 읽지 않는다
        self.feeds["https://it/"] = articles("아", 2)
        self.at(7, 5)
        self.assertEqual(N.due_posts()[0]["titles"], ["아1", "아2"])

    def test_keyword_turn_advances_only_on_ack(self):
        for word in ("하나", "둘", "셋"):
            self.say(f"!뉴스 관심어 추가 it {word}")
            self.feeds[N.search_url(word)] = articles(word, 5)
        self.at(7)
        item = N.due_posts()[0]
        self.assertEqual(item["titles"], ["아1", "아2", "아3", "하나1", "둘1"])
        self.assertEqual(N._sub_rows()[0][6], 0)
        N.mark_posted(item["id"], item["slot"], item["titles"], item["turn"])
        self.at(12)
        self.assertEqual(N.due_posts()[0]["titles"], ["아4", "아5", "아6", "셋1", "셋2"])

    def test_skip_slot_keeps_turn_and_articles(self):
        self.at(7)
        item = N.due_posts()[0]
        N.mark_posted(item["id"], item["slot"], [], None)
        self.assertEqual(N.due_posts(), [])
        self.at(12)
        self.assertEqual(N.due_posts()[0]["titles"][0], "아1")

    def test_sent_kept_per_channel(self):
        self.say("!뉴스 구독 it 07:00", channel={"id": "600", "name": "다른"})
        self.at(7)
        items = N.due_posts()
        self.assertEqual(len(items), 2)
        N.mark_posted(items[0]["id"], items[0]["slot"], items[0]["titles"], items[0]["turn"])
        self.at(12)
        self.assertEqual(N._sent_keys("600", "it"), set())

    def test_old_sent_removed(self):
        self.at(7)
        item = N.due_posts()[0]
        N.mark_posted(item["id"], item["slot"], item["titles"], item["turn"])
        self.now = self.now + N.SENT_KEEP + timedelta(days=1)
        N.mark_posted(item["id"], "2026-12-05 07:00", [], None)
        self.assertEqual(N._sent_keys("500", "it"), set())

    def test_gone_sub(self):
        self.assertFalse(N.mark_posted(9999, "2026-11-03 07:00", ["가"], 1))


class Send(unittest.TestCase):
    def run_news(self, send):
        acked = []

        async def fetch():
            return [{"id": 1, "channel_id": "5", "slot": "s", "text": "글", "titles": ["가"], "turn": 2}]

        async def ack(*args):
            acked.append(args)
        return asyncio.run(reminders.run_news(fetch, send, ack)), acked

    def test_sent(self):
        async def send(channel, text):
            pass
        self.assertEqual(self.run_news(send), ([1], [(1, "s", ["가"], 2)]))

    def test_failure_not_marked(self):
        async def send(channel, text):
            raise RuntimeError("끊김")
        self.assertEqual(self.run_news(send), ([], []))

    def test_give_up_marks_slot_only(self):
        async def send(channel, text):
            raise reminders.GiveUp("Forbidden")
        self.assertEqual(self.run_news(send), ([], [(1, "s", [], None)]))


class Briefing(Base):
    def test_no_news(self):
        with mock.patch.object(briefing.weather, "report_place", return_value="맑음"):
            out = briefing.build("owner")
        self.assertNotIn("뉴스", out)
        self.assertEqual(self.calls, [])


try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi 가 없어서 건너뜀")
class Api(Base):
    def setUp(self):
        super().setUp()
        from entrances import api_server
        from entrances.api import common
        self.common = common
        self.hosts = set(common.LOCAL_HOSTS)
        common.LOCAL_HOSTS.add("testclient")
        users.link("owner", "owner", "discord", "111")
        self.key = apikeys.create("owner", "디스코드 봇", kind="bot")
        self.client = TestClient(api_server.app)
        self.clear_sources()
        self.add_raw("it", "https://it/")
        self.feeds["https://it/"] = articles("아", 3)

    def tearDown(self):
        self.common.LOCAL_HOSTS.clear()
        self.common.LOCAL_HOSTS.update(self.hosts)
        super().tearDown()

    def post(self, path, body):
        return self.client.post(path, json=body, headers={"Authorization": f"Bearer {self.key}"})

    def message(self, text, channel=None):
        body = {"source": "discord", "external_id": "111", "text": text}
        if channel is not None:
            body["channel"] = channel
        return self.post("/internal/message", body).json()["reply"]

    def test_channel_passed(self):
        self.assertIn("디스코드 채널에서만", self.message("!뉴스 구독 it 07:30"))
        self.assertIn("디스코드 채널에서만", self.message("!뉴스 구독 it 07:30", {"name": "id 없음"}))
        self.assertIn("올려요", self.message("!뉴스 구독 it 07:30", CHANNEL))

    def test_due_and_ack(self):
        self.message("!뉴스 구독 it 07:30", CHANNEL)
        self.now = datetime(2026, 11, 3, 7, 30)
        item = self.post("/internal/news/due", {}).json()["items"][0]
        self.assertEqual(item["channel_id"], "500")
        self.assertIn("아1", item["text"])
        body = {"id": item["id"], "slot": item["slot"], "titles": item["titles"], "turn": item["turn"]}
        self.assertEqual(self.post("/internal/news/ack", body).json(), {"ok": True})
        self.assertEqual(self.post("/internal/news/due", {}).json()["items"], [])

    def test_ack_bad_input(self):
        for body in ({}, {"id": "1", "slot": "2026-11-03 07:00"}, {"id": 1, "slot": "내일"},
                     {"id": 1, "slot": "2026-11-03 07:00", "titles": [1]},
                     {"id": 1, "slot": "2026-11-03 07:00", "turn": "2"}):
            self.assertEqual(self.post("/internal/news/ack", body).status_code, 400, body)

    def test_needs_bot_key(self):
        self.assertIn(self.client.post("/internal/news/due", json={}).status_code, (401, 403))
        self.assertIn(self.client.post("/internal/news/ack", json={}).status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
