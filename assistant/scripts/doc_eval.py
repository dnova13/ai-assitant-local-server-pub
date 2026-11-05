"""문서 검색 시험 (실제 서버 필요).
  python -m scripts.doc_eval --route     # 입구 거르기 + 문서 분류기: 5_1_4 12절 시험 문장이 파일/문서/평소 중 어디로 가는지 (Ollama 필요)
  python -m scripts.doc_eval --scores    # 문서 안·밖 질문의 최고 점수 (색인 후, 임베딩과 검색만 해서 답은 만들지 않음)
점수 기준(min_score)은 --scores 결과를 보고 documents.json 에 적는다. 문서 안 질문은 지식 기반에 맞춰 고쳐 쓴다.
~~--scores 는 워크스페이스 모델에 물어 답과 거절 여부도 봤다~~ (2026-10-28 구조 변경, 5_1_4 17절)"""
import argparse
import time

from features import documents, files

ROUTE = [  # (문장, 기대 갈래, 기대 동작 또는 카테고리 별칭)
    ("문서 폴더에 2026 폴더 만들어줘", "파일", "file.mkdir"),
    ("문서에 있는 보고서.pdf 옮겨줘", "파일", "file.move"),
    ("문서 기술문서 도커 설정 찾아줘", "문서", "기술문서"),
    ("문서 기술 문서 도커 설정 찾아줘", "문서", "기술문서"),
    ("!문서 기술 스택이 뭐야?", "문서", None),
    ("문서 환불 규정 찾아줘", "문서", None),
    ("문서 작성하는 법 알려줘", "문서", None),
    ("문서화가 왜 중요해?", "평소", None),
    ("기술 문서에서 도커 설정 찾아줘", "평소", None),
    ("!문서 포트폴리오 이력서", "문서", "포트폴리오"),
    ("!문서 선착순 쿠폰 프로젝트는 어떤 기술을 썼어?", "문서", None),
    ("!문서 계약서 A 요약해줘", "문서", "요약"),
    ("!문서 카테고리", "문서", "카테고리"),
    ("!문서 목록", "문서", "목록"),
    ("!문서 어떤 문서들이 있어?", "문서", None),
    ("!문서 내가 한 개인 프로젝트는 뭐야?", "문서", None),
]
IN_DOC = ["디스코드 봇 systemd Restart 설정은?", "Top K를 몇으로 바꿨어?", "서버 서비스 제어를 켜려면 .env에서 뭘 바꿔?",
          "대화용과 코딩용 모델은 각각 뭐야?", "디스코드 봇 토큰은 어디서 발급해?", "RAG 관련성 임계값은 몇으로 정했어?"]
OUT_DOC = ["피자 도우 반죽하는 법", "오늘 기분이 어때?", "에펠탑 높이는 몇 미터야?", "고양이 사료 추천해줘", "자바스크립트 배열 정렬하는 법"]


def route(text):
    """(갈래, 동작/카테고리) — 비서와 같은 순서: !문서 -> 파일 단서 -> 맨 앞 '문서' -> 평소."""
    if text.startswith("!문서"):
        body = text[3:].strip()
    else:
        if files.looks_like_file(text):
            f = files.classify(text)
            if f and f["동작"] != "아님":
                return "파일", f["동작"]
        if not documents.starts_with_document(text):
            return "평소", None
        body = documents.strip_start(text)
    if body in ("카테고리", "목록"):  # 모델에 묻지 않는다
        return "문서", body
    cfg = documents.load_config()
    cat, question = documents.split_category(cfg, body)
    r = documents.classify(question)
    if r and r["동작"] == "요약":
        return "문서", r["동작"]
    return "문서", (cat["aliases"] if cat else None)


def matches(got, want_kind, want):
    """갈래와 동작이 같은가. 카테고리는 기대한 별칭이 그 카테고리의 별칭 중 하나인지 본다 (순서·공백 무관)."""
    kind, value = got
    if isinstance(value, list):
        return kind == want_kind and want is not None and "".join(want.split()) in ["".join(a.split()) for a in value]
    return got == (want_kind, want)


def run_route():
    ok = 0
    for text, want_kind, want in ROUTE:
        got = route(text)
        good = matches(got, want_kind, want)
        ok += good
        print(("OK  " if good else "X   ") + f"{text} -> {got}" + ("" if good else f" (기대 {(want_kind, want)})"))
    print(f"\n갈래·동작 정확 {ok}/{len(ROUTE)}")


def run_scores():
    from clients import llm, vector_store
    cfg = documents.load_config()
    kbs = documents.all_knowledge(cfg)
    store = vector_store.open_store()
    for kind, qs in (("안", IN_DOC), ("밖", OUT_DOC)):
        for q in qs:
            t = time.time()
            try:
                hits = store.search(llm.embed([q])[0], kbs, 3)
            except (llm.LLMError, vector_store.StoreError) as e:  # 서버에 요청이 쌓이지 않게 바로 멈춘다
                print(f"[{kind}] 오류 {e} | {q}\n오류가 나서 멈춥니다. 서버 상태를 확인하세요 (ollama ps, journalctl -u ollama).")
                return
            top = " ".join(f"{h['score']:.3f}({h['file']})" for h in hits) or "조각 없음"
            print(f"[{kind}] {top} {time.time() - t:.1f}초 | {q}")
    print(f"\n지금 기준 min_score {cfg['min_score']}: 안 질문의 최고 점수는 모두 넘고, 밖 질문은 모두 못 넘어야 한다.")


def main():
    ap = argparse.ArgumentParser(description="문서 검색 시험")
    ap.add_argument("--route", action="store_true")
    ap.add_argument("--scores", action="store_true")
    a = ap.parse_args()
    if a.route or not a.scores:
        run_route()
    if a.scores:
        run_scores()


if __name__ == "__main__":
    main()
