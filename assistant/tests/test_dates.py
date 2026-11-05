"""날씨 날짜 규칙: 기준일부터 3일, 지난 날도 조회, 데이터 없는 날은 '데이터가 없어요' (네트워크 없음)"""
import datetime, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from features import weather

TODAY = datetime.date(2026, 10, 20)
FIRST, DAYS = datetime.date(2026, 10, 10), 27   # 10/10 ~ 11/05 (지난 10일 + 오늘 + 예보)


def data(missing=()):
    days = [FIRST + datetime.timedelta(days=i) for i in range(DAYS)]
    code = [None if d.isoformat() in missing else 2 for d in days]
    n = len(days)
    return {
        "current": {"time": "2026-10-20T16:45", "temperature_2m": 18.0, "apparent_temperature": 17.0,
                    "relative_humidity_2m": 55, "precipitation": 0.0, "weather_code": 2, "wind_speed_10m": 9.0},
        "daily": {"time": [d.isoformat() for d in days], "weather_code": code,
                  "temperature_2m_max": [20] * n, "temperature_2m_min": [10] * n,
                  "precipitation_probability_max": [10] * n, "precipitation_sum": [0.0] * n},
    }


class Dates(unittest.TestCase):
    def setUp(self):
        self.real = weather.today
        weather.today = lambda: TODAY

    def tearDown(self):
        weather.today = self.real

    def lines(self, day, **kw):
        return weather.fmt(data(**kw), "부산", day).splitlines()

    def test_today_shows_now_plus_three_days(self):
        out = "\n".join(self.lines("오늘"))
        self.assertIn("지금:", out)
        for label in ("오늘(10/20)", "내일(10/21)", "10/22(목)"):
            self.assertIn(label, out)
        self.assertNotIn("(10/23)", out)

    def test_tomorrow_starts_tomorrow(self):
        out = "\n".join(self.lines("내일"))
        self.assertNotIn("지금:", out)
        self.assertNotIn("오늘(", out)
        self.assertEqual([x for x in out.splitlines() if "(10/" in x][0][:9], "내일(10/21)")
        self.assertIn("10/22(목)", out)
        self.assertIn("10/23(금)", out)

    def test_day_after_tomorrow(self):
        out = "\n".join(self.lines("모레"))
        self.assertLess(out.index("10/22(목)"), out.index("10/23(금)"))
        self.assertIn("10/24(토)", out)
        self.assertNotIn("모레(", out)
        self.assertNotIn("내일(", out)

    def test_specific_date_shows_that_day_and_next_two(self):
        out = "\n".join(self.lines("2026-10-30"))
        self.assertNotIn("지금:", out)
        for label in ("10/30(금)", "10/31(토)", "11/01(일)"):
            self.assertIn(label, out)
        self.assertNotIn("(11/02)", out)

    def test_past_date_is_shown(self):
        out = "\n".join(self.lines("어제"))
        self.assertIn("10/19(월)", out)
        self.assertIn("오늘(10/20)", out)
        out = "\n".join(self.lines("2026-10-12"))
        self.assertIn("10/12(월)", out)

    def test_no_data_marked(self):
        out = "\n".join(self.lines("2026-11-04"))    # 11/04, 11/05 만 있고 11/06 은 없음
        self.assertIn("11/04(수)", out)
        self.assertIn("11/06(금): 데이터가 없어요", out)
        self.assertIn("확인할 수 있는 날짜: 10/10 ~ 11/05", out)

    def test_base_day_without_data(self):
        out = "\n".join(self.lines("2026-12-25"))
        self.assertEqual(out.count("데이터가 없어요"), 3)

    def test_null_value_counts_as_no_data(self):
        out = "\n".join(self.lines("내일", missing=("2026-10-22",)))
        self.assertIn("10/22(목): 데이터가 없어요", out)

    def test_bad_day(self):
        with self.assertRaises(weather.WeatherError):
            weather.fmt(data(), "부산", None)


class ParseQuery(unittest.TestCase):
    def setUp(self):
        self.real = weather.today
        weather.today = lambda: TODAY

    def tearDown(self):
        weather.today = self.real

    def test_date_words_are_removed_from_place(self):
        self.assertEqual(weather.parse_query("모레 부산 날씨"), ("부산", "모레"))
        self.assertEqual(weather.parse_query("내일모레 서울 날씨"), ("서울", "모레"))
        self.assertEqual(weather.parse_query("부산 10월 30일 날씨"), ("부산", "2026-10-30"))
        self.assertEqual(weather.parse_query("서울 28일 날씨"), ("서울", "2026-10-28"))
        self.assertEqual(weather.parse_query("10/30 대구 날씨"), ("대구", "2026-10-30"))
        self.assertEqual(weather.parse_query("어제 광주 날씨"), ("광주", "어제"))
        self.assertEqual(weather.parse_query("낼 부산 날씨"), ("부산", "내일"))
        self.assertEqual(weather.parse_query("부산 낼 날씨 어때"), ("부산", "내일"))
        self.assertEqual(weather.parse_query("낼모레 대구 날씨"), ("대구", "모레"))

    def test_nael_inside_a_word_is_not_tomorrow(self):
        self.assertEqual(weather.parse_query("부산 날씨 보낼게")[1], "오늘")

    def test_invalid_date_is_none(self):
        self.assertEqual(weather.parse_query("부산 2월 30일 날씨"), ("부산", None))

    def test_days_later_is_unsupported_and_not_a_place(self):
        # '3일 뒤'는 지원하지 않는 날짜 말(None → 안내). '3일'이 지역으로 읽히면 안 된다.
        for text in ("3일 뒤 부산 날씨", "부산 3일 뒤 날씨", "날씨 3일 후 부산", "부산 날씨 3일 전"):
            self.assertEqual(weather.parse_query(text), ("부산", None), text)


if __name__ == "__main__":
    unittest.main()
