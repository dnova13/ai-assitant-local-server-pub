"""뉴스: !뉴스 보기와 채널 구독 게시 (README.md).
- 기사는 코드가 RSS 와 Google 뉴스 검색에서 가져온다. 모델은 부르지 않는다 (요약·번역 없음).
- 카테고리, 출처, 관심어, 제외 언론사, 구독은 비서 DB 에 있어 명령으로 바꾸고 재시작이 필요 없다.
- 서버가 입력한 주소로 접속하므로 내부 주소는 막는다. 리다이렉트도 매번 다시 검사한다.
  DNS 답이 검사와 접속 사이에 바뀌는 경우까지는 막지 못한다. 출처 변경이 관리자 전용인 이유이기도 하다."""
import html
import ipaddress
import logging
import math
import re
import socket
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import quote, urljoin, urlsplit

import requests

from clients import db
from features import schedule

log = logging.getLogger("assistant.news")
UA = {"User-Agent": "Mozilla/5.0 (local-ai assistant news)"}
DEFAULT_COUNT = 5
MAX_COUNT = 20
MAX_SOURCES = 20
MAX_KEYWORDS = 10
MAX_TIMES = 24
FETCH_TIMEOUT = 10
FETCH_WORKERS = 5
MAX_REDIRECTS = 3
MAX_BYTES = 5 * 1024 * 1024
MAX_SECONDS = 20  # 조금씩 보내는 서버에 묶이지 않게 한 주소에 쓰는 전체 시간
TITLE_LIMIT = 300
FAIL_MARK = 3  # 이만큼 연달아 실패하면 !뉴스 출처 에 표시한다
SOURCE_SHARE = 0.6  # 5개 중 출처 3개, 관심어 2개
SEND_WINDOW = timedelta(hours=1)  # 정한 시각부터 이 시간 안이면 올린다
RETRY_AFTER = timedelta(minutes=5)  # 출처가 모두 실패하면 이만큼 뒤에 다시
SENT_KEEP = timedelta(days=30)
DEFAULT_VIEW = ("it", "일반")  # !뉴스 가 함께 보여 주는 카테고리. 지울 수 없다
DEFAULTS = (
    ("it", "기술", ("https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=ko&gl=KR&ceid=KR:ko",
                    "https://news.hada.io/rss/news", "https://feeds.feedburner.com/zdkorea",
                    "https://www.itworld.co.kr/feed/")),
    ("일반", "", ("https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko",)),
)
RESERVED = {"구독", "관심어", "출처", "제외", "카테고리", "도움말", "사용법", "추가", "삭제", "취소", "전체"}
EDITIONS = {"일본": ("ja", "JP"), "미국": ("en-US", "US")}  # 관심어 앞에 붙이는 Google 뉴스 현지판
ADMIN_ACTIONS = {"추가", "삭제"}
USAGE = """뉴스 사용법
  !뉴스 [개수]             it 와 일반을 함께 보기 (기본 5개씩, 개수는 합친 수, 최대 20)
  !뉴스 <카테고리> [개수]  그 카테고리 보기 (기본 5개, 최대 20)
  !뉴스 카테고리           카테고리 목록
  !뉴스 구독               이 채널의 구독 (카테고리, 시각, 개수)
  !뉴스 출처 | 관심어 | 제외   출처, 관심어, 제외 언론사 보기
관리자만
  !뉴스 카테고리 추가 <이름> | 삭제 <이름>
  !뉴스 구독 전체          모든 채널의 구독
  !뉴스 구독 <카테고리>    이 채널에 구독 추가 (시각을 정해야 올라와요)
  !뉴스 구독 <카테고리> <시각들>   예: !뉴스 구독 it 07:00,12:00
  !뉴스 구독 <카테고리> 개수 <수>  한 번에 올릴 기사 수 (1~20, 기본 5)
  !뉴스 구독 <카테고리> 취소
  !뉴스 출처 추가 <카테고리> <주소 또는 site:도메인> | 삭제 <카테고리> <주소 또는 번호>
  !뉴스 관심어 추가 <카테고리> <단어, 단어> | 삭제 <카테고리> <단어 또는 번호, ...>   일본:, 미국: 을 앞에 붙이면 현지판
  !뉴스 제외 추가 <언론사> | 삭제 <언론사>"""


