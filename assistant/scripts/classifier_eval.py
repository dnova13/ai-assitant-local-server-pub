"""분류기 시험. 시험 문장을 모델에 하나씩 보내 정확도, JSON 형식 성공률, 응답 시간을 잰다.
지금 비서의 코드 규칙("날씨"라는 글자가 있으면 날씨)도 같은 문장으로 비교한다.

실행 (assistant/ 폴더, 가상환경 켠 상태, 서버 Ollama에 닿아야 함):
  python -m scripts.classifier_eval                                  # 세 모델 비교 (8b, 7b coder, 3b coder)
  python -m scripts.classifier_eval --models llama3.1:8b             # 모델 지정 (쉼표로 여러 개)
  python -m scripts.classifier_eval --all                            # 문장별 결과 전부 출력 (기본은 틀린 것만)
  python -m scripts.classifier_eval --no-rule                        # 코드 규칙 비교 빼기
  python -m scripts.classifier_eval --models qwen3:8b,llama3.1:8b --think off   # 생각을 끄고 비교
"""
import re
import argparse

from core import classifier
from features import weather

DEFAULT_MODELS = "llama3.1:8b,qwen2.5-coder:7b,qwen2.5-coder:3b"
DATE = "날짜지정"  # 기대 날짜가 특정 날짜(YYYY-MM-DD)인 문장
SLOT_INTENTS = ("날씨", "메모저장", "메모조회", "메모삭제", "일정등록", "일정삭제")  # 지역·날짜 또는 내용을 채점하는 의도
ANY = "_"  # 기대 의도가 정해져 있지 않은 문장 (의도는 채점하지 않음)

