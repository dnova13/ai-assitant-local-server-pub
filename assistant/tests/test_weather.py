import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

import datetime, json

from core import classifier, config, users
from clients import llm
from features import weather
from core.assistant import Assistant

SAMPLE = {
    "current": {"time": "2026-10-17T16:45", "temperature_2m": 18.4, "apparent_temperature": 17.1,
                "relative_humidity_2m": 55, "precipitation": 0.0, "weather_code": 2,
                "wind_speed_10m": 9.7},
    "daily": {"time": ["2026-10-17", "2026-10-18"], "weather_code": [2, 61],
              "temperature_2m_max": [21.3, 17.0], "temperature_2m_min": [12.1, 13.4],
              "precipitation_probability_max": [10, 80], "precipitation_sum": [0.0, 6.2]},
}
REAL_FETCH, REAL_GEOCODE, REAL_TODAY = weather.fetch, weather.geocode, weather.today
REAL_ASK = classifier.ask_model


def fake_ask(text, model=None, timeout=120):
    """분류 모델 대신: '날씨'가 들어간 문장을 날씨로 보고 지역·날짜 글자를 돌려준다 (네트워크 없음)"""
    q = weather.parse_query(text)
    out = {"의도": "일반대화"} if q is None else {"의도": "날씨", "지역": q[0], "날짜": "내일" if q[1] == "내일" else None}
    return json.dumps(out, ensure_ascii=False), 0.0
REAL_OPENMETEO = weather.geocode_openmeteo


class Weather(unittest.TestCase):
    def setUp(self):
        self.calls, self.fetched, self.geo = [], [], []
        llm.chat = lambda *a, **k: self.calls.append(a) or "모델"
        weather.fetch = lambda lat=None, lon=None, timeout=10: self.fetched.append((lat, lon)) or SAMPLE
        weather.geocode = lambda name, timeout=10: self.geo.append(name) or (
            ("곡성기차마을 (전남 곡성군)", 35.28, 127.29) if name == "곡성" else None)
        config.WEATHER_LOOKUP = "both"
        # .env 의 실제 값(키, 기본 지역)에 시험이 좌우되지 않게 고정한다
        self.real_key, config.KAKAO_REST_API_KEY = config.KAKAO_REST_API_KEY, ""
        self.real_place = (config.WEATHER_PLACE, config.WEATHER_LAT, config.WEATHER_LON)
        config.WEATHER_PLACE, config.WEATHER_LAT, config.WEATHER_LON = "서울", 37.5665, 126.9780
        weather.today = lambda: datetime.date(2026, 10, 17)  # SAMPLE 의 첫 날
        classifier.ask_model = fake_ask
        users.set_setting("u", "기본위치", "서울")  # 지역 없는 질문은 사용자 기본 위치로
        self.bot = Assistant()

    def tearDown(self):
        weather.fetch, weather.geocode, weather.today = REAL_FETCH, REAL_GEOCODE, REAL_TODAY
        classifier.ask_model = REAL_ASK
        config.KAKAO_REST_API_KEY = self.real_key
        config.WEATHER_PLACE, config.WEATHER_LAT, config.WEATHER_LON = self.real_place

    # --- 형식 ---
    def test_format(self):
        out = self.bot.handle("u", "!날씨")
        self.assertIn("📍 서울 날씨", out)
        self.assertIn("구름 조금, 18°C", out)
        self.assertIn("내일(10/18): 약한 비", out)
        self.assertIn("☔ 내일", out)
        self.assertEqual(self.calls, [])  # 모델을 쓰지 않음

    def test_tomorrow_starts_from_tomorrow(self):
        out = weather.fmt(SAMPLE, "부산", "내일")
        self.assertNotIn("지금:", out)
        self.assertNotIn("오늘(", out)  # 지난 날이 아니어도 기준일 앞은 보여 주지 않음
        self.assertIn("10/19(월): 데이터가 없어요", out)  # SAMPLE 은 이틀치뿐

    def test_missing_values(self):
        data = {"current": dict(SAMPLE["current"], temperature_2m=None),
                "daily": dict(SAMPLE["daily"], precipitation_probability_max=[None, None])}
        out = weather.fmt(data, "서울")
        self.assertIn("?°C", out)
        self.assertNotIn("☔", out)

    # --- 문장 인식 ---
    def test_parse(self):
        cases = {
            "오늘 부산 날씨는?": ("부산", "오늘"),
            "서울 날씨 어때": ("서울", "오늘"),
            "내일 제주 날씨 알려줘": ("제주", "내일"),
            "부산날씨": ("부산", "오늘"),
            "날씨": (None, "오늘"),
            "내일 날씨는?": (None, "내일"),
            "해운대는 날씨 어때?": ("해운대", "오늘"),
        }
        for text, want in cases.items():
            self.assertEqual(weather.parse_query(text), want, text)
        self.assertIsNone(weather.parse_query("오늘 기분 어때"))

    def test_command_not_sent_to_model(self):
        out = self.bot.handle("u", "!날씨 부산")
        self.assertIn("📍 부산 날씨", out)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.geo, [])  # 표에 있으면 검색 안 함
        self.assertEqual(self.fetched[-1], weather.CITIES["부산"])

    def test_command_with_place(self):
        self.assertIn("📍 대구 날씨", self.bot.handle("u", "!날씨 대구"))

    # --- 지역 찾는 방식 ---
    def test_both_falls_back_to_geocode(self):
        out = self.bot.handle("u", "곡성 날씨")
        self.assertIn("곡성기차마을 (전남 곡성군)", out)
        self.assertEqual(self.geo, ["곡성"])

    def test_table_only(self):
        config.WEATHER_LOOKUP = "table"
        out = self.bot.handle("u", "곡성 날씨")
        self.assertIn("찾지 못했습니다", out)
        self.assertIn("바로 되는 지역", out)
        self.assertEqual(self.geo, [])

    def test_geo_only(self):
        config.WEATHER_LOOKUP = "geo"
        self.bot.handle("u", "부산 날씨")
        self.assertEqual(self.geo, ["부산"])  # 표를 쓰지 않음

    def test_not_found(self):
        out = self.bot.handle("u", "아무데나 날씨")
        self.assertIn("'아무데나' 지역을 찾지 못했습니다", out)
        self.assertEqual(self.calls, [])

    # --- 오류 ---
    def test_bad_response(self):
        weather.fetch = lambda lat=None, lon=None, timeout=10: {"error": True}
        self.assertIn("⚠️", self.bot.handle("u", "!날씨"))

    def test_network_error(self):
        def boom(lat=None, lon=None, timeout=10):
            raise weather.WeatherError("날씨 정보를 가져오지 못했습니다")
        weather.fetch = boom
        self.assertIn("⚠️ 날씨 정보를", self.bot.handle("u", "!날씨"))

    def test_unknown_code(self):
        self.assertEqual(weather.describe(42), "알 수 없음(코드 42)")