SUB_USAGE = "사용법: !뉴스 구독 <카테고리> [시각들 | 개수 <수> | 취소]. 예: !뉴스 구독 it 07:00,12:00"


class NewsError(Exception):
    """사용자에게 그대로 보여 줄 안내."""


_retry_at = {}  # 구독 번호 -> 다시 시도할 시각 (봇 하나만 부르고, 재시작하면 잊어도 된다)


# --- 가져오기 ---
def _text(el):
    return re.sub(r"\s+", " ", html.unescape((el.text or "") if el is not None else "")).strip()


def clean_link(link):
    """링크 끝의 추적용 utm_ 꼬리표를 지운다."""
    base, _, query = link.partition("?")
    keep = [q for q in query.split("&") if q and not q.startswith("utm_")]
    return base + ("?" + "&".join(keep) if keep else "")


def _clean_title(title, press):
    """Google 뉴스 제목 끝의 ' - 언론사'를 지운다."""
    tail = f" - {press}"
    return title[:-len(tail)].strip() if press and title.endswith(tail) else title


def _feed_name(title):
    """피드 제목에서 앞 이름만. 'GeekNews - 개발 뉴스' -> 'GeekNews'."""
    return re.split(r"\s[-|]\s", title)[0].strip()


def parse_feed(xml_text):
    """RSS 2.0 또는 Atom 글자 -> [(제목, 링크, 언론사)]. 언론사는 <source>, 피드 제목 순서이고 없으면 ''. 형식이 틀리면 빈 목록."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    feed_press = _feed_name(_text(root.find("channel/title")))
    for item in root.iter("item"):
        press = _text(item.find("source")) or feed_press
        title, link = _clean_title(_text(item.find("title")), press), _text(item.find("link"))
        if title and link:
            out.append((title, clean_link(link), press))
    if not out:
        ns = "{http://www.w3.org/2005/Atom}"
        feed_press = _feed_name(_text(root.find(f"{ns}title")))
        for entry in root.iter(f"{ns}entry"):
            title = _clean_title(_text(entry.find(f"{ns}title")), feed_press)
            links = [e for e in entry.findall(f"{ns}link") if e.get("rel", "alternate") == "alternate"]
            link = links[0].get("href", "").strip() if links else ""
            if title and link:
                out.append((title, clean_link(link), feed_press))
    return out


def check_url(url):
    """접속해도 되는 주소인가. http(s)만 받고, 호스트의 IP 가 하나라도 내부 주소면 NewsError."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise NewsError("주소 형식이 틀렸어요.")
    if parts.scheme not in ("http", "https") or not host:
        raise NewsError("http 또는 https 주소만 쓸 수 있어요.")
    if parts.username or parts.password:
        raise NewsError("아이디나 비밀번호가 들어간 주소는 쓸 수 없어요.")
    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        raise NewsError("주소를 찾을 수 없어요.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global or ip.is_multicast:
            raise NewsError("서버 내부 주소는 쓸 수 없어요.")


def _read_limited(response):
    body, started = b"", time.monotonic()
    for chunk in response.iter_content(65536):
        body += chunk
        if len(body) > MAX_BYTES:
            raise NewsError("내용이 너무 커요.")
        if time.monotonic() - started > MAX_SECONDS:
            raise NewsError("내용을 받는 데 너무 오래 걸려요.")
    return body


def fetch_feed(url, timeout=FETCH_TIMEOUT):
    """주소 하나를 읽어 기사 목록으로. 리다이렉트는 직접 따라가며 주소를 다시 검사한다."""
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        with requests.get(url, headers=UA, timeout=timeout, stream=True, allow_redirects=False) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers["Location"])
                continue
            r.raise_for_status()
            return parse_feed(_read_limited(r))
    raise NewsError("다른 주소로 너무 많이 넘어가요.")


def search_url(text):
    """관심어나 site: 출처 -> Google 뉴스 RSS 검색 주소 (최근 7일). '일본:', '미국:'은 현지판."""
    prefix, sep, rest = text.partition(":")
    hl, gl = "ko", "KR"
    if sep and prefix in EDITIONS and rest.strip():
        (hl, gl), text = EDITIONS[prefix], rest.strip()
    return (f"https://news.google.com/rss/search?q={quote(text + ' when:7d')}"
            f"&hl={hl}&gl={gl}&ceid={gl}:{hl.split('-')[0]}")