# (번호, 문장, 기대 의도, 기대 지역, 기대 날짜)  날씨가 아니면 지역·날짜는 None
# 메모·일정 의도에서는 "기대 지역" 칸이 기대 내용이다: 메모저장=저장할 글자, 메모조회=검색어 또는 None, 메모삭제=번호(숫자) 또는 None, 메모요약=None,
# 일정등록=등록할 글자(날짜·시각·할 일), 일정조회=None
CASES = [
    # A. 날씨여야 하는 문장
    (1, "부산 비 와?", "날씨", "부산", "오늘"),
    (2, "내일 제주 우산 필요해?", "날씨", "제주", "내일"),
    (3, "오늘 서울 추워?", "날씨", "서울", "오늘"),
    (4, "내일 해운대 날씨 어때", "날씨", "해운대", "내일"),
    (5, "강원 고성 날씨 어떻게 돼?", "날씨", "강원 고성", "오늘"),
    (6, "어쩔티비 부산 날씨", "날씨", "부산", "오늘"),
    (7, "내일 판교 기온 몇 도야", "날씨", "판교", "내일"),
    (8, "경기 광주 내일 비 오나", "날씨", "경기 광주", "내일"),
    (9, "부산 더워?", "날씨", "부산", "오늘"),
    (10, "제주도 날씨 알려줘", "날씨", "제주도", "오늘"),
    (11, "비 와?", "날씨", None, "오늘"),
    (12, "지금 바람 많이 불어?", "날씨", None, "오늘"),
    # B. 날씨 단어가 있지만 날씨가 아닌 문장
    (13, "비 오는 날 듣기 좋은 노래 추천해줘", "일반대화", None, None),
    (14, "날씨라는 단어를 영어로 뭐라고 해?", "일반대화", None, None),
    (15, "요즘 날씨가 사람 기분에 미치는 영향을 설명해줘", "일반대화", None, None),
    (16, "우산 브랜드 추천해줘", "일반대화", None, None),
    (17, "비빔밥 레시피 알려줘", "일반대화", None, None),
    # C. 완전히 다른 문장
    (18, "파이썬 리스트 정렬 어떻게 해?", "일반대화", None, None),
    (19, "부산에서 갈 만한 맛집 추천해줘", "일반대화", None, None),
    (20, "제주도 여행 3일 코스 짜줘", "일반대화", None, None),
    (21, "서울 인구가 몇 명이야?", "일반대화", None, None),
    (22, "오늘 기분이 너무 우울해", "일반대화", None, None),
    (23, "어제 쓴 메모 보여줘", "메모조회", None, None),
    # D. 경계 문장
    (24, "주말에 광주 눈 와?", "날씨", "광주", None),    # 날짜로 바꿀 수 없는 말은 null
    (25, "모레 서울 날씨", "날씨", "서울", "모레"),
    (26, "오늘 대전 미세먼지 어때?", ANY, None, None),     # 미세먼지는 지금 날씨 API에 없음: 의도는 채점 안 함
    (27, "겨울에 따뜻한 옷 뭐 입지?", "일반대화", None, None),
    # E. 새 문장 (28~47): 지시문을 고친 뒤 정확도가 부풀려졌는지 확인하는 검증용. 1~27번과 지시문 예시에는 없는 문장
    (28, "수원 내일 우산 챙겨야 돼?", "날씨", "수원", "내일"),
    (29, "울릉도 날씨 어때?", "날씨", "울릉도", "오늘"),
    (30, "오늘 전주 많이 추워?", "날씨", "전주", "오늘"),
    (31, "내일 강릉 눈 오나요?", "날씨", "강릉", "내일"),
    (32, "충북 청주 날씨 알려줘", "날씨", "충북 청주", "오늘"),
    (33, "서귀포 지금 비 오고 있어?", "날씨", "서귀포", "오늘"),
    (34, "밖에 우산 필요해?", "날씨", None, "오늘"),
    (35, "글피 부산 날씨", "날씨", "부산", None),
    (36, "다음 주 월요일 서울 비 와?", "날씨", "서울", None),
    (37, "내일 대구 최고 기온이 몇 도야?", "날씨", "대구", "내일"),
    (38, "혹시 내일 포항 날씨 알 수 있을까", "날씨", "포항", "내일"),
    (39, "비 맞으면 감기 걸려?", "일반대화", None, None),
    (40, "우산 접는 법 알려줘", "일반대화", None, None),
    (41, "날씨 앱 추천해줘", "일반대화", None, None),
    (42, "내일 회의 자료 요약해줘", "일반대화", None, None),
    (43, "경기도 맛집 알려줘", "일반대화", None, None),
    (44, "비행기 표 싸게 사는 법", "일반대화", None, None),
    (45, "눈사람 만드는 방법 알려줘", "일반대화", None, None),
    (46, "태풍이 왜 생겨?", "일반대화", None, None),
    (47, "서울 날씨를 알려주는 코드를 파이썬으로 짜줘", "일반대화", None, None),
    # 날짜 지정 (기대 날짜 DATE = 어제·오늘·내일·모레가 아닌 'YYYY-MM-DD')
    (48, "10월 30일 부산 날씨 어때?", "날씨", "부산", DATE),
    (49, "서울 28일에 비 와?", "날씨", "서울", DATE),
    (50, "모레 대구 우산 필요해?", "날씨", "대구", "모레"),
    (51, "어제 광주 비 왔어?", "날씨", "광주", "어제"),
    (52, "낼 서울 비 와?", "날씨", "서울", "내일"),
    # 메모 (53~): 저장 / 조회 / 삭제 / 요약 / 메모와 헷갈리는 일반대화
    (53, "내일 치과 3시 메모해줘", "메모저장", "내일 치과 3시", None),
    (54, "우유랑 계란 사야 해 적어 놔", "메모저장", "우유랑 계란 사야 해", None),
    (55, "엄마 생일 11월 3일 기억해줘", "메모저장", "엄마 생일 11월 3일", None),
    (56, "메모: 세탁소 찾아가기", "메모저장", "세탁소 찾아가기", None),
    (57, "비 온다고 적어 둬", "메모저장", "비 온다고", None),
    (58, "내 메모 보여줘", "메모조회", None, None),
    (59, "치과 관련 메모 찾아줘", "메모조회", "치과", None),
    (60, "저장해 둔 메모 목록 알려줘", "메모조회", None, None),
    (61, "3번 메모 지워줘", "메모삭제", 3, None),
    (62, "방금 쓴 메모 삭제해줘", "메모삭제", None, None),
    (63, "이번 주 메모 정리해줘", "메모요약", None, None),
    (64, "메모한 거 요약해줘", "메모요약", None, None),
    (65, "메모 앱 추천해줘", "일반대화", None, None),
    (66, "메모리가 뭐야?", "일반대화", None, None),
    (67, "회의록 쓰는 법 알려줘", "일반대화", None, None),
    (68, "메모 잘 하는 방법 알려줘", "일반대화", None, None),
    # 일정 (69~): 등록 / 조회 / 메모와 헷갈리는 문장 / 일정과 헷갈리는 일반대화
    (69, "내일 오후 3시 치과 일정 잡아줘", "일정등록", "내일 오후 3시 치과", None),
    (70, "모레 9시 회의 등록해줘", "일정등록", "모레 9시 회의", None),
    (71, "10월 30일 저녁 7시 친구 약속 일정 넣어줘", "일정등록", "10월 30일 저녁 7시 친구 약속", None),
    (72, "내일 15시에 미용실 가야 하니까 알려줘", "일정등록", "내일 15시에 미용실 가야 하니까", None),
    (73, "오늘 오후 6시 운동 일정 추가해줘", "일정등록", "오늘 오후 6시 운동", None),
    (74, "내 일정 보여줘", "일정조회", None, None),
    (75, "앞으로 무슨 일정 있어?", "일정조회", None, None),
    (76, "등록한 약속 목록 알려줘", "일정조회", None, None),
    (77, "다음 일정이 뭐야?", "일정조회", None, None),
    (78, "내일 3시 병원 메모해줘", "메모저장", "내일 3시 병원", None),
    (79, "일정 관리 앱 추천해줘", "일반대화", None, None),
    (80, "약속 시간 잘 지키는 법 알려줘", "일반대화", None, None),
    (81, "여행 일정 짜줘", "일반대화", None, None),
    (82, "알람 맞추는 법 알려줘", "일반대화", None, None),
    # 상대 시간과 알림 (83~): 일정과 같은 의도로 읽어야 한다
    (83, "10분 뒤에 빨래 알려줘", "일정등록", "10분 뒤에 빨래", None),
    (84, "1분뒤에 qa 테스트 알려줘", "일정등록", "1분뒤에 qa 테스트", None),
    (85, "2시간 후에 회의 준비 알림 잡아줘", "일정등록", "2시간 후에 회의 준비", None),
    (86, "1시간 30분 뒤에 약 먹으라고 알려줘", "일정등록", "1시간 30분 뒤에 약 먹으라고", None),
    (87, "오후 3시 치과 알림 잡아줘", "일정등록", "오후 3시 치과", None),
    (88, "등록한 알림 보여줘", "일정조회", None, None),
    (89, "10분 뒤에 뭐 하면 좋을까?", "일반대화", None, None),
    (90, "알림 끄는 방법 알려줘", "일반대화", None, None),
    # 모델이 날짜를 틀리거나 빼먹어도 코드가 원문에서 되살리는 문장 (91~)
    (91, "낼 부산 우산 필요해?", "날씨", "부산", "내일"),
    (92, "모레 대전 비 와?", "날씨", "대전", "모레"),
    (93, "11월 5일 저녁 7시 동창회 일정 넣어줘", "일정등록", "11월 5일 저녁 7시 동창회", None),
    (94, "내일 오후 2시 치과 일정 잡아줘", "일정등록", "내일 오후 2시 치과", None),
    # 일정 삭제와 요일, 약속 (95~)
    (95, "1번 일정 지워줘", "일정삭제", 1, None),
    (96, "2번 알림 삭제해줘", "일정삭제", 2, None),
    (97, "3번 약속 없애줘", "일정삭제", 3, None),
    (98, "일정 1번 삭제해", "일정삭제", 1, None),
    (99, "일정 지워줘", "일정삭제", None, None),
    (100, "메모 지워줘", "메모삭제", None, None),
    (101, "이번주 금요일 15시 치과 일정 잡아줘", "일정등록", "이번주 금요일 15시 치과", None),
    (102, "다음 주 월요일 오전 10시 회의 등록해줘", "일정등록", "다음 주 월요일 오전 10시 회의", None),
    (103, "금요일 3시 치과 알림 잡아줘", "일정등록", "금요일 3시 치과", None),
    (104, "내일 오후 3시 약속 잡아줘", "일정등록", "내일 오후 3시 약속", None),
    (105, "약속 뭐 있어?", "일정조회", None, None),
    (106, "1번 약속 다 지워줘", "일정삭제", 1, None),
]

