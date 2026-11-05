#!/usr/bin/env python3
"""staged diff -> 커밋 메시지 초안 (서버 Ollama 사용). 커밋은 하지 않는다.

흐름은 코드가 정하고, AI는 문장만 쓴다.
출력 형식 (종류별로 한 줄, 변경마다 괄호에 파일명):
  feat: a 내용(a.py), b 내용(b.py)
  docs: c 내용(c.md)
종류는 코드가 정한다: 코드 파일 -> 새 파일만이면 feat, 기존 수정이 있으면 feat/fix/refactor 중 모델 선택,
문서 -> docs, 설정 -> chore.

알려진 결점 (2026-10-17 시험 결과, qwen2.5-coder:7b):
- 내용 문장이 막연할 때가 있다: 한 파일에 변경이 여러 개면 "업데이트", "형식 정의 및 업데이트"처럼 뭉뚱그린다.
  종류(feat/docs)와 파일명은 코드가 정해서 믿을 수 있지만, 내용은 사람이 한 번 다듬어야 한다.
- 3b는 쓰지 않는다: diff에 없는 기능(예: 없는 "코드 리뷰 지원")을 지어냈다.
- 기존 코드 수정의 fix / refactor 구분은 시험하지 못했다 (feat만 확인).
- diff가 길면 큰 파일부터 잘라서 보낸다. 잘린 내용은 반영되지 않을 수 있다.
- e(편집기에서 고쳐서 커밋)는 실제로 시험하지 못했다.
- 항상 사람이 확인하고 커밋한다 (초안일 뿐이다).

사용: python tools/commit_msg.py          초안을 보여 주고 커밋할지 묻는다 (엔터만 치면 취소)
      python tools/commit_msg.py --print  초안만 출력하고 끝낸다
"""
import json, os, re, subprocess, sys, tempfile, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_KEY = "AI_SERVER_ADDRESS"        # .env에서 읽는 값은 이 키 하나뿐

def read_address(path):
    """저장소 루트 .env에서 AI_SERVER_ADDRESS 한 줄만 읽는다. 다른 줄은 보지 않는다."""
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith(ENV_KEY + "="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""

def base_url(value):
    """'http://호스트', '호스트:포트', '호스트' 어느 형태든 http://호스트:11434 로 맞춘다."""
    host = value.split("://")[-1].rstrip("/").split(":")[0]
    return f"http://{host}:11434"

ADDRESS = read_address(os.path.join(ROOT, ".env"))
MODEL = os.environ.get("COMMIT_MODEL", "qwen2.5-coder:7b")   # 3b는 없는 내용을 지어내서 기본값에서 제외
MAX_CHARS = 14000                                # 그룹별 diff 글자 수 한도 (넘으면 큰 파일부터 자름)

DOC_EXT = {".md", ".txt", ".rst"}
CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}
CONFIG_NAMES = {".gitignore", ".gitattributes", ".env.example", ".editorconfig"}
OTHER_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf"}   # 그림·첨부 -> 문서 그룹으로 취급

TYPE_HELP = {
    "feat": "새 기능·동작이 늘었다 (새 함수, 새 옵션 등)",
    "fix": "버그나 오류 처리를 고쳤다",
    "refactor": "동작은 같고 구조·이름·정리만 바뀌었다",
}

PROMPT = """아래 git diff를 보고 커밋 메시지를 **한 줄**로 한국어로 써라.
이번 대상 파일: {files}
(신규 = 새로 만든 파일, 수정 = 기존 파일을 고침, 삭제 = 파일을 지움)
규칙:
{type_rule}
- 형식: `종류: 내용(파일명), 내용(파일명)` — 변경 하나마다 내용 뒤에 괄호로 파일명(경로 없이 이름만)을 붙이고, 변경끼리는 쉼표로 잇는다.
- 같은 변경이 여러 파일이면 괄호에 파일명을 쉼표로 함께 쓴다. 대상 파일은 하나도 빠뜨리지 마라.
- 내용은 명사형으로 쓴다 (예: "추가", "정리", "표기 통일"). "~했습니다" 체는 쓰지 않는다.
- 신규 파일은 파일 내용을 읽고 무엇에 관한 문서·코드인지 주제를 밝혀 "OO 문서 추가", "OO 기능 추가"로 쓴다. 수정 파일의 변경과 한 항목으로 합치지 마라.
- "수정", "업데이트", "변경"만 쓰지 마라. 무엇을 어떻게 바꿨는지 드러내라.
- diff에 실제로 있는 내용만 쓴다. 없는 기능이나 파일을 지어내지 마라.
- 줄바꿈, 코드 블록(```), 설명 없이 그 한 줄만 출력한다.

예시 (형식만 참고하고 내용은 베끼지 마라):
feat: 빈 입력 처리 추가(sample.py), 종류 자동 판단 추가(commit_msg.py)
docs: README 설치와 실행 안내 정리
chore: 로그 폴더 무시 규칙 추가(.gitignore)

{diff}"""

