#!/usr/bin/env bash
# 지식 기반 ID·이름·파일 수 보기 (documents.json 에 ID를 적을 때). 어느 폴더에서 실행해도 된다
set -euo pipefail
cd "$(dirname "$0")"

VENV=../.venv  # 저장소 맨 위
if [ ! -f "$VENV/bin/activate" ]; then
  echo "❌ 가상환경이 없습니다. 먼저 ./setup.sh 를 실행하세요." >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

exec python -m scripts.doc_index --list