def _source_url(source):
    return search_url(source) if source.startswith("site:") else source


def _fetch_all(urls):
    """주소들을 동시에 읽는다. 주소마다 기사 목록, 실패하면 None."""
    def one(url):
        try:
            return fetch_feed(url)
        except (requests.RequestException, ValueError, NewsError, ET.ParseError) as e:
            log.warning("뉴스 출처 읽기 실패: %s", type(e).__name__)
            return None
    if not urls:
        return []
    with ThreadPoolExecutor(FETCH_WORKERS) as pool:
        return list(pool.map(one, urls))


# --- 저장: 카테고리 ---
def _ensure_defaults():
    """처음 쓸 때 기본값을 넣는다. it 와 일반은 지울 수 없어 표가 다시 비지 않는다."""
    with db.connect() as c:
        if c.execute("SELECT 1 FROM news_categories LIMIT 1").fetchone():
            return
        now = schedule.now()
        for name, aliases, sources in DEFAULTS:
            c.execute("INSERT INTO news_categories(name, aliases, created) VALUES (?,?,?) ON CONFLICT (name) DO NOTHING",
                      (name, aliases, now))
            for url in sources:
                c.execute("INSERT INTO news_sources(category, url, fails, created) VALUES (?,?,0,?)", (name, url, now))


def categories():
    """[(이름, [별칭])]"""
    _ensure_defaults()
    with db.connect() as c:
        rows = c.execute("SELECT name, aliases FROM news_categories ORDER BY created, name").fetchall()
    return [(r[0], [a for a in r[1].split(",") if a]) for r in rows]


def resolve(word):
    """이름이나 별칭 -> 카테고리 이름. 대소문자를 가리지 않고, 없으면 None."""
    word = word.casefold()
    for name, aliases in categories():
        if word == name.casefold() or word in [a.casefold() for a in aliases]:
            return name
    return None


def _need_category(word):
    name = resolve(word)
    if name is None:
        raise NewsError(f"'{word}' 카테고리는 없어요.\n{categories_text()}")
    return name


def categories_text():
    parts = [name + (f" ({', '.join(aliases)})" if aliases else "") for name, aliases in categories()]
    return "뉴스 카테고리: " + ", ".join(parts)


def add_category(name):
    if name.isdecimal() or name in RESERVED or len(name) > 20:
        raise NewsError(f"'{name}'은(는) 카테고리 이름으로 쓸 수 없어요.")
    if resolve(name):
        return f"'{name}' 카테고리는 이미 있어요."
    with db.connect() as c:
        c.execute("INSERT INTO news_categories(name, aliases, created) VALUES (?,?,?)", (name, "", schedule.now()))
    return f"'{name}' 카테고리를 추가했어요. 출처나 관심어가 없으면 이름으로 찾아요."


def check_deletable(word):
    """지울 수 있으면 카테고리 이름을 돌려준다."""
    name = _need_category(word)
    if name in DEFAULT_VIEW:
        raise NewsError(f"'{name}'은(는) !뉴스 의 기본 카테고리라 지울 수 없어요.")
    with db.connect() as c:
        if c.execute("SELECT 1 FROM news_subs WHERE category=? LIMIT 1", (name,)).fetchone():
            raise NewsError(f"'{name}'을(를) 구독 중인 채널이 있어요. 구독을 먼저 취소해 주세요 (!뉴스 구독 전체).")
    return name


def delete_category(word):
    name = check_deletable(word)
    with db.connect() as c:
        for table in ("news_sources", "news_keywords", "news_sent"):
            c.execute(f"DELETE FROM {table} WHERE category=?", (name,))
        c.execute("DELETE FROM news_categories WHERE name=?", (name,))
    return f"'{name}' 카테고리를 지웠어요."


# --- 저장: 출처 ---
def sources(category):
    """[(번호, 주소, 연속 실패 수)]"""
    with db.connect() as c:
        return c.execute("SELECT id, url, fails FROM news_sources WHERE category=? ORDER BY id", (category,)).fetchall()


def _normal_source(text):
    text = text.strip()
    if text.lower().startswith("site:"):
        domain = text[5:].strip().lower()
        if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}", domain):
            raise NewsError("도메인 형식이 틀렸어요. 예: site:example.com")
        return f"site:{domain}"
    return text.strip("<>")


