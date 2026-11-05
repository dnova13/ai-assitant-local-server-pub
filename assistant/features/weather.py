"""날씨 조회 (Open-Meteo, API 키 불필요).
값은 코드가 API에서 가져오고, 문장도 코드가 만든다. 모델은 쓰지 않는다 (날씨를 지어내는 문제 방지).

지역 찾기 순서(기본 both): 1) 카카오 주소 검색 -> 없으면 카카오 키워드 검색(키가 있을 때)  2) 카카오 오류·한도 초과 시 아래 CITIES 표  3) 표에도 없으면 Open-Meteo 검색(한국만)  4) 못 찾으면 안내
키가 없으면 표 -> Open-Meteo 검색 순서
문장 인식: "오늘 부산 날씨는?", "내일 제주 날씨 알려줘"처럼 '날씨'가 들어간 문장에서 지역·날짜를 코드가 뽑는다."""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from core import config

API = "https://api.open-meteo.com/v1/forecast"
GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
KAKAO_API = "https://dapi.kakao.com/v2/local/search/keyword.json"
KAKAO_ADDRESS_API = "https://dapi.kakao.com/v2/local/search/address.json"

# 자주 볼 도시 (대략 시청 좌표). 여기 있으면 지명 검색을 하지 않는다. 필요한 도시는 추가하면 된다.
CITIES = {
    "서울": (37.5665, 126.9780), "부산": (35.1796, 129.0756), "대구": (35.8714, 128.6014),
    "인천": (37.4563, 126.7052), "광주": (35.1595, 126.8526), "대전": (36.3504, 127.3845),
    "울산": (35.5384, 129.3114), "세종": (36.4800, 127.2890), "수원": (37.2636, 127.0286),
    "성남": (37.4200, 127.1265), "고양": (37.6584, 126.8320), "용인": (37.2411, 127.1776),
    "춘천": (37.8813, 127.7298), "강릉": (37.7519, 128.8761), "청주": (36.6424, 127.4890),
    "천안": (36.8151, 127.1139), "전주": (35.8242, 127.1480), "목포": (34.8118, 126.3922),
    "여수": (34.7604, 127.6622), "포항": (36.0190, 129.3435), "안동": (36.5684, 128.7294),
    "창원": (35.2280, 128.6811), "제주": (33.4996, 126.5312), "서귀포": (33.2541, 126.5600),
    # 자주 찾는 동네 (대략 좌표) [확인 필요: 카카오 결과와 비교해서 맞는지 볼 것]
    "해운대": (35.1631, 129.1635), "판교": (37.3947, 127.1112), "분당": (37.3595, 127.1052),
    # 서울 25개 구 (구청 부근 대략 좌표). 이름이 겹치는 구(중구, 강서구)는 넣지 않았다
    "강남구": (37.5172, 127.0473), "강동구": (37.5301, 127.1238), "강북구": (37.6396, 127.0257),
    "관악구": (37.4784, 126.9516), "광진구": (37.5385, 127.0824), "구로구": (37.4954, 126.8874),
    "금천구": (37.4519, 126.8955), "노원구": (37.6542, 127.0568), "도봉구": (37.6688, 127.0471),
    "동대문구": (37.5744, 127.0396), "동작구": (37.5124, 126.9393), "마포구": (37.5663, 126.9019),
    "서대문구": (37.5791, 126.9368), "서초구": (37.4837, 127.0324), "성동구": (37.5634, 127.0369),
    "성북구": (37.5894, 127.0167), "송파구": (37.5145, 127.1059), "양천구": (37.5169, 126.8664),
    "영등포구": (37.5264, 126.8963), "용산구": (37.5324, 126.9906), "은평구": (37.6027, 126.9291),
    "종로구": (37.5735, 126.9790), "중랑구": (37.6063, 127.0926),
}
CITIES["강남"] = CITIES["강남구"]  # '강남'이라고만 써도 서울 강남구