# 결과를 나누어 보는 세트 (이름, 번호 범위)
SETS = [("시험 문장 1~27", 1, 27), ("새 문장 28~52 (검증용)", 28, 52), ("메모 문장 53~68 (검증용)", 53, 68),
        ("일정 문장 69~82 (검증용)", 69, 82),
        ("상대 시간·알림 문장 83~90 (검증용)", 83, 90),
        ("날짜 되살리기 문장 91~94 (검증용)", 91, 94),
        ("일정 삭제·요일·약속 문장 95~106 (검증용)", 95, 106)]


def rule_classify(text):
    """지금 비서의 코드 규칙: '날씨'가 들어간 문장이면 날씨, 지역·날짜는 parse_query 가 뽑는다."""
    q = weather.parse_query(text)
    if not q:
        return {"의도": "일반대화", "지역": None, "날짜": None}
    return {"의도": "날씨", "지역": q[0], "날짜": q[1]}


def score(got, case):
    """(의도 채점 대상인가, 의도 맞음, 슬롯 채점 대상인가, 슬롯 맞음)"""
    _, _, intent, region, date = case
    if got is None:
        return intent != ANY, False, intent in SLOT_INTENTS, False
    intent_scored = intent != ANY
    intent_ok = (not intent_scored) or got["의도"] == intent
    slot_scored = intent in SLOT_INTENTS
    if intent == "날씨":
        got_date = got["날짜"]
        date_ok = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", got_date or "")) if date == DATE else got_date == date
        slot_ok = got["의도"] == "날씨" and got["지역"] == region and date_ok
    else:  # 메모: 기대 내용과 같은가
        slot_ok = slot_scored and got["의도"] == intent and got.get("내용") == region
    return intent_scored, intent_ok, slot_scored, slot_ok