def add_source(word, text):
    name, source = _need_category(word), _normal_source(text)
    rows = sources(name)
    if len(rows) >= MAX_SOURCES:
        raise NewsError(f"출처는 카테고리당 {MAX_SOURCES}개까지예요.")
    if source in [r[1] for r in rows]:
        return "이미 등록한 출처예요."
    try:
        found = fetch_feed(_source_url(source))
    except requests.RequestException as e:
        raise NewsError(f"주소를 읽지 못해 등록하지 않았어요 ({type(e).__name__}).")
    if not found:
        raise NewsError("이 주소에서 기사를 찾지 못해 등록하지 않았어요.")
    with db.connect() as c:
        c.execute("INSERT INTO news_sources(category, url, fails, created) VALUES (?,?,0,?)", (name, source, schedule.now()))
    return f"{name} 출처를 추가했어요 (기사 {len(found)}건 확인)."


def _pick(rows, target, column=1):
    """주소·단어 또는 목록 번호로 지울 행을 찾는다."""
    target = target.strip().strip("<>")
    if target.isdecimal() and 1 <= int(target) <= len(rows):
        return rows[int(target) - 1]
    for row in rows:
        if row[column] == target:
            return row
    return None


def delete_source(word, target):
    name = _need_category(word)
    row = _pick(sources(name), target)
    if row is None:
        raise NewsError("그런 출처가 없어요. 목록: !뉴스 출처")
    with db.connect() as c:
        c.execute("DELETE FROM news_sources WHERE id=?", (row[0],))
    return f"{name} 출처를 지웠어요: <{row[1]}>"


def sources_text():
    lines = ["뉴스 출처"]
    for name, _ in categories():
        rows = sources(name)
        lines.append(f"[{name}]" + ("" if rows else " 없음"))
        for i, (_, url, fails) in enumerate(rows, 1):
            mark = f" ⚠️ 연속 {fails}회 실패" if fails >= FAIL_MARK else ""
            lines.append(f"{i}. <{url}>{mark}")
    return "\n".join(lines)


# --- 저장: 관심어 ---
def keywords(category):
    """[(번호, 단어)]"""
    with db.connect() as c:
        return c.execute("SELECT id, text FROM news_keywords WHERE category=? ORDER BY id", (category,)).fetchall()


def _split_words(text):
    """쉼표로 나눈 단어들 (띄어쓰기는 단어 안에 둔다). 겹치면 한 번만."""
    words = [" ".join(w.split()) for w in re.split(r"[,，]", text)]
    return list(dict.fromkeys(w for w in words if w))


def add_keyword(word, text):
    """쉼표로 여러 개 받는다. 틀린 단어가 있거나 한도를 넘으면 하나도 넣지 않는다."""
    name, words = _need_category(word), _split_words(text)
    for w in words:
        prefix, sep, rest = w.partition(":")
        if len(w) > 60 or (sep and prefix in EDITIONS and not rest.strip()):
            raise NewsError(f"관심어를 알 수 없어요: {w[:60]}. 예: !뉴스 관심어 추가 it 보안, 클라우드")
    if not words:
        raise NewsError("관심어를 알 수 없어요. 예: !뉴스 관심어 추가 it 보안, 클라우드")
    rows = keywords(name)
    have = {r[1] for r in rows}
    new, skipped = [w for w in words if w not in have], [w for w in words if w in have]
    if len(rows) + len(new) > MAX_KEYWORDS:
        raise NewsError(f"관심어는 카테고리당 {MAX_KEYWORDS}개까지예요. 지금 {len(rows)}개라 {len(new)}개를 더할 수 없어요.")
    current = schedule.now()
    with db.connect() as c:
        for w in new:
            c.execute("INSERT INTO news_keywords(category, text, created) VALUES (?,?,?)", (name, w, current))
    lines = [f"{name} 관심어를 추가했어요: {', '.join(new)}"] if new else []
    if skipped:
        lines.append(f"이미 등록한 관심어예요: {', '.join(skipped)}")
    return "\n".join(lines)