class FakeResp:
    def __init__(self, data=None, status=200):
        self.data, self.status = data, status

    def raise_for_status(self):
        if self.status >= 400:
            raise weather.requests.HTTPError(f"{self.status}")

    def json(self):
        return self.data


class KakaoGeocode(unittest.TestCase):
    """카카오 지명 검색 (네트워크 없이 requests.get 을 바꿔서 시험). 주소 검색 -> 키워드 검색 순서"""
    ADDR, KEYWORD = weather.KAKAO_ADDRESS_API, weather.KAKAO_API

    def setUp(self):
        self.real_get, self.real_key = weather.requests.get, config.KAKAO_REST_API_KEY
        self.sent = []
        config.KAKAO_REST_API_KEY = "TESTKEY"

    def tearDown(self):
        weather.requests.get, config.KAKAO_REST_API_KEY = self.real_get, self.real_key

    def fake(self, addr=None, keyword=None, error=None):
        """addr / keyword: 각 API가 돌려줄 FakeResp (없으면 결과 없음)"""
        def get(url, **kw):
            self.sent.append((url, kw))
            if isinstance(error, Exception):
                raise error
            return (addr if url == self.ADDR else keyword) or FakeResp({"documents": []})
        weather.requests.get = get

    ADDR_DOC = {"documents": [{"address_name": "광주광역시", "address_type": "REGION", "x": "126.8514", "y": "35.1601"}]}
    PLACE_DOC = {"documents": [{"place_name": "판교역 신분당선", "address_name": "경기 성남시 분당구 삼평동",
                                "x": "127.1112", "y": "37.3947"}]}

    def test_address_first(self):
        self.fake(addr=FakeResp(self.ADDR_DOC), keyword=FakeResp(self.PLACE_DOC))
        label, lat, lon = weather.geocode_kakao("광주")
        self.assertEqual((label, lat, lon), ("광주광역시", 35.1601, 126.8514))  # y=위도, x=경도
        self.assertEqual([u for u, _ in self.sent], [self.ADDR])  # 키워드 검색은 안 부름
        kw = self.sent[0][1]
        self.assertEqual(kw["headers"]["Authorization"], "KakaoAK TESTKEY")
        self.assertEqual(kw["params"]["query"], "광주")

    def test_keyword_when_no_address(self):
        self.fake(keyword=FakeResp(self.PLACE_DOC))
        label, lat, lon = weather.geocode_kakao("판교역")
        self.assertEqual(label, "판교역 신분당선 (경기 성남시)")
        self.assertEqual((lat, lon), (37.3947, 127.1112))
        self.assertEqual([u for u, _ in self.sent], [self.ADDR, self.KEYWORD])

    def test_no_result(self):
        self.fake()
        self.assertIsNone(weather.geocode_kakao("없는곳"))

    def test_error_hides_key(self):
        self.fake(addr=FakeResp({}, status=401), keyword=FakeResp({}, status=401))
        with self.assertRaises(weather.WeatherError) as cm:
            weather.geocode_kakao("판교")
        self.assertNotIn("TESTKEY", str(cm.exception))

    def test_bad_response(self):
        self.fake(addr=FakeResp({"documents": [{"address_name": "x"}]}))
        with self.assertRaises(weather.WeatherError):
            weather.geocode_kakao("판교")

    def test_dispatch_no_result_does_not_fall_back(self):
        self.fake()
        weather.geocode_openmeteo = lambda *a, **k: self.fail("카카오가 결과 없음이면 Open-Meteo로 넘어가면 안 됨")
        try:
            self.assertIsNone(weather.geocode("없는곳"))
        finally:
            weather.geocode_openmeteo = REAL_OPENMETEO

    def test_dispatch_falls_back_on_error(self):
        self.fake(addr=FakeResp({}, status=429))
        weather.geocode_openmeteo = lambda name, timeout=10: ("대체", 1.0, 2.0)
        try:
            self.assertEqual(weather.geocode("판교"), ("대체", 1.0, 2.0))
        finally:
            weather.geocode_openmeteo = REAL_OPENMETEO

    def test_dispatch_without_key_uses_openmeteo(self):
        config.KAKAO_REST_API_KEY = ""
        self.fake(error=AssertionError("카카오를 부르면 안 됨"))
        weather.geocode_openmeteo = lambda name, timeout=10: ("검색", 3.0, 4.0)
        try:
            self.assertEqual(weather.geocode("판교"), ("검색", 3.0, 4.0))
        finally:
            weather.geocode_openmeteo = REAL_OPENMETEO