# 전국 광역시·큰 도시의 구·군 (구청 부근 대략 좌표) [확인 필요: python -m features.weather --check-table 로 카카오와 비교]
# 이름이 겹치는 구는 "부산 중구"처럼 도시 이름을 붙인 것만 넣었다 (문장에서도 "부산 중구 날씨"처럼 쓴다)
GU = {
    "서울 중구": (37.5641, 126.9979), "서울 강서구": (37.5509, 126.8495),
    # 부산
    "부산 중구": (35.1063, 129.0324), "부산 서구": (35.0977, 129.0240), "부산 동구": (35.1294, 129.0454),
    "부산 남구": (35.1365, 129.0843), "부산 북구": (35.1972, 128.9903), "부산 강서구": (35.2123, 128.9805),
    "영도구": (35.0912, 129.0679), "부산진구": (35.1629, 129.0532), "동래구": (35.2049, 129.0837),
    "해운대구": (35.1631, 129.1635), "사하구": (35.1045, 128.9748), "금정구": (35.2430, 129.0925),
    "연제구": (35.1760, 129.0799), "수영구": (35.1454, 129.1131), "사상구": (35.1527, 128.9910),
    "기장군": (35.2446, 129.2222),
    # 대구
    "대구 중구": (35.8693, 128.6062), "대구 동구": (35.8866, 128.6355), "대구 서구": (35.8719, 128.5592),
    "대구 남구": (35.8460, 128.5974), "대구 북구": (35.8858, 128.5828),
    "수성구": (35.8582, 128.6306), "달서구": (35.8299, 128.5329), "달성군": (35.7746, 128.4314),
    # 인천
    "인천 중구": (37.4738, 126.6217), "인천 동구": (37.4737, 126.6432), "인천 서구": (37.5453, 126.6760),
    "미추홀구": (37.4635, 126.6505), "연수구": (37.4102, 126.6783), "남동구": (37.4470, 126.7314),
    "부평구": (37.5071, 126.7219), "계양구": (37.5372, 126.7378), "강화군": (37.7469, 126.4878),
    # 광주
    "광주 동구": (35.1460, 126.9232), "광주 서구": (35.1519, 126.8895), "광주 남구": (35.1330, 126.9026),
    "광주 북구": (35.1741, 126.9120), "광산구": (35.1396, 126.7938),
    # 대전
    "대전 동구": (36.3120, 127.4548), "대전 중구": (36.3255, 127.4214), "대전 서구": (36.3555, 127.3838),
    "유성구": (36.3622, 127.3561), "대덕구": (36.3466, 127.4150),
    # 울산
    "울산 중구": (35.5695, 129.3327), "울산 남구": (35.5438, 129.3301), "울산 동구": (35.5049, 129.4167),
    "울산 북구": (35.5827, 129.3614), "울주군": (35.5224, 129.1544),
    # 구가 있는 일반 시
    "장안구": (37.3045, 127.0101), "권선구": (37.2575, 126.9716), "팔달구": (37.2827, 127.0200),
    "영통구": (37.2596, 127.0467), "수정구": (37.4504, 127.1457), "중원구": (37.4306, 127.1372),
    "분당구": (37.3595, 127.1052), "만안구": (37.3864, 126.9327), "동안구": (37.3925, 126.9515),
    "상록구": (37.3010, 126.8468), "단원구": (37.3199, 126.8338), "덕양구": (37.6377, 126.8323),
    "일산동구": (37.6588, 126.7745), "일산서구": (37.6757, 126.7503), "처인구": (37.2342, 127.2010),
    "기흥구": (37.2804, 127.1146), "수지구": (37.3220, 127.0977), "상당구": (36.5920, 127.5030),
    "서원구": (36.6370, 127.4710), "흥덕구": (36.6420, 127.4300), "청원구": (36.6560, 127.4870),
    "동남구": (36.8066, 127.1540), "서북구": (36.8800, 127.1250), "완산구": (35.8120, 127.1195),
    "덕진구": (35.8430, 127.1220), "포항 남구": (36.0090, 129.3600), "포항 북구": (36.0400, 129.3700),
    "의창구": (35.2540, 128.6410), "성산구": (35.1980, 128.7020), "마산합포구": (35.1970, 128.5680),
    "마산회원구": (35.2180, 128.5800), "진해구": (35.1490, 128.7050),
}
CITIES.update(GU)