def delete_keyword(word, target):
    """쉼표로 여러 개 받는다. 번호는 지우기 전 목록의 번호다."""
    name = _need_category(word)
    rows, picked, missing = keywords(name), {}, []
    for w in _split_words(target):
        row = _pick(rows, w)
        if row is None:
            missing.append(w)
        else:
            picked[row[0]] = row[1]
    if not picked:
        raise NewsError("그런 관심어가 없어요. 목록: !뉴스 관심어")
    with db.connect() as c:
        for kid in picked:
            c.execute("DELETE FROM news_keywords WHERE id=?", (kid,))
    lines = [f"{name} 관심어를 지웠어요: {', '.join(picked.values())}"]
    if missing:
        lines.append(f"없는 관심어: {', '.join(missing)}")
    return "\n".join(lines)


def keywords_text():
    lines = ["뉴스 관심어"]
    for name, _ in categories():
        rows = keywords(name)
        lines.append(f"[{name}] " + (", ".join(r[1] for r in rows) if rows else "없음"))
    return "\n".join(lines)


# --- 저장: 제외 언론사 ---
def excluded():
    with db.connect() as c:
        return [r[0] for r in c.execute("SELECT name FROM news_excluded ORDER BY name").fetchall()]


def add_excluded(name):
    if name in excluded():
        return f"'{name}'은(는) 이미 제외했어요."
    with db.connect() as c:
        c.execute("INSERT INTO news_excluded(name, created) VALUES (?,?)", (name, schedule.now()))
    return f"'{name}' 기사를 모든 뉴스에서 뺄게요. 이름이 정확히 같을 때만 빠져요."


def delete_excluded(name):
    with db.connect() as c:
        done = c.execute("DELETE FROM news_excluded WHERE name=?", (name,)).rowcount
    return f"'{name}' 제외를 풀었어요." if done else f"'{name}'은(는) 제외 목록에 없어요."


def excluded_text():
    rows = excluded()
    return "제외 언론사: " + ", ".join(rows) if rows else "제외한 언론사가 없어요."


# --- 기사 고르기 ---
def _title_key(title):
    return " ".join(title.casefold().split())[:TITLE_LIMIT]


def _round_robin(feeds, start=0):
    """여러 목록에서 하나씩 번갈아 꺼낸다. start 번째 목록부터."""
    if not feeds:
        return
    start %= len(feeds)
    feeds = feeds[start:] + feeds[:start]
    for i in range(max(len(f) for f in feeds)):
        for f in feeds:
            if i < len(f):
                yield f[i]


def _rotated(rows, turn):
    if not rows:
        return rows
    turn %= len(rows)
    return rows[turn:] + rows[:turn]


def split_quota(count, has_sources, has_keywords):
    """(출처 몫, 관심어 몫). 한쪽만 있으면 그쪽이 다 가진다."""
    if not has_keywords:
        return count, 0
    if not has_sources:
        return 0, count
    first = math.ceil(count * SOURCE_SHARE)
    return first, count - first


def collect(category, count, turn=0, sent=()):
    """(기사 목록, 쓴 관심어 수). sent: 이미 보낸 제목 키. 출처를 모두 읽지 못하면 NewsError."""
    src_rows, kw_rows = sources(category), keywords(category)
    words = [r[1] for r in kw_rows] or ([] if src_rows else [category])  # 출처도 관심어도 없으면 카테고리 이름으로 찾는다
    results = _fetch_all([_source_url(r[1]) for r in src_rows] + [search_url(w) for w in _rotated(words, turn)])
    src_results, kw_results = results[:len(src_rows)], results[len(src_rows):]
    _record_fails(src_rows, src_results)
    if results and all(r is None for r in results):
        raise NewsError("뉴스를 가져오지 못했어요.")
    n_src, n_kw = split_quota(count, bool(src_rows), bool(words))
    src_iter = _round_robin([r for r in src_results if r], turn)
    kw_iter = _round_robin([r for r in kw_results if r])
    skip_press, seen, out = set(excluded()), set(sent), []

    def take(articles, n):
        got = []
        if n <= 0:
            return got
        for article in articles:
            key = _title_key(article[0])
            if key not in seen and article[2] not in skip_press:
                seen.add(key)
                got.append(article)
                if len(got) >= n:
                    break
        return got
    out += take(src_iter, n_src)
    out += take(kw_iter, n_kw)
    out += take(src_iter, count - len(out))
    out += take(kw_iter, count - len(out))
    return out, n_kw if words else 0