class ResolveOrder(unittest.TestCase):
    """both 순서: 카카오 -> (오류·한도 초과 시) 표 -> Open-Meteo 검색"""

    def setUp(self):
        self.real = (config.KAKAO_REST_API_KEY, config.WEATHER_LOOKUP, weather.geocode_kakao,
                     weather.geocode_openmeteo, weather.geocode)
        config.KAKAO_REST_API_KEY, config.WEATHER_LOOKUP = "TESTKEY", "both"
        self.log = []
        weather.geocode = REAL_GEOCODE
        weather.geocode_openmeteo = lambda n, timeout=10: self.log.append("om") or ("OM " + n, 1.0, 2.0)

    def tearDown(self):
        (config.KAKAO_REST_API_KEY, config.WEATHER_LOOKUP, weather.geocode_kakao,
         weather.geocode_openmeteo, weather.geocode) = self.real

    def kakao(self, result):
        def f(name, timeout=10):
            self.log.append("kakao")
            if isinstance(result, Exception):
                raise result
            return result
        weather.geocode_kakao = f

    def test_kakao_first_even_if_in_table(self):
        self.kakao(("해운대해수욕장 (부산 해운대구)", 35.15, 129.16))
        self.assertEqual(weather.resolve("해운대")[0], "해운대해수욕장 (부산 해운대구)")
        self.assertEqual(self.log, ["kakao"])

    def test_kakao_error_uses_table(self):
        self.kakao(weather.WeatherError("x"))
        self.assertEqual(weather.resolve("해운대")[0], "해운대")
        self.assertEqual(self.log, ["kakao"])  # Open-Meteo 안 부름

    def test_kakao_error_not_in_table_uses_openmeteo(self):
        self.kakao(weather.WeatherError("x"))
        self.assertEqual(weather.resolve("곡성")[0], "OM 곡성")
        self.assertEqual(self.log, ["kakao", "om"])

    def test_kakao_no_result_in_table(self):
        self.kakao(None)
        self.assertEqual(weather.resolve("강남구")[0], "강남구")
        self.assertEqual(self.log, ["kakao"])

    def test_kakao_no_result_not_in_table_stops(self):
        self.kakao(None)
        self.assertIsNone(weather.resolve("없는곳"))
        self.assertEqual(self.log, ["kakao"])  # Open-Meteo로 넘어가지 않음

    def test_no_key_table_then_openmeteo(self):
        config.KAKAO_REST_API_KEY = ""
        self.kakao(AssertionError("카카오 호출 금지"))
        self.assertEqual(weather.resolve("부산")[0], "부산")
        self.assertEqual(weather.resolve("곡성")[0], "OM 곡성")

    def test_gu_names(self):
        self.assertEqual(weather.lookup_table("송파구")[0], "송파구")
        self.assertEqual(weather.lookup_table("강남"), ("강남", *weather.CITIES["강남구"]))
        self.assertIsNone(weather.lookup_table("중구"))  # 이름이 겹치는 구는 넣지 않음