# 문장에서 지역 이름이 아닌 말 (빼고 남은 단어를 지역으로 본다)
STOPWORDS = {"오늘", "내일", "지금", "현재", "좀", "알려줘", "알려", "줘", "어때", "어떄", "어떻게",
             "돼", "되나", "될까", "궁금해", "뭐야", "어떤가", "어떨까", "봐줘", "확인"}
JOSA = ("에서", "에는", "은", "는", "이", "가", "의", "에", "도")

# 광역시·특별시 이름은 표를 먼저 쓴다 ("광주" = 광주광역시. 경기 광주시는 "경기 광주" 또는 "광주시"로 쓴다)
PREFER_TABLE = {"서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "제주"}
# 뒤에 지역 이름이 붙으면 한 지역으로 합쳐서 읽는 도 이름 ("경기 광주", "강원 고성군")
PROVINCES = {"경기", "경기도", "강원", "강원도", "강원특별자치도", "충북", "충청북도", "충남", "충청남도",
             "경북", "경상북도", "경남", "경상남도", "전북", "전라북도", "전북특별자치도", "전남", "전라남도",
             "제주", "제주도", "제주특별자치도"}
AREA_SUFFIX = ("구", "군", "시", "읍", "면", "동", "리")
# 이름이 여러 곳인 지역: 날씨 대신 후보를 안내한다 (써 보다가 겹치는 이름을 발견하면 한 줄씩 추가)
AMBIGUOUS = {
    "고성": ["강원 고성군", "경남 고성군"],
    "고성군": ["강원 고성군", "경남 고성군"],
    "성산": ["제주 성산읍", "창원 성산구", "마포 성산동"],
}

# WMO 날씨 코드 -> 한국어
CODES = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림",
    45: "안개", 48: "짙은 안개(서리)",
    51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비",
    56: "약한 어는 이슬비", 57: "강한 어는 이슬비",
    61: "약한 비", 63: "비", 65: "강한 비",
    66: "약한 어는 비", 67: "강한 어는 비",
    71: "약한 눈", 73: "눈", 75: "강한 눈", 77: "싸락눈",
    80: "약한 소나기", 81: "소나기", 82: "강한 소나기",
    85: "약한 눈 소나기", 86: "강한 눈 소나기",
    95: "뇌우", 96: "뇌우(약한 우박)", 99: "뇌우(강한 우박)",
}
RAIN_ALERT = 60  # 강수확률(%)이 이 이상이면 우산 안내


class WeatherError(Exception):
    pass


# --- 날짜: 기준일부터 SHOW_DAYS 일을 보여 준다. 지난 날도 PAST_DAYS 일 전까지 조회한다 ---
PAST_DAYS = 92        # [확인 필요] Open-Meteo past_days 한도
FORECAST_DAYS = 16    # Open-Meteo 예보 한도
SHOW_DAYS = 3
WEEKDAYS = "월화수목금토일"
RELATIVE = {"어제": -1, "오늘": 0, "내일": 1, "낼": 1, "모레": 2, "내일모레": 2, "낼모레": 2, "그제": -2, "그저께": -2}
UNSUPPORTED_DATE_WORDS = ("주말", "다음", "이번", "요일", "다다음")  # 날짜로 바꿀 수 없는 말
# 날짜 말 모음: 내일모레·모레·내일(낼)·오늘·어제·그제 / 10월 23일 / 10/23 / 23일 ('3일 뒤' 같은 말은 제외)
DATE_PATTERN = (r"(?<![가-힣])낼\s*모레|내일\s*모레|모레|내일|(?<![가-힣])낼|오늘|어제|그저께|그제|"
                r"\d{1,2}\s*월\s*\d{1,2}\s*일?|(?<!\d)\d{1,2}\s*/\s*\d{1,2}(?!\d)|"
                r"(?<![\d월])\d{1,2}\s*일(?!\s*(?:뒤|후|전|간|동안))")


# "3일 뒤"·"3일 후"·"3일 전"은 날짜로 바꾸지 않는다(지원 안 함). 다만 지역 이름으로 읽히지 않게 따로 걸러 낸다.
RELATIVE_DAYS_PATTERN = r"\d{1,2}\s*일\s*(?:뒤|후|전)"