def _record_fails(rows, results):
    with db.connect() as c:
        for row, result in zip(rows, results):
            fails = row[2] + 1 if result is None else 0
            if fails != row[2]:
                c.execute("UPDATE news_sources SET fails=? WHERE id=?", (fails, row[0]))


def format_articles(category, articles, day=None):
    day = day or schedule.now()
    lines = [f"📰 {day.month}월 {day.day}일 {category} 뉴스"]
    for i, (title, link, press) in enumerate(articles, 1):
        lines.append(f"{i}. {title}" + (f" ({press})" if press else ""))
        lines.append(f"   <{link}>")
    return "\n".join(lines)


def _view(category, count):
    try:
        articles, _ = collect(category, count)
    except NewsError as e:
        return f"📰 {category} 뉴스\n⚠️ {e}"
    return format_articles(category, articles) if articles else f"📰 {category} 뉴스\n가져온 기사가 없어요."


def view_default(total=None):
    """!뉴스: it 와 일반을 함께. total 은 합친 개수이고 홀수면 앞 카테고리가 하나 더."""
    total = total or DEFAULT_COUNT * len(DEFAULT_VIEW)
    first = (total + 1) // 2
    parts = zip(DEFAULT_VIEW, (first, total - first))
    return "\n\n".join(_view(name, n) for name, n in parts if n)


# --- 구독 ---
def parse_times(text):
    """'7:00,12:00 19:00' -> ['07:00', '12:00', '19:00'] (정렬, 중복 제거). 틀리면 NewsError."""
    found = set()
    for word in re.split(r"[,\s]+", text.strip()):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", word)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise NewsError(f"시각을 알 수 없어요: {word}. 예: 07:00,12:00")
        found.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    if len(found) > MAX_TIMES:
        raise NewsError(f"시각은 {MAX_TIMES}개까지예요.")
    return sorted(found)


def due_slot(times, last_slot, current):
    """올릴 차례인 시각 'YYYY-MM-DD HH:MM' 또는 None. 1시간 안의 가장 최근 시각만 보고, 이미 올렸으면 None."""
    best = None
    for day in (current - timedelta(days=1), current):  # 23:30 시각은 다음 날 00:10 에도 올릴 수 있다
        for hhmm in times:
            h, m = (int(x) for x in hhmm.split(":"))
            start = current.replace(year=day.year, month=day.month, day=day.day, hour=h, minute=m, second=0, microsecond=0)
            if start <= current < start + SEND_WINDOW:
                best = max(best, start) if best else start
    if best is None:
        return None
    slot = best.strftime("%Y-%m-%d %H:%M")
    return None if last_slot and slot <= last_slot else slot


def _no_times(times):
    return "시각 없음 (올라오지 않아요)" if not times else times.replace(",", ", ")


def _sub_line(row):
    return f"{row[3]}: {_no_times(row[4])} / {row[7]}개"


def add_sub(channel, word):
    """시각 없이 구독을 만든다. 이미 있으면 설정을 보여 준다."""
    name = _need_category(word)
    with db.connect() as c:
        row = c.execute("SELECT id FROM news_subs WHERE channel_id=? AND category=?", (channel["id"], name)).fetchone()
        if not row:
            c.execute("INSERT INTO news_subs(channel_id, channel_name, category, times, turn, created, article_count) "
                      "VALUES (?,?,?,'',0,?,?)", (channel["id"], channel.get("name") or "", name, schedule.now(), DEFAULT_COUNT))
            return f"이 채널에 {name} 구독을 추가했어요. 시각을 정해야 올라와요. 예: !뉴스 구독 {name} 07:00,19:00"
    current = [r for r in _sub_rows(channel["id"]) if r[3] == name][0]
    return f"이미 구독 중이에요. {_sub_line(current)}"


def set_count(channel, word, count):
    name = _need_category(word)
    with db.connect() as c:
        done = c.execute("UPDATE news_subs SET article_count=? WHERE channel_id=? AND category=?",
                         (count, channel["id"], name)).rowcount
    if not done:
        return f"이 채널은 {name}을(를) 구독하지 않았어요. 먼저: !뉴스 구독 {name}"
    return f"이 채널의 {name} 뉴스를 한 번에 {count}개씩 올려요."