def brief(r):
    if r is None:
        return "형식 오류"
    if r["의도"] in classifier.MEMO_INTENTS or r["의도"] in classifier.SCHEDULE_INTENTS:
        return f"{r['의도']}/{r.get('내용')}"
    if r["의도"] != "날씨":
        return "일반대화"
    return f"날씨/{r['지역']}/{r['날짜']}"


def expected_text(case):
    _, _, intent, region, date = case
    if intent == ANY:
        return "(의도 무관)"
    if intent == "날씨":
        return f"날씨/{region}/{date}"
    return f"{intent}/{region}" if intent in SLOT_INTENTS else intent


class Tally:
    def __init__(self, name):
        self.name, self.rows = name, []
        self.i_ok = self.i_n = self.s_ok = self.s_n = self.fmt_ok = 0
        self.times, self.first = [], None

    def add(self, case, got, secs=None):
        i_scored, i_ok, s_scored, s_ok = score(got, case)
        self.i_n += i_scored
        self.i_ok += i_scored and i_ok
        self.s_n += s_scored
        self.s_ok += s_ok
        self.fmt_ok += got is not None
        if secs is not None:
            self.times.append(secs)
        self.rows.append((case, got, (i_ok if i_scored else True) and (s_ok if s_scored else True)))

    def summary(self):
        n = len(self.rows)
        t = f"{sum(self.times) / len(self.times):.1f}초" if self.times else "-"
        first = f"{self.first:.1f}초" if self.first is not None else "-"
        return (f"| {self.name} | {self.i_ok}/{self.i_n} | {self.s_ok}/{self.s_n} | {self.fmt_ok}/{n} | {t} | {first} |")