def today():
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def _day_value(d, t):
    """날짜를 '어제/오늘/내일/모레' 또는 'YYYY-MM-DD' 글자로 돌려준다."""
    off = (d - t).days
    return {-1: "어제", 0: "오늘", 1: "내일", 2: "모레"}.get(off, d.isoformat())


def normalize_day(phrase, now=None):
    """날짜 글자(모델이 뽑았거나 문장에 적힌 것)를 코드가 판정한다.
    '어제'/'오늘'/'내일'/'모레' 또는 'YYYY-MM-DD'. 날짜로 바꿀 수 없으면 None. 날짜 말이 없으면(null) 오늘."""
    if phrase is None:
        return "오늘"
    p = str(phrase).strip()
    if p in ("", "없음", "null", "None"):
        return "오늘"
    if any(w in p for w in UNSUPPORTED_DATE_WORDS):
        return None
    t = now or today()
    m = re.search(DATE_PATTERN, p)
    if not m:
        return "오늘" if any(w in p for w in ("지금", "현재")) else None
    g = re.sub(r"\s+", "", m.group(0))
    try:
        if g in RELATIVE:
            return _day_value(t + timedelta(days=RELATIVE[g]), t)
        md = re.fullmatch(r"(\d{1,2})월(\d{1,2})일?", g) or re.fullmatch(r"(\d{1,2})/(\d{1,2})", g)
        if md:
            mo, d = int(md.group(1)), int(md.group(2))
            cand = date(t.year, mo, d)
            if cand < t - timedelta(days=PAST_DAYS):
                cand = date(t.year + 1, mo, d)  # 너무 오래 전이면 내년으로 본다
            return _day_value(cand, t)
        return _day_value(date(t.year, t.month, int(re.fullmatch(r"(\d{1,2})일", g).group(1))), t)
    except ValueError:  # 2월 30일 같은 없는 날짜
        return None


def to_date(day, now=None):
    """'오늘'/'내일'/'모레'/'YYYY-MM-DD' 또는 date 를 date 로."""
    if isinstance(day, date):
        return day
    t = now or today()
    if day in RELATIVE:
        return t + timedelta(days=RELATIVE[day])
    try:
        return date.fromisoformat(str(day))
    except ValueError:
        raise WeatherError("날짜를 알 수 없어요. 날짜로 말씀해 주세요 (예: 10월 23일).")


def describe(code):
    return CODES.get(code, f"알 수 없음(코드 {code})")


def parse_query(text):
    """'날씨'가 들어간 문장에서 (지역 이름 또는 None, 날짜 글자)를 뽑는다. 날씨 문장이 아니면 None.
    날짜 글자는 normalize_day 의 결과('오늘'/'내일'/'모레'/'YYYY-MM-DD', 바꿀 수 없으면 None)."""
    if "날씨" not in text:
        return None
    unsupported = re.search(RELATIVE_DAYS_PATTERN, text)
    if unsupported:
        text = re.sub(RELATIVE_DAYS_PATTERN, " ", text)  # '3일'이 지역으로 읽히지 않게 지운다
    m = re.search(DATE_PATTERN, text)
    day = None if unsupported else (normalize_day(m.group(0)) if m else "오늘")
    words = []
    cleaned = re.sub(r"날씨(는|가|도|좀|를|은)?", " ", re.sub(DATE_PATTERN, " ", text))
    for word in re.sub(r"[?!.,~]", " ", cleaned).split():
        if word in STOPWORDS or word in JOSA:
            continue
        for j in JOSA:
            if word.endswith(j) and len(word) > len(j) + 1:
                word = word[: -len(j)]
                break
        if word and word not in STOPWORDS:
            words.append(word)
    place = words[0] if words else None
    # "경기 광주", "부산 중구", "서울 강남구"처럼 두 단어를 합쳐 한 지역으로 본다
    # (앞이 도 이름이거나, 뒤가 구·군·시·읍·면·동·리로 끝나는 경우)
    if place and len(words) > 1 and not place.endswith(AREA_SUFFIX):
        if place in PROVINCES or words[1].endswith(AREA_SUFFIX):
            place = f"{place} {words[1]}"
    return place, day


