"""터미널용 입구. 실행: python -m entrances.cli   (종료: exit 또는 Ctrl+C)"""
from core import config, users
from core.assistant import Assistant

# 터미널은 서버에 접속한 사람 = owner(관리자)로 본다. 디스코드와 같은 내부 ID라 같은 메모·설정을 본다
users.bootstrap()
USER = users.OWNER


def main():
    bot = Assistant()
    print(f"개인 비서 ({config.MODEL}) — '도움말' 입력, 종료는 exit")  # 주소는 화면에 찍지 않는다
    while True:
        try:
            text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in ("exit", "quit", "종료"):
            break
        if text or USER in bot.pending_mode:  # 일정 알림 방식을 묻는 중이면 엔터도 대답이다
            print(bot.handle(USER, text))


if __name__ == "__main__":
    main()
