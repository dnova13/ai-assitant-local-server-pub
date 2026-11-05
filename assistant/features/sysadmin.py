"""서버 관리 기능. 기존 서버 에이전트(legacy/server_agent_original.py)의 도구 함수를 가져왔다.
원본과 다른 점: 어떤 함수를 실행할지는 모델이 아니라 사용자 명령(코드)이 정한다."""
import ipaddress
import re
import socket
import subprocess
import time

import psutil
import requests

from clients import db, llm
from core import config
from features import dbinfo

# 조회 전용 명령 화이트리스트 (원본과 동일)
SAFE_COMMANDS = {
    "disk": ["df", "-h"],
    "mem": ["free", "-h"],
    "uptime": ["uptime", "-p"],
    "docker": ["docker", "ps"],
}
# 조회만 한다. 컴퓨터 절전·재시작은 !컴퓨터 로 나눴다 (2026-11-02, README.md). 목록 밖 서비스는 조회도 거부한다
STATUS_SERVICES = ["nginx", "mariadb", "mysql", "redis-server", "docker", "ollama", "assistant-api", "assistant-bot"]
CONTAINER_SERVICES = {"postgres": "assistant-postgres"}  # systemd 가 아니라 도커 컨테이너로 도는 것
NO_LOG_SERVICES = ("assistant-api", "assistant-bot")  # 로그에 요청 내용이 섞일 수 있어 상태만 보여 준다
SUSPEND_DELAY = 5  # 절전·재시작 답을 보낼 시간 (초)
ACTIONS = {
    "상태": "status", "status": "status",
    "시작": "start", "start": "start",
    "중지": "stop", "stop": "stop",
    "재시작": "restart", "restart": "restart",
}
CONTROL_OFF = "서버 제어가 꺼져 있습니다 (.env 의 SYSADMIN_CONTROL=true 로 켤 수 있음). 절전, 재시작, 깨우기는 할 수 없습니다."
# 시키는 말끝만 받는다. 부정·질문·지난 일('재시작하지 마', '재시작할까', '재시작했어')은 넣지 않는다
_DO_ENDINGS = ("", "해", "해라", "해줘", "해줘라", "해주라", "해주세요", "해줄래", "해봐", "해봐라", "해요", "하자",
               "시켜", "시켜라", "시켜봐", "시켜줘", "시켜줘라", "시켜주세요")
_ASK_ENDINGS = ("", "라", "줘", "줘라", "주라", "주세요", "줄래", "봐", "봐라", "요")
SLEEP_WORDS = (*("절전" + e for e in _DO_ENDINGS), *("재워" + e for e in _ASK_ENDINGS), "재우자")
WAKE_WORDS = ("깨우기", "깨우자", "켜자", *("깨워" + e for e in _ASK_ENDINGS), *("켜" + e for e in _ASK_ENDINGS))
RESTART_WORDS = tuple(s + e for s in ("재시작", "재부팅", "리붓", "리부트", "restart", "reboot") for e in _DO_ENDINGS)
# !컴퓨터, !서비스 뒤의 문장에서 뜻 없이 빼는 말. 나머지는 이름과 동작이어야 하고, 모르는 말이 남으면 실행하지 않는다
FILLER_WORDS = ("좀", "줘", "줘라", "주라", "주세요", "줄래", "봐", "봐라", "부탁해", "부탁해요",
                *(e for e in _DO_ENDINGS if e))  # 띄어 쓴 '재시작 해라', '재워 봐'
SHOW_WORDS = ("보여줘", "보여주세요", "알려줘", "알려주세요")
VIEW_WORDS = (*SHOW_WORDS, "상태", "status", "어때", "어때요")
PARTICLES = "을를은는이가"
THIS_SERVER = "서버"
RESERVED_NAMES = (*SLEEP_WORDS, *WAKE_WORDS, *RESTART_WORDS, *FILLER_WORDS, *SHOW_WORDS, THIS_SERVER, "컴퓨터", "수정", "목록", "등록", "삭제", "이름변경", "이거")  # 명령과 겹치는 이름은 받지 않는다
NAME_PATTERN = r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣_-]{0,29}"
TARGET_PATTERN = r"[A-Za-z0-9_][A-Za-z0-9._-]{0,31}@([0-9.]{7,15})"
SSH_PORT = 22
SLEEP_CHECKS = 4  # 재운 뒤 응답이 없어졌는지 볼 횟수
SLEEP_CHECK_SECONDS = 3
WOL_PORT = 9
MAX_OUT = 1500
# 그래픽카드: 사용률·메모리·온도 (조회만). GPU 사용률이 계속 높으면 모델이 생성 중이거나 요청이 밀린 것이다 (README.md)
GPU_QUERY = ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"]