def type_rule(allowed):
    if len(allowed) == 1:
        return f"- 종류는 `{allowed[0]}`로 이미 정해졌으니 바꾸지 마라."
    lines = "\n".join(f"  - {t}: {TYPE_HELP[t]}" for t in allowed)
    return "- 종류는 아래 단어 중 하나를 그대로 쓴다.\n" + lines

def staged_files():
    """[(상태 글자, 경로)] 스테이징된 파일 목록. 이름 바꾸기는 새 경로를 쓴다."""
    out = subprocess.run(["git", "diff", "--staged", "--name-status"],
                         capture_output=True, text=True).stdout
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append((parts[0][0], parts[-1]))
    return files

def kind_of(path):
    name, ext = os.path.basename(path), os.path.splitext(path)[1].lower()
    if ext in DOC_EXT or ext in OTHER_EXT:
        return "docs"
    if name in CONFIG_NAMES or ext in CONFIG_EXT:
        return "config"
    return "code"

def make_groups(files):
    """[(허용 종류 목록, [(상태, 경로)])] 코드 -> 문서 -> 설정 순. 종류가 정해지면 1개로 고정."""
    by = {"code": [], "docs": [], "config": []}
    for status, path in files:
        by[kind_of(path)].append((status, path))
    groups = []
    if by["code"]:
        all_new = all(s == "A" for s, _ in by["code"])
        groups.append((["feat"] if all_new else ["feat", "fix", "refactor"], by["code"]))
    if by["docs"]:
        groups.append((["docs"], by["docs"]))
    if by["config"]:
        groups.append((["chore"], by["config"]))
    return groups

def clean(text, allowed, group_files):
    """한 줄만 남기고, 코드 블록 표시를 지우고, 종류를 바로잡고, 빠진 파일명을 덧붙인다."""
    lines = [l.strip() for l in text.strip().splitlines()
             if l.strip() and not l.strip().startswith("```")]
    line = lines[0] if lines else ""
    m = re.match(r"^(\w+)\s*:\s*(.*)$", line)
    if m:
        kind, rest = m.group(1), m.group(2)
        if kind not in allowed:
            kind = allowed[0]
    else:
        kind, rest = allowed[0], line
    missing = [os.path.basename(p) for p in group_files if os.path.basename(p) not in rest]
    if missing:
        rest += (", " if rest else "") + "기타(" + ", ".join(missing) + ")"
    return f"{kind}: {rest}"

def ask(prompt, temperature=0.2):
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False,
                       "options": {"temperature": temperature, "num_ctx": 8192}}).encode()
    req = urllib.request.Request(f"{base_url(ADDRESS)}/api/generate", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["response"]

def build_diff(paths):
    """변경된 줄만(-U0) 모아서 한도 안에 맞춘다. 작은 파일은 전부 쓰고, 남는 글자 수를 큰 파일에 나눠준다."""
    diffs = {p: subprocess.run(["git", "diff", "--staged", "-U0", "--", p],
                               capture_output=True, text=True).stdout for p in paths}
    budget, left = {}, MAX_CHARS
    for i, p in enumerate(sorted(paths, key=lambda x: len(diffs[x]))):
        share = left // (len(paths) - i)
        budget[p] = min(len(diffs[p]), share)
        left -= budget[p]
    cut = [p for p in paths if budget[p] < len(diffs[p])]
    return "\n".join(diffs[p][:budget[p]] for p in paths), cut

def main():
    if not ADDRESS:
        sys.exit(f".env에 {ENV_KEY}=http://서버주소 한 줄을 적으세요.")
    files = staged_files()
    if not files:
        sys.exit("staged 변경이 없습니다. git add 먼저 하세요.")
    out, cut_all = [], []
    try:
        for allowed, gfiles in make_groups(files):
            paths = [p for _, p in gfiles]
            diff, cut = build_diff(paths)
            cut_all += cut
            label = {"A": "신규", "M": "수정", "D": "삭제", "R": "수정"}
            names = ", ".join(f"{os.path.basename(p)}({label.get(st, '수정')})" for st, p in gfiles)
            prompt = PROMPT.format(files=names,
                                   type_rule=type_rule(allowed), diff=diff)
            out.append(clean(ask(prompt), allowed, paths))
    except OSError as e:
        sys.exit(f"연결 실패: {e}")
    message = "\n\n".join(out)   # 빈 줄로 구분: 첫 줄만 제목이 되고, git log --oneline에서 한 줄로 합쳐지지 않게
    print(message)
    if cut_all:
        print(f"[참고: diff가 길어 일부만 사용: {', '.join(cut_all)}]")
    if "--print" in sys.argv or not sys.stdin.isatty():
        return                                   # 출력만 (옵션 --print, 또는 터미널이 아닐 때)
    answer = input("\n[y] 이대로 커밋  [e] 편집기에서 고쳐서 커밋  [엔터] 취소 > ").strip().lower()
    if answer not in ("y", "e"):
        print("취소했습니다. 커밋하지 않았습니다.")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(message + "\n")
    try:
        cmd = ["git", "commit", "-F", f.name] + (["-e"] if answer == "e" else [])
        sys.exit(subprocess.run(cmd).returncode)
    finally:
        os.unlink(f.name)

if __name__ == "__main__":
    main()