def subscribe(channel, word, times_text):
    name, times = _need_category(word), parse_times(times_text)
    joined = ",".join(times)
    current = schedule.now()
    now_slot = due_slot(times, None, current)  # 이미 지난 시각의 기사를 구독하자마자 올리지 않는다
    with db.connect() as c:
        row = c.execute("SELECT id, last_slot FROM news_subs WHERE channel_id=? AND category=?",
                        (channel["id"], name)).fetchone()
        if row:
            last = max(row[1] or "", now_slot or "") or None
            c.execute("UPDATE news_subs SET channel_name=?, times=?, last_slot=? WHERE id=?",
                      (channel.get("name") or "", joined, last, row[0]))
        else:
            c.execute("INSERT INTO news_subs(channel_id, channel_name, category, times, last_slot, turn, created) "
                      "VALUES (?,?,?,?,?,0,?)", (channel["id"], channel.get("name") or "", name, joined, now_slot, current))
    return f"이 채널에 {name} 뉴스를 매일 {', '.join(times)}에 올려요."


def unsubscribe(channel, word):
    name = _need_category(word)
    with db.connect() as c:
        done = c.execute("DELETE FROM news_subs WHERE channel_id=? AND category=?", (channel["id"], name)).rowcount
    return f"이 채널의 {name} 구독을 취소했어요." if done else f"이 채널은 {name}을(를) 구독하지 않았어요."


def _sub_rows(channel_id=None):
    sql = "SELECT id, channel_id, channel_name, category, times, last_slot, turn, article_count FROM news_subs"
    with db.connect() as c:
        if channel_id is None:
            return c.execute(f"{sql} ORDER BY channel_name, category").fetchall()
        return c.execute(f"{sql} WHERE channel_id=? ORDER BY category", (channel_id,)).fetchall()


def subs_text(channel):
    rows = _sub_rows(channel["id"])
    if not rows:
        return "이 채널은 구독한 뉴스가 없어요. 구독하려면: !뉴스 구독 <카테고리>"
    return "이 채널의 구독\n" + "\n".join(f"- {_sub_line(r)}" for r in rows)


def all_subs_text():
    rows = _sub_rows()
    if not rows:
        return "구독한 채널이 없어요."
    return "뉴스 구독 전체\n" + "\n".join(f"- #{r[2] or r[1]} {_sub_line(r)}" for r in rows)


def _sent_keys(channel_id, category):
    with db.connect() as c:
        return {r[0] for r in c.execute("SELECT title FROM news_sent WHERE channel_id=? AND category=?",
                                        (channel_id, category)).fetchall()}


def due_posts(current=None):
    """올릴 차례인 구독 [{"id", "channel_id", "slot", "text", "titles", "turn"}]. 새 기사가 없으면 시각만 넘기고 올리지 않는다."""
    current = current or schedule.now()
    out = []
    for sid, channel_id, _, category, times, last_slot, turn, count in _sub_rows():
        if not times:  # 시각을 아직 정하지 않은 구독
            continue
        slot = due_slot(times.split(","), last_slot, current)
        if slot is None or _retry_at.get(sid, current) > current:
            continue
        try:
            articles, used = collect(category, count, turn, _sent_keys(channel_id, category))
        except NewsError:
            _retry_at[sid] = current + RETRY_AFTER
            continue
        _retry_at.pop(sid, None)
        next_turn = turn + used
        if not articles:
            mark_posted(sid, slot, [], next_turn)
            continue
        out.append({"id": sid, "channel_id": channel_id, "slot": slot, "text": format_articles(category, articles, current),
                    "titles": [a[0] for a in articles], "turn": next_turn})
    return out


def mark_posted(sid, slot, titles=(), turn=None):
    """올렸다고 표시한다. titles 없이 부르면 그 시각만 넘긴다 (올릴 수 없는 채널. 기사는 보낸 것으로 치지 않는다)."""
    current = schedule.now()
    with db.connect() as c:
        row = c.execute("SELECT channel_id, category, turn FROM news_subs WHERE id=?", (sid,)).fetchone()
        if row is None:
            return False
        c.execute("UPDATE news_subs SET last_slot=?, turn=? WHERE id=?", (slot, row[2] if turn is None else turn, sid))
        for title in titles:
            c.execute("INSERT INTO news_sent(channel_id, category, title, sent) VALUES (?,?,?,?) "
                      "ON CONFLICT (channel_id, category, title) DO NOTHING", (row[0], row[1], _title_key(title), current))
        c.execute("DELETE FROM news_sent WHERE sent < ?", (current - SENT_KEEP,))
    return True