def run_model(model):
    """(문장별 결과 [(case, got, secs)], 첫 호출 시간)을 돌려준다. 접속하지 못하면 None."""
    try:  # 첫 호출은 모델을 올리는 시간이 섞이므로 따로 잰다
        _, first = classifier.ask_model("안녕", model)
    except classifier.ClassifierError as e:
        print(f"[{model}] 건너뜀: {e}\n  (서버 Ollama에 닿는지, 모델 이름이 `ollama list`와 같은지 확인하세요. 맥에서는 .env의 OLLAMA_URL 또는 SSH 터널이 필요합니다)")
        return None
    results = []
    for case in CASES:
        try:
            got, _, secs = classifier.classify(case[1], model)
        except classifier.ClassifierError as e:
            print(f"[{model}] #{case[0]} 오류: {e}")
            got, secs = None, None
        results.append((case, got, secs))
    return results, first


def build_tally(name, results, lo, hi, first=None):
    t = Tally(name)
    t.first = first
    for case, got, secs in results:
        if lo <= case[0] <= hi:
            t.add(case, got, secs)
    return t


def main():
    ap = argparse.ArgumentParser(description="분류기 시험")
    ap.add_argument("--models", default=DEFAULT_MODELS, help="쉼표로 구분한 모델 이름")
    ap.add_argument("--all", action="store_true", help="문장별 결과 전부 출력")
    ap.add_argument("--no-rule", action="store_true", help="코드 규칙 비교 빼기")
    ap.add_argument("--think", choices=("on", "off"), help="생각 기능이 있는 모델의 생각 켜기·끄기 (주지 않으면 Ollama 기본값)")
    args = ap.parse_args()

    runs = []  # (이름, 문장별 결과, 첫 호출 시간)
    if not args.no_rule:
        runs.append(("코드 규칙", [(c, rule_classify(c[1]), None) for c in CASES], None))
    classifier.set_think(args.think)
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        r = run_model(model)
        if r:
            runs.append((model, r[0], r[1]))

    for title, lo, hi in SETS:
        subset = [c for c in CASES if lo <= c[0] <= hi]
        print(f"\n## {title}: 문장 {len(subset)}개 (의도 채점 {sum(1 for c in subset if c[2] != ANY)}개, "
              f"슬롯 채점 {sum(1 for c in subset if c[2] in SLOT_INTENTS)}개)\n")
        print("| 방식 | 의도 정확 | 슬롯(지역·날짜·내용) 정확 | JSON 형식 | 평균 응답 | 첫 호출(모델 올림) |")
        print("|---|---|---|---|---|---|")
        for name, results, first in runs:
            print(build_tally(name, results, lo, hi, first).summary())

    for name, results, first in runs:
        t = build_tally(name, results, 1, len(CASES))
        shown = [r for r in t.rows if args.all or not r[2]]
        print(f"\n[{name}] {'전체' if args.all else '틀린 문장'} {len(shown)}개")
        for case, got, ok in shown:
            print(f"  {'O' if ok else 'X'} #{case[0]} {case[1]}\n      기대 {expected_text(case)} / 결과 {brief(got)}")


if __name__ == "__main__":
    main()
