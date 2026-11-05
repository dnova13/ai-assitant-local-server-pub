#!/usr/bin/env bash
# documents.json 의 min_score 보기·바꾸기. 재시작은 필요 없다
#   ./doc-min-score.sh        지금 값 보기
#   ./doc-min-score.sh 0.45   값 바꾸기
set -euo pipefail
cd "$(dirname "$0")"

FILE=documents.json
if ! grep -qE '"min_score": *[0-9.]+' "$FILE"; then
  echo "❌ $FILE 에서 min_score 를 찾지 못했습니다." >&2
  exit 1
fi
if [ $# -eq 0 ]; then
  grep -oE '"min_score": *[0-9.]+' "$FILE"
  exit 0
fi
if ! [[ "$1" =~ ^(0(\.[0-9]+)?|1(\.0+)?)$ ]]; then
  echo "❌ 값은 0 에서 1 사이 숫자여야 합니다. 예: ./doc-min-score.sh 0.45" >&2
  exit 2
fi

sed -i.bak -E "s/\"min_score\": *[0-9.]+/\"min_score\": $1/" "$FILE"
rm -f "$FILE.bak"
grep -oE '"min_score": *[0-9.]+' "$FILE"
