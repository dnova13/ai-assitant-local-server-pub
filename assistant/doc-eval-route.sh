#!/usr/bin/env bash
# 입구 분류 시험: 문장이 파일·문서·평소 중 어디로 가는지 본다 (답은 만들지 않는다). 모델을 차례로 부르니 조금 걸린다
set -euo pipefail
cd "$(dirname "$0")"

VENV=../.venv  # 저장소 맨 위
if [ ! -f "$VENV/bin/activate" ]; then
  echo "❌ 가상환경이 없습니다. 먼저 ./setup.sh 를 실행하세요." >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

exec python -m scripts.doc_eval --route
