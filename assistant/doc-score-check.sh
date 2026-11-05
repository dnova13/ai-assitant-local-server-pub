#!/usr/bin/env bash
# 질문의 점수와 모델 답을 포트폴리오·전체 범위에서 비교한다. 저장은 하지 않는다. 모델을 차례로 부르니 조금 걸린다
#   ./doc-score-check.sh                        기본 질문 2개
#   ./doc-score-check.sh "질문 1" "질문 2"      원하는 질문
set -euo pipefail
cd "$(dirname "$0")"

VENV=../.venv  # 저장소 맨 위
if [ ! -f "$VENV/bin/activate" ]; then
  echo "❌ 가상환경이 없습니다. 먼저 ./setup.sh 를 실행하세요." >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

exec python - "$@" <<'PY'
import sys

from clients import llm, vector_store
from core import config
from features import documents

questions = sys.argv[1:] or ["기술 스택이 뭐야?", "기술 스택이 어떻게 돼?"]
cfg = documents.load_config()
store = vector_store.open_store()
all_kbs = documents.all_knowledge(cfg)
port = next(c for c in cfg["categories"] if "포트폴리오" in c["aliases"])["knowledge"]
for scope, kbs in (("포트폴리오", port), ("전체", all_kbs)):
    for q in questions:
        hits = store.search(llm.embed([q])[0], kbs, config.DOC_TOP_K)
        good = [h for h in hits if h["score"] >= cfg["min_score"]]
        print(f"[{scope}] {q}")
        print("  점수:", " ".join(f"{h['score']:.3f}({h['file']})" for h in hits))
        if good:
            reply = llm.chat([{"role": "user", "content": documents.build_question(q, good)}], system=documents.ANSWER_PROMPT, max_tokens=config.DOC_MAX_TOKENS)
            print("  모델 답:", reply[:60].replace("\n", " "))
        else:
            print("  기준 미달: 모델 호출 없음")
PY