def geocode_openmeteo(name, timeout=10):
    """Open-Meteo 지명 검색 (한국만). 찾으면 (표시 이름, 위도, 경도), 없으면 None."""
    try:
        r = requests.get(GEO_API, params={"name": name, "count": 1, "language": "ko",
                                         "countryCode": "KR"}, timeout=timeout)
        r.raise_for_status()
        results = r.json().get("results") or []
    except requests.RequestException as e:
        raise WeatherError(f"지역을 찾는 중 오류가 났습니다 ({type(e).__name__}).")
    except ValueError:
        raise WeatherError("지역 검색 응답을 해석하지 못했습니다.")
    if not results:
        return None
    g = results[0]
    label = g.get("name", name)
    if g.get("admin1") and g["admin1"] != label:
        label = f"{label} ({g['admin1']})"
    return label, g["latitude"], g["longitude"]


def _kakao_docs(url, name, timeout):
    """카카오 로컬 API 한 번 호출 -> documents 목록. 오류는 WeatherError (문구에 키가 들어가지 않게 예외 종류만 적는다)."""
    try:
        r = requests.get(url, params={"query": name, "size": 1},
                         headers={"Authorization": f"KakaoAK {config.KAKAO_REST_API_KEY}"}, timeout=timeout)
        r.raise_for_status()
        return r.json().get("documents") or []
    except requests.RequestException as e:
        raise WeatherError(f"카카오 지역 검색 오류 ({type(e).__name__}).")
    except (ValueError, AttributeError):
        raise WeatherError("카카오 지역 검색 응답을 해석하지 못했습니다.")


def geocode_kakao(name, timeout=10):
    """카카오 로컬 검색. 찾으면 (표시 이름, 위도, 경도), 없으면 None.
    1) 주소 검색(시·구·동 같은 행정구역 이름에 맞음)  2) 결과가 없으면 키워드 장소 검색(역, 해수욕장 같은 장소 이름).
    키 오류·한도 초과·접속 오류는 WeatherError."""
    try:
        docs = _kakao_docs(KAKAO_ADDRESS_API, name, timeout)
        if docs:
            d = docs[0]
            return d["address_name"], float(d["y"]), float(d["x"])  # y=위도, x=경도
        docs = _kakao_docs(KAKAO_API, name, timeout)
        if docs:
            d = docs[0]
            label = d["place_name"]
            area = " ".join((d.get("address_name") or "").split()[:2])
            return (f"{label} ({area})" if area else label), float(d["y"]), float(d["x"])
        return None
    except (ValueError, KeyError, TypeError):
        raise WeatherError("카카오 지역 검색 응답을 해석하지 못했습니다.")


def geocode(name, timeout=10):
    """지명 -> (표시 이름, 위도, 경도) 또는 None. 카카오 키가 있으면 카카오 먼저.
    카카오가 '결과 없음'이라고 답하면 None (엉뚱한 곳을 대신 찾지 않는다). 카카오 오류면 Open-Meteo 검색으로 넘어간다."""
    if config.KAKAO_REST_API_KEY:
        try:
            return geocode_kakao(name, timeout)
        except WeatherError:
            pass
    return geocode_openmeteo(name, timeout)


def lookup_table(place):
    """표에서 찾는다. "서울 강남구"처럼 도시+구로 써도 되고, 정확히 있으면("부산 중구") 그것을 쓴다."""
    if place in CITIES:
        return (place, *CITIES[place])
    parts = place.split()
    if len(parts) == 2 and parts[1].endswith(("구", "군")) and parts[1] in CITIES:
        return (place, *CITIES[parts[1]])
    return None