# --- 명령 ---
def needs_admin(rest):
    """관리자만 쓰는 명령인가. 보기는 누구나 쓴다."""
    words = rest.split()
    if not words:
        return False
    if words[0] == "구독":
        return len(words) > 1
    return words[0] in ("카테고리", "출처", "관심어", "제외") and len(words) > 1 and words[1] in ADMIN_ACTIONS


def _count(word):
    n = int(word)
    if not 1 <= n <= MAX_COUNT:
        raise NewsError(f"기사 수는 1~{MAX_COUNT}개예요.")
    return n


def _two_args(rest, usage):
    """'추가 it <나머지>'의 (카테고리, 나머지). 모자라면 사용법."""
    parts = rest.split(None, 1)
    if len(parts) < 2:
        raise NewsError(f"사용법: {usage}")
    return parts[0], parts[1]


def _subscription(rest, channel):
    """!뉴스 구독 [전체] 또는 !뉴스 구독 <카테고리> [시각들 | 개수 N | 취소]."""
    words = rest.split()
    if words == ["전체"]:
        return all_subs_text()
    if channel is None:
        return "구독은 디스코드 채널에서만 받아요. 받을 채널에서 쳐 주세요."
    if not words:
        return subs_text(channel)
    if words[0] in ("취소", "개수"):
        return SUB_USAGE
    word, more = words[0], words[1:]
    if not more:
        return add_sub(channel, word)
    if more == ["취소"]:
        return unsubscribe(channel, word)
    if more[0] == "개수":
        if len(more) != 2 or not more[1].isdecimal():
            return SUB_USAGE
        return set_count(channel, word, _count(more[1]))
    return subscribe(channel, word, " ".join(more))


def run(rest, channel=None):
    """!뉴스 뒤의 글자 처리. 권한은 needs_admin 으로 입구에서 먼저 본다. 카테고리 삭제 확인은 assistant 가 한다."""
    words = rest.split()
    if not words:
        return view_default()
    head, tail = words[0], rest.split(None, 1)[1].strip() if len(words) > 1 else ""
    action, _, more = tail.partition(" ")
    more = more.strip()
    if head in ("도움말", "사용법"):
        return USAGE
    if head == "구독":
        return _subscription(tail, channel)
    if head == "카테고리":
        if not tail:
            return categories_text()
        if action == "추가" and more and len(more.split()) == 1:
            return add_category(more)
        return "사용법: !뉴스 카테고리 추가 <이름> | 삭제 <이름>"
    if head == "출처":
        if not tail:
            return sources_text()
        usage = "!뉴스 출처 추가 <카테고리> <주소 또는 site:도메인> | 삭제 <카테고리> <주소 또는 번호>"
        if action == "추가":
            return add_source(*_two_args(more, usage))
        if action == "삭제":
            return delete_source(*_two_args(more, usage))
        return f"사용법: {usage}"
    if head == "관심어":
        if not tail:
            return keywords_text()
        usage = "!뉴스 관심어 추가 <카테고리> <단어, 단어> | 삭제 <카테고리> <단어 또는 번호, ...>"
        if action == "추가":
            return add_keyword(*_two_args(more, usage))
        if action == "삭제":
            return delete_keyword(*_two_args(more, usage))
        return f"사용법: {usage}"
    if head == "제외":
        if not tail:
            return excluded_text()
        if action == "추가" and more:
            return add_excluded(more)
        if action == "삭제" and more:
            return delete_excluded(more)
        return "사용법: !뉴스 제외 추가 <언론사> | 삭제 <언론사>"
    if head.isdecimal() and len(words) == 1:
        return view_default(_count(head))
    name = resolve(head)
    if name is None:
        return f"'{head}' 카테고리는 없어요.\n{categories_text()}"
    if len(words) == 1:
        return _view(name, DEFAULT_COUNT)
    if len(words) == 2 and words[1].isdecimal():
        return _view(name, _count(words[1]))
    return "사용법: !뉴스 <카테고리> [개수]"