class GuTable(unittest.TestCase):
    def test_parse_city_plus_gu(self):
        self.assertEqual(weather.parse_query("부산 중구 날씨"), ("부산 중구", "오늘"))
        self.assertEqual(weather.parse_query("내일 서울 강남구 날씨는?"), ("서울 강남구", "내일"))
        self.assertEqual(weather.parse_query("해운대구 날씨"), ("해운대구", "오늘"))
        self.assertEqual(weather.parse_query("오늘 부산 날씨 어때"), ("부산", "오늘"))

    def test_duplicate_names_are_distinct(self):
        a, b = weather.lookup_table("부산 중구"), weather.lookup_table("대구 중구")
        self.assertNotEqual(a[1:], b[1:])
        self.assertIsNone(weather.lookup_table("중구"))
        self.assertIsNone(weather.lookup_table("서구"))

    def test_city_plus_gu_lookup(self):
        self.assertEqual(weather.lookup_table("부산 해운대구")[1:], weather.CITIES["해운대구"])
        self.assertEqual(weather.lookup_table("서울 강남구")[0], "서울 강남구")
        self.assertIsNone(weather.lookup_table("부산 없는구"))

    def test_all_coordinates_in_korea(self):
        for name, (lat, lon) in weather.CITIES.items():
            self.assertTrue(33.0 < lat < 38.7 and 124.5 < lon < 131.0, name)


class SameNameRules(unittest.TestCase):
    def setUp(self):
        self.real = (config.KAKAO_REST_API_KEY, config.WEATHER_LOOKUP, weather.geocode_kakao,
                     weather.geocode_openmeteo, weather.fetch)
        config.KAKAO_REST_API_KEY, config.WEATHER_LOOKUP = "TESTKEY", "both"
        self.asked = []
        weather.geocode_kakao = lambda n, timeout=10: self.asked.append(n) or (f"카카오:{n}", 1.0, 2.0)
        weather.geocode_openmeteo = lambda n, timeout=10: self.fail("Open-Meteo 호출 금지")
        weather.fetch = lambda lat=None, lon=None, timeout=10: SAMPLE

    def tearDown(self):
        (config.KAKAO_REST_API_KEY, config.WEATHER_LOOKUP, weather.geocode_kakao,
         weather.geocode_openmeteo, weather.fetch) = self.real

    def test_metro_name_uses_table_first(self):
        self.assertEqual(weather.resolve("광주")[0], "광주")
        self.assertEqual(weather.resolve("광주")[1:], weather.CITIES["광주"])
        self.assertEqual(self.asked, [])  # 카카오를 부르지 않음

    def test_gyeonggi_gwangju_uses_kakao(self):
        self.assertEqual(weather.parse_query("경기 광주 날씨"), ("경기 광주", "오늘"))
        self.assertEqual(weather.resolve("경기 광주")[0], "카카오:경기 광주")
        self.assertEqual(weather.resolve("광주시")[0], "카카오:광주시")
        self.assertEqual(self.asked, ["경기 광주", "광주시"])

    def test_province_plus_name_parse(self):
        self.assertEqual(weather.parse_query("강원 고성 날씨 어떻게 돼?"), ("강원 고성", "오늘"))
        self.assertEqual(weather.parse_query("내일 경남 고성군 날씨"), ("경남 고성군", "내일"))
        self.assertEqual(weather.parse_query("마포 성산동 날씨"), ("마포 성산동", "오늘"))
        self.assertEqual(weather.parse_query("제주 날씨 어때"), ("제주", "오늘"))  # 한 단어는 그대로

    def test_ambiguous_names_list_candidates(self):
        out = weather.report("고성 날씨")
        self.assertIn("여러 곳", out)
        self.assertIn("강원 고성군", out)
        self.assertIn("경남 고성군", out)
        self.assertEqual(self.asked, [])  # 카카오·날씨 조회 없음

    def test_ambiguous_with_province_is_not_blocked(self):
        out = weather.report("강원 고성군 날씨")
        self.assertIn("카카오:강원 고성군", out)
        self.assertIn("(출처: Open-Meteo)", out)


if __name__ == "__main__":
    unittest.main()
