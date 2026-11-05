#!/usr/bin/env python3
"""staged diff -> 코드 리뷰 (서버 Ollama 사용). 읽기만 하고 파일·커밋은 건드리지 않는다.

[임시용] 진짜 리뷰는 Copilot이나 Claude 같은 큰 모델에 맡긴다. 이 도구는 참고용이다.
알려진 결점 (2026-10-17 시험 결과, qwen2.5-coder:7b):
- 문제를 거의 못 찾는다: 빈 리스트로 나누기, 인자 sort(), open 후 닫지 않기를 짚지 못했다.
- 틀린 지적을 한다: 같은 변경에서 맞는 코드를 고치라고 했고, 이유 설명도 틀렸다.
- 결과가 흔들린다: temperature를 낮춰도 입력에 따라 지적이 나오거나 안 나온다.
- 믿을 만한 것은 코드가 직접 하는 문법 검사(.py)뿐이다.

동작:
- 코드 파일(.py 등)만 리뷰한다. 문서·설정 파일은 건너뛴다.
- 파일마다 따로 리뷰한다 (여러 파일을 섞으면 7b가 뭉뚱그리는 경향이 있어서).
사용: python tools/review.py
"""
import os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import commit_msg as cm          # 주소 읽기(.env), 파일 분류, Ollama 호출을 재사용

MAX_FILE_CHARS = 9000            # 파일 하나의 diff 글자 수 한도

CHECKLIST = [
    "빈 입력(빈 리스트·빈 문자열·0·None)이 들어오면 오류가 나거나 틀린 값을 돌려주는가? (예: 빈 리스트로 나누기)",
    "파일·연결 같은 자원을 열고 닫지 않거나, 중간에 오류가 나면 닫히지 않는가? (예: with 없이 open)",
    "받은 인자(리스트·딕셔너리)를 직접 바꿔서 호출한 쪽의 값이 달라지는가? (예: 인자에 sort())",
    "변환이 실패할 수 있는 입력(int(문자열) 등)에 대한 예외 처리가 없는가?",
    "지운 줄 때문에 남은 코드가 깨지는가? (예: 함수 본문이 사라짐, 부르는 곳이 있는 함수가 사라짐)",
    "비밀 값 노출, 파일 삭제 같은 위험한 동작이 있는가?",
]

PROMPT = """너는 꼼꼼한 코드 리뷰어다. 아래 git diff(파일 하나)를 한국어로 리뷰해라.
`+` 줄은 새로 넣은 코드, `-` 줄은 지운 코드, 나머지는 주변 코드다.

아래 점검 항목을 **하나씩 모두** 확인해라. 해당하는 코드가 있으면 지적하고, 해당 없으면 그 항목은 쓰지 마라.
{checklist}

규칙:
- 지적은 한 줄 형식: [심각도: 높음/중간/낮음] "문제가 되는 코드 한 줄을 그대로 인용" — 왜 문제인지, 어떻게 고칠지
- 줄 번호는 쓰지 말고 코드를 인용해라. diff에 보이지 않는 코드를 지어내지 마라.
- 항목 중 해당하는 게 하나도 없을 때만 "지적 없음"이라고 써라.
- 설명이나 인사말 없이 지적 목록만 출력한다.

파일: {path} ({status})
{facts}
{diff}"""

def syntax_check(path):
    """스테이징된 .py 내용을 컴파일해서 문법 오류를 확인한다. 코드가 확인하는 사실이다. -> (통과?, 설명)"""
    if not path.endswith(".py"):
        return True, ""
    src = subprocess.run(["git", "show", f":{path}"], capture_output=True, text=True).stdout
    try:
        compile(src, path, "exec")
        return True, "문법 검사: 통과"
    except SyntaxError as e:
        return False, f"문법 검사: 실패 ({e.lineno}번째 줄 근처: {e.msg})"

def file_diff(path):
    d = subprocess.run(["git", "diff", "--staged", "-U3", "--", path],
                       capture_output=True, text=True).stdout
    return (d[:MAX_FILE_CHARS], True) if len(d) > MAX_FILE_CHARS else (d, False)

def main():
    if not cm.ADDRESS:
        sys.exit(f".env에 {cm.ENV_KEY}=http://서버주소 한 줄을 적으세요.")
    files = cm.staged_files()
    if not files:
        sys.exit("staged 변경이 없습니다. git add 먼저 하세요.")
    code = [(s, p) for s, p in files if cm.kind_of(p) == "code" and s != "D"]
    skipped = [p for s, p in files if (s, p) not in code]
    if not code:
        sys.exit("리뷰할 코드 파일이 없습니다 (문서·설정만 스테이징됨).")
    label = {"A": "신규", "M": "수정", "R": "수정"}
    try:
        for status, path in code:
            diff, cut = file_diff(path)
            print(f"=== {path} ({label.get(status, '수정')})" + (" [diff가 길어 앞부분만 검토]" if cut else ""))
            ok, note = syntax_check(path)
            if note:
                print(note + ("" if ok else "  [확정, 심각도: 높음]"))
            facts = "" if ok else f"(이미 확인된 사실: 이 파일은 {note}. 이 오류가 어디서 왔는지 diff에서 찾아 지적하고, 문법 오류와 별개로 위 점검 항목도 모두 확인해라.)\n"
            checklist = "\n".join(f"{i}. {q}" for i, q in enumerate(CHECKLIST, 1))
            prompt = PROMPT.format(checklist=checklist, path=path, status=label.get(status, "수정"), facts=facts, diff=diff)
            result = cm.ask(prompt, temperature=0).strip()      # 0: 같은 입력이면 최대한 같은 결과
            if not ok and result.startswith("지적 없음"):
                result = "AI 지적 없음 (하지만 위 문법 오류는 확정이니 먼저 고치세요)"
            print(result + "\n")
    except OSError as e:
        sys.exit(f"연결 실패: {e}")
    if skipped:
        print("[건너뜀: 코드가 아닌 파일] " + ", ".join(skipped))
    print("[임시용] 7b 리뷰는 문제를 거의 못 찾고 틀린 지적도 합니다. 믿을 수 있는 건 '문법 검사'뿐입니다.\n"
          "         진짜 리뷰는 Copilot이나 Claude에 맡기세요. (자세한 결점: tools/review.py 맨 위 설명)")

if __name__ == "__main__":
    main()