def resolve(place, mode=None):
    """지역 이름 -> (표시 이름, 위도, 경도). 이름이 없으면 기본 위치. 못 찾으면 None.
    mode: table(표만) / geo(지명 검색만) / both(기본값, config.WEATHER_LOOKUP)
    both 순서: 광역시 이름이면 표 -> 카카오(키가 있으면) -> 카카오 오류·한도 초과 시 표 -> 표에도 없으면 Open-Meteo 검색.
    카카오가 '결과 없음'이라고 답하면 표만 확인하고 끝낸다 (Open-Meteo는 엉뚱한 곳을 찾기 쉬움)."""
    mode = mode or config.WEATHER_LOOKUP
    if not place:
        return config.WEATHER_PLACE, config.WEATHER_LAT, config.WEATHER_LON
    if mode == "table":
        return lookup_table(place)
    if mode == "geo":
        return geocode(place) if len(place) >= 2 else None
    if place in PREFER_TABLE and place in CITIES:
        return lookup_table(place)  # 광역시 이름은 표가 정확하다 (카카오는 "광주"에서 경기 광주시를 준다)
    kakao_failed = False
    if config.KAKAO_REST_API_KEY and len(place) >= 2:
        try:
            return geocode_kakao(place) or lookup_table(place)
        except WeatherError:
            kakao_failed = True  # 키 오류·한도 초과·접속 오류 -> 표 -> Open-Meteo
    found = lookup_table(place)
    if found:
        return found
    if len(place) < 2:
        return None
    return geocode_openmeteo(place) if kakao_failed else geocode(place)  # 카카오를 두 번 부르지 않는다


