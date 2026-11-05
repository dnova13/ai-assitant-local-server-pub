#!/usr/bin/env bash
# 실행: ./run.sh [api|bot|cli|test]   (인자 없으면 api)
set -euo pipefail
cd "$(dirname "$0")"

VENV=../.venv  # 저장소 맨 위
if [ ! -f "$VENV/bin/activate" ]; then
  echo "❌ 가상환경이 없습니다. 먼저 ./setup.sh 를 실행하세요." >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

case "${1:-api}" in
  api)  exec python -m entrances.api_server ;;
  bot)  exec python -m entrances.discord_bot ;;
  cli)  exec python -m entrances.cli ;;
  test) exec python -m unittest discover -s tests -v ;;
  *)
    echo "사용법: ./run.sh [api|bot|cli|test]"
    echo "  api   앱·봇 입구 + 비서 본체 (포트 8050, 기본)"
    echo "  bot   디스코드 봇 (api 가 먼저 켜져 있어야 함)"
    echo "  cli   터미널 비서"
    echo "  test  단위 시험"
    exit 2
    ;;
esac