def _clip(text):
    text = (text or "").strip()
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + "\n…(생략)"


def gpu_status():
    """그래픽카드 한 줄씩. nvidia-smi 가 없거나 읽지 못하면 그 사실을 한 줄로."""
    try:
        res = subprocess.run(GPU_QUERY, capture_output=True, text=True, check=True, timeout=10)
    except FileNotFoundError:
        return ["- GPU: nvidia-smi 가 없어 읽을 수 없음"]
    except Exception as e:
        return [f"- GPU: 읽기 실패 ({type(e).__name__})"]
    lines = []
    for row in res.stdout.strip().splitlines():
        parts = [p.strip() for p in row.split(",")]
        if len(parts) != 5:
            return [f"- GPU: 출력을 읽지 못함 ({_clip(res.stdout)[:80]})"]
        name, util, used, total, temp = parts
        try:
            busy = "작업 중" if int(util) >= 10 else "대기"
        except ValueError:
            busy = "?"
        lines.append(f"- GPU: {name} {util}% ({busy}), VRAM {used}MB / {total}MB, {temp}°C")
    return lines or ["- GPU: 없음"]


def ollama_models(timeout=3):
    """Ollama 에 올라가 있는 모델 (/api/ps, 생성 요청이 아니라 대기열에 쌓이지 않는다). 못 읽으면 None."""
    try:
        r = requests.get(f"{config.OLLAMA_URL}/api/ps", headers=config.ollama_headers(), timeout=timeout)
        r.raise_for_status()
        return [(m.get("name", "?"), int(m.get("size_vram") or 0) // (1024**2)) for m in r.json().get("models", [])]
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None


def get_system_status():
    """CPU, 메모리, 루트 디스크 사용량 + 그래픽카드와 Ollama 에 올라간 모델 + DB"""
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    models = ollama_models()
    if models is None:
        ollama = "- Ollama: 응답 없음 (꺼졌거나 요청이 밀림)"
    else:
        ollama = "- Ollama 모델: " + (", ".join(f"{n} ({v}MB)" for n, v in models) or "올라간 모델 없음")
    return "\n".join([
        "[시스템 상태]",
        f"- CPU: {cpu}%",
        f"- RAM: {mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB ({mem.percent}%)",
        f"- Disk: {disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB ({disk.percent}%)",
        *gpu_status(),
        ollama,
        dbinfo.status_line(),
    ])


def run_safe(key):
    if key not in SAFE_COMMANDS:
        return f"실행 거부: '{key}'는 허용되지 않은 명령입니다."
    try:
        res = subprocess.run(SAFE_COMMANDS[key], capture_output=True, text=True, check=True, timeout=10)
        return _clip(res.stdout)
    except FileNotFoundError:
        return f"명령을 찾을 수 없습니다: {SAFE_COMMANDS[key][0]}"
    except Exception as e:
        return f"명령 실행 실패: {e}"


def _unit_state(name):
    res = subprocess.run(["systemctl", "show", name, "--property=LoadState,ActiveState"],
                         capture_output=True, text=True, timeout=10)
    values = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
    if "LoadState" not in values:
        return "확인 못 함"
    if values["LoadState"] == "not-found":
        return "없음"
    return "실행 중" if values.get("ActiveState") == "active" else "멈춤"


def _container_state(container):
    res = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container],
                         capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        return "없음" if "no such" in (res.stderr or "").lower() else "확인 못 함"
    return "실행 중" if res.stdout.strip() == "true" else "멈춤"


def service_state(name):
    """실행 중 / 멈춤 / 없음 / 확인 못 함."""
    try:
        return _container_state(CONTAINER_SERVICES[name]) if name in CONTAINER_SERVICES else _unit_state(name)
    except Exception:
        return "확인 못 함"


def service_list():
    names = [*STATUS_SERVICES, *CONTAINER_SERVICES]
    lines = [f"- {name}: {service_state(name)} / 조회만" for name in names]
    lines.append(f"- 절전: 서버 절전 / {'제어 가능' if config.SYSADMIN_CONTROL else '조회만'}")
    return "\n".join(["서비스 목록", *lines, "", "사용법: 서비스 <이름> (상태 보기). 컴퓨터 절전과 재시작은 !컴퓨터"])


def _allowed():
    return ", ".join([*STATUS_SERVICES, *CONTAINER_SERVICES])


def service_status(name):
    if name in CONTAINER_SERVICES:
        return f"{name}: {service_state(name)}\n{dbinfo.status_line()}"
    if name not in STATUS_SERVICES:
        return f"조회 거부: '{name}'는 허용된 서비스가 아닙니다. ({_allowed()})"
    if name in NO_LOG_SERVICES:
        return f"{name}: {service_state(name)}"
    try:
        res = subprocess.run(
            ["systemctl", "status", name, "--no-pager", "-n", "5"],
            capture_output=True, text=True, timeout=10,
        )
        return _clip(res.stdout or res.stderr) or f"{name}: 출력 없음"
    except Exception as e:
        return f"상태 조회 실패: {e}"


def clean_words(text, names):
    """명령 뒤 말에서 뜻 없는 말을 빼고, 아는 이름에 붙은 조사만 뗀다 ('소니를' → '소니')."""
    words = []
    for p in text.split():
        if p in FILLER_WORDS:
            continue
        words.append(p[:-1] if p not in names and p[-1] in PARTICLES and p[:-1] in names else p)
    return words


def _power_later(action):
    """이 서버의 절전·재시작. 답을 보낼 수 있게 조금 뒤에 실행한다. sudo -n 이라 비밀번호를 묻지 않는다. 허용되어 있지 않으면 False."""
    allowed = subprocess.run(["sudo", "-n", "-l", "systemctl", action], capture_output=True, text=True, timeout=10)
    if allowed.returncode != 0:
        return False
    subprocess.Popen(["sh", "-c", f"sleep {SUSPEND_DELAY}; exec sudo -n systemctl {action}"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def suspend():
    """절전. SYSADMIN_CONTROL=true 일 때만."""
    if not config.SYSADMIN_CONTROL:
        return CONTROL_OFF
    try:
        if not _power_later("suspend"):
            return "sudo 권한이 없습니다 (비밀번호 없이 systemctl suspend 허용 설정 필요). README.md 참고."
    except Exception as e:
        return f"절전 실패: {e}"
    return f"{SUSPEND_DELAY}초 뒤 서버를 절전합니다. 깨우려면 서버 앞에서 전원 버튼이나 키보드를 눌러 주세요."


def restart_server():
    """재시작. SYSADMIN_CONTROL=true 일 때만. 비서는 서비스 등록대로 다시 켜진다."""
    if not config.SYSADMIN_CONTROL:
        return CONTROL_OFF
    try:
        if not _power_later("reboot"):
            return "sudo 권한이 없습니다 (비밀번호 없이 systemctl reboot 허용 설정 필요). README.md 참고."
    except Exception as e:
        return f"재시작 실패: {e}"
    return f"{SUSPEND_DELAY}초 뒤 서버를 재시작합니다. 비서가 다시 켜질 때까지 답을 못 해요. 잠시 뒤 `!상태`로 확인해 주세요."


# --- 다른 컴퓨터: 재우기는 SSH (키를 그 컴퓨터의 절전 명령만 실행하게 묶어 둔다), 깨우기는 WOL ---
def computers():
    """등록한 컴퓨터 {이름: (사용자@주소, MAC)}."""
    with db.connect() as c:
        rows = c.execute("SELECT name, target, mac FROM computers ORDER BY created, name").fetchall()
    return {name: (target, mac) for name, target, mac in rows}


def check_name(name):
    """이름이 틀리면 안내, 맞으면 None."""
    if not re.fullmatch(NAME_PATTERN, name) or name in RESERVED_NAMES:
        return f"이름은 띄어쓰기 없이 한글·영문·숫자로 적어 주세요. 명령과 겹치는 말({', '.join(RESERVED_NAMES[:5])} 등)은 쓸 수 없어요."
    return None


def check_target(target):
    """SSH 명령에 들어가는 값이라 '영문 사용자@사설 IPv4'만 받는다. 틀리면 안내, 맞으면 None."""
    m = re.fullmatch(TARGET_PATTERN, target)
    if m:
        try:
            address = ipaddress.ip_address(m.group(1))
            if address.version == 4 and address.is_private and not address.is_loopback:
                return None
        except ValueError:
            pass
    return "접속 정보는 '영문사용자@내부주소' 형식으로 적어 주세요. 예: ubuntu@192.168.0.10"


def add_computer(name, target):
    with db.connect() as c:
        c.execute("INSERT INTO computers(name, target, created) VALUES (?,?,?)", (name, target, llm.now().replace(microsecond=0)))


def delete_computer(name):
    with db.connect() as c:
        return c.execute("DELETE FROM computers WHERE name=?", (name,)).rowcount


def rename_computer(name, new):
    with db.connect() as c:
        return c.execute("UPDATE computers SET name=? WHERE name=?", (new, name)).rowcount


def update_target(name, target):
    """접속 정보를 바꾼다. 주소가 달라지면 다른 컴퓨터일 수 있어 저장한 MAC 을 지운다 (다음 재우기 때 다시 저장한다)."""
    keep_mac = _address(name) == target.rsplit("@", 1)[1]
    with db.connect() as c:
        if keep_mac:
            return c.execute("UPDATE computers SET target=? WHERE name=?", (target, name)).rowcount
        return c.execute("UPDATE computers SET target=?, mac=NULL WHERE name=?", (target, name)).rowcount


def set_mac(name, mac):
    with db.connect() as c:
        c.execute("UPDATE computers SET mac=? WHERE name=?", (mac, name))


def _address(name):
    return computers()[name][0].rsplit("@", 1)[-1]


def computer_up(name):
    """SSH 포트에 접속되면 True. 잠듦과 꺼짐은 네트워크로 구분할 수 없다."""
    try:
        with socket.create_connection((_address(name), SSH_PORT), timeout=1):
            return True
    except OSError:
        return False


def read_mac(name):
    """깨어 있는 컴퓨터의 MAC. 비서 서버의 네트워크 표에서 읽는다 (같은 공유기 안). 못 읽으면 None."""
    address = _address(name)
    try:
        subprocess.run(["ping", "-c", "1", "-W", "1", address], capture_output=True, text=True, timeout=5)
        res = subprocess.run(["ip", "neigh", "show", address], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    m = re.search(r"lladdr\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", res.stdout)
    return m.group(1).lower() if m else None


def _power_failure(err):
    low = (err or "").lower()
    if "permission denied" in low:  # 'Permission denied (publickey,password)' 라서 암호 검사보다 먼저 본다
        return "SSH 접속이 거부됐습니다. 등록한 사용자 이름과 그 컴퓨터의 키 등록을 확인해 주세요. README.md 참고."
    if "sudo:" in low or "암호" in low or "password" in low:
        return "그 컴퓨터(우분투)에 절전·재시작 sudo 허용 설정이 필요합니다. README.md 참고."
    if "host key verification" in low:
        return "비서 서버에서 그 컴퓨터에 처음 접속하는 것입니다. 서버에서 한 번 접속해 지문을 저장해 주세요."
    if "could not resolve" in low or "no route" in low or "timed out" in low or "refused" in low:
        return "그 컴퓨터에 접속하지 못했습니다. 켜져 있는지 확인해 주세요."
    return None


def _power_ssh(name, *word):
    """전용 키로 접속한다. 무슨 동작을 할지는 그 컴퓨터의 키 설정이 정한다 (아무 말 없으면 절전, restart 면 재시작). 실패 안내 또는 None."""
    try:
        res = subprocess.run(["ssh", "-T", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                              "-o", "ServerAliveInterval=2", "-o", "ServerAliveCountMax=2",
                              "-i", config.COMPUTER_SSH_KEY, computers()[name][0], *word],
                             capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None  # 꺼지면서 연결이 멈춘 경우
    return _power_failure(res.stderr)


def _went_down(name):
    for _ in range(SLEEP_CHECKS):
        time.sleep(SLEEP_CHECK_SECONDS)
        if not computer_up(name):
            return True
    return False


def computer_sleep(name):
    """절전하면 연결이 끊겨 오류로 끝날 수 있어, 응답이 없어졌는지로 판단한다."""
    if not config.SYSADMIN_CONTROL:
        return CONTROL_OFF
    try:
        problem = _power_ssh(name)
    except Exception as e:
        return f"절전 실패: {type(e).__name__}"
    if problem:
        return problem
    if _went_down(name):
        return f"{name}이(가) 잠들었어요. 깨우려면: 컴퓨터 {name} 깨워"
    return f"{name}이(가) 아직 응답해요. 절전이 안 됐을 수 있어요. '컴퓨터 목록'으로 확인해 주세요."


def computer_restart(name):
    """켜지는 데 시간이 걸려 기다리지 않는다. 응답이 끊겼는지만 보고, 다시 켜졌는지는 사람이 목록으로 본다."""
    if not config.SYSADMIN_CONTROL:
        return CONTROL_OFF
    try:
        problem = _power_ssh(name, "restart")
    except Exception as e:
        return f"재시작 실패: {type(e).__name__}"
    if problem:
        return problem
    if _went_down(name):
        return (f"{name}의 연결이 끊겼어요. 재시작 중이에요. 1~2분 뒤 '컴퓨터 목록'으로 확인해 주세요. "
                "계속 응답이 없으면 키 설정이 절전만 하는 것일 수 있어요 (README.md).")
    return f"{name}이(가) 아직 응답해요. 재시작이 안 됐을 수 있어요. '컴퓨터 목록'으로 확인해 주세요."


def wake(mac):
    """WOL 신호 (매직 패킷)를 같은 공유기 안에 보낸다."""
    packet = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, ("255.255.255.255", WOL_PORT))


def computer_list(files=None):
    """주소와 MAC 은 보여 주지 않는다. files: 앱 컴퓨터 {이름: 파일 상태}. 이름이 같으면 한 줄로 합친다."""
    files = dict(files or {})
    control = config.SYSADMIN_CONTROL
    lines = [f"- 이 서버: 켜짐 / {'절전 가능' if control else '조회만'} / 깨우기 안 됨"]
    for name, (_, mac) in computers().items():
        state = "켜짐" if computer_up(name) else "응답 없음"
        sleep = "절전 가능" if control else "조회만"
        waking = "조회만" if not control else "깨우기 가능" if mac else "깨우기 준비 안 됨"
        file_state = files.pop(name, None)
        lines.append(f"- {name}: {state} / {sleep} / {waking}" + (f" / {file_state}" if file_state else ""))
    lines += [f"- {name}: {state}" for name, state in files.items()]
    return "\n".join(["컴퓨터 목록", *lines, "", "사용법: 컴퓨터 <이름> 절전, 컴퓨터 <이름> 깨워, 컴퓨터 <이름> 재시작, 컴퓨터 절전 (이 서버), 컴퓨터 재시작 (이 서버)",
                       "등록: 컴퓨터 등록 <이름> <사용자@내부주소>, 컴퓨터 수정 <이름> <사용자@내부주소> (DM, 등록한 서버·채널), 컴퓨터 삭제 <이름>, 컴퓨터 이름변경 <이름> <새 이름>"])


def snapshot():
    """점검용: 상태 + 디스크 + 메모리 + 가동시간 + 도커 한 번에 수집"""
    parts = [
        get_system_status(),
        "[디스크]\n" + run_safe("disk"),
        "[메모리]\n" + run_safe("mem"),
        "[가동시간] " + run_safe("uptime"),
        "[도커]\n" + run_safe("docker"),
    ]
    return "\n\n".join(parts)