def fetch(lat=None, lon=None, timeout=10):
    params = {
        "latitude": config.WEATHER_LAT if lat is None else lat,
        "longitude": config.WEATHER_LON if lon is None else lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,precipitation_sum",
        "timezone": config.TIMEZONE,
        "forecast_days": FORECAST_DAYS,
        "past_days": PAST_DAYS,
    }
    try:
        r = requests.get(API, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise WeatherError(f"날씨 정보를 가져오지 못했습니다 ({type(e).__name__}). 잠시 후 다시 시도하세요.")
    except ValueError:
        raise WeatherError("날씨 응답을 해석하지 못했습니다.")


def _num(v, fmt="{:.0f}"):
    return "?" if v is None else fmt.format(v)


def _day_name(d, t):
    off = (d - t).days
    md = f"{d.month:02d}/{d.day:02d}"
    name = {0: "오늘", 1: "내일"}.get(off)  # 이름은 오늘·내일만, 나머지는 날짜(요일)
    return f"{name}({md})" if name else f"{md}({WEEKDAYS[d.weekday()]})"


def fmt(data, place, day="오늘"):
    """API 응답(JSON)을 사람이 읽는 문장으로 만든다. 기준일부터 SHOW_DAYS 일을 보여 주고,
    데이터가 없는 날은 '데이터가 없어요'로 표시한다. '지금' 줄은 기준일이 오늘일 때만 넣는다."""
    t = today()
    base = to_date(day, t)
    try:
        c, d = data["current"], data["daily"]
        index = {s: i for i, s in enumerate(d["time"])}
        lines = [f"📍 {place} 날씨" + (f" ({c['time'][11:16]} 기준)" if base == t else "")]
        if base == t:
            lines.append(
                f"지금: {describe(c['weather_code'])}, {_num(c['temperature_2m'])}°C "
                f"(체감 {_num(c['apparent_temperature'])}°C), 습도 {_num(c['relative_humidity_2m'])}%, "
                f"바람 {_num(c['wind_speed_10m'])}km/h")
        rain_days, missing = [], False
        for k in range(SHOW_DAYS):
            dt = base + timedelta(days=k)
            label = _day_name(dt, t)
            i = index.get(dt.isoformat())
            if i is None or d["weather_code"][i] is None:
                lines.append(f"{label}: 데이터가 없어요")
                missing = True
                continue
            prob = d["precipitation_probability_max"][i]
            lines.append(
                f"{label}: {describe(d['weather_code'][i])}, "
                f"최저 {_num(d['temperature_2m_min'][i])}° / 최고 {_num(d['temperature_2m_max'][i])}°, "
                f"강수확률 {_num(prob)}%, 강수량 {_num(d['precipitation_sum'][i], '{:.1f}')}mm"
            )
            if prob is not None and prob >= RAIN_ALERT:
                rain_days.append(label.split("(")[0])
        if missing and d["time"]:
            first, last = d["time"][0][5:].replace("-", "/"), d["time"][-1][5:].replace("-", "/")
            lines.append(f"(확인할 수 있는 날짜: {first} ~ {last})")
    except (KeyError, IndexError, TypeError):
        raise WeatherError("날씨 응답 형식이 예상과 다릅니다.")
    if rain_days:
        lines.append(f"☔ {'·'.join(rain_days)} 비 소식이 있습니다 (강수확률 {RAIN_ALERT}% 이상). 우산 챙기세요.")
    lines.append("(출처: Open-Meteo)")
    return "\n".join(lines)


def ambiguous_message(place):
    """이름이 여러 곳인 지역이면 후보 안내문, 아니면 None."""
    if place in AMBIGUOUS:
        return f"'{place}'은(는) 여러 곳입니다. 이렇게 다시 물어봐 주세요: " + " · ".join(AMBIGUOUS[place])
    return None


def not_found_message(place):
    msg = f"'{place}' 지역을 찾지 못했습니다. 도시 이름으로 다시 물어봐 주세요."
    if config.WEATHER_LOOKUP in ("table", "both"):
        msg += f"\n바로 되는 지역: {', '.join(c for c in CITIES if not c.endswith(('구', '군')))} (구·군도 됩니다. 예: 강남구, 해운대구, 부산 중구)"
    return msg


def report_place(place, day="오늘"):
    """지역 이름(None 이면 .env 기본 위치)과 날짜(normalize_day 의 결과)로 날씨 안내문을 만든다."""
    msg = ambiguous_message(place)
    if msg:
        return msg
    found = resolve(place)
    if not found:
        return not_found_message(place)
    label, lat, lon = found
    return fmt(fetch(lat, lon), label, day)


def report(text="날씨"):
    """문장(또는 '날씨')을 받아 날씨 안내문을 돌려준다. 지역이 없으면 .env 기본 위치."""
    place, day = parse_query(text) or (None, "오늘")
    return report_place(place, day)  # day 가 None 이면 report_place -> fmt 가 안내


def compare(names):
    """표 / 카카오 / Open-Meteo 지명 검색 결과를 나란히 보여 준다 (날씨는 조회하지 않음).
    실행: python -m features.weather 부산 해운대 강남 광주"""
    show = lambda r: "못 찾음" if r is None else (r if isinstance(r, str) else f"{r[0]}  ({r[1]:.4f}, {r[2]:.4f})")

    def attempt(fn, name):
        try:
            return fn(name)
        except WeatherError as e:
            return f"오류: {e}"

    for name in names:
        t = resolve(name, "table")
        k = attempt(geocode_kakao, name) if config.KAKAO_REST_API_KEY else "키 없음 (.env의 KAKAO_REST_API_KEY)"
        g = attempt(geocode_openmeteo, name)
        final = attempt(resolve, name)
        print(f"[{name}]\n  카카오     : {show(k)}\n  표         : {show(t)}\n  Open-Meteo : {show(g)}\n  ==> 실제로 쓰는 값 : {show(final)}")


def check_table(limit_km=5.0):
    """표의 좌표가 카카오 검색 결과와 얼마나 떨어져 있는지 확인한다 (카카오 키 필요). 멀리 떨어진 것만 출력한다.
    카카오는 구청·역 같은 장소를 찾기 때문에 1~3km 차이는 정상이다. 실행: python -m features.weather --check-table"""
    import math
    import time
    if not config.KAKAO_REST_API_KEY:
        print("KAKAO_REST_API_KEY 가 없습니다 (.env)")
        return
    bad = 0
    for name, (lat, lon) in CITIES.items():
        try:
            k = geocode_kakao(name)
        except WeatherError as e:
            print(f"[{name}] 오류: {e}")
            continue
        if k is None:
            print(f"[{name}] 카카오 결과 없음")
            continue
        _, klat, klon = k
        dy, dx = (klat - lat) * 111.0, (klon - lon) * 111.0 * math.cos(math.radians(lat))
        km = math.hypot(dx, dy)
        if km > limit_km:
            bad += 1
            print(f"[{name}] 표 ({lat}, {lon}) / 카카오 {k[0]} ({klat:.4f}, {klon:.4f}) -> {km:.1f}km 차이")
        time.sleep(0.1)
    print(f"확인 끝: 표 {len(CITIES)}개 중 {limit_km}km 넘게 다른 것 {bad}개")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--check-table"]:
        check_table()
        sys.exit()
    compare(sys.argv[1:] or ["서울", "부산", "광주", "해운대", "강남", "성산", "고성", "판교"])
