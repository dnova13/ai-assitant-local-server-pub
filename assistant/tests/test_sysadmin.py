import os, sys, tempfile, time, unittest, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["ASSISTANT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from core import classifier, config
from clients import llm
from features import sysadmin
from core.assistant import Assistant


class Result:
    def __init__(self, out="", err="", code=0):
        self.stdout, self.stderr, self.returncode = out, err, code


class Sys(unittest.TestCase):
    def setUp(self):
        self.ran = []
        def fake_run(cmd, **kw):
            self.ran.append(cmd)
            return Result("출력:" + " ".join(cmd))
        self._orig = subprocess.run
        subprocess.run = fake_run
        self._orig_classify = classifier.ask_model
        classifier.ask_model = lambda text, model=None, timeout=120: ('{"의도": "일반대화"}', 0.0)  # 시험에서 실제 모델을 부르지 않는다
        self.llm_calls = []
        llm.chat = lambda m, model=None, timeout=300: (self.llm_calls.append(m) or "요약됨")
        config.SYSADMIN_CONTROL = False
        self._orig_models = sysadmin.ollama_models
        sysadmin.ollama_models = lambda timeout=3: []  # 시험에서 실제 Ollama 를 부르지 않는다
        self.bot = Assistant()

    def tearDown(self):
        subprocess.run = self._orig
        classifier.ask_model = self._orig_classify
        sysadmin.ollama_models = self._orig_models

    def test_status_no_model(self):
        out = self.bot.handle("u", "!상태")
        self.assertIn("CPU", out); self.assertIn("RAM", out); self.assertIn("Disk", out); self.assertIn("GPU", out)

    def test_gpu_line(self):
        subprocess.run = lambda cmd, **kw: Result("NVIDIA GeForce RTX 2060, 97, 4637, 6144, 71\n")
        self.assertEqual(sysadmin.gpu_status(), ["- GPU: NVIDIA GeForce RTX 2060 97% (작업 중), VRAM 4637MB / 6144MB, 71°C"])
        subprocess.run = lambda cmd, **kw: Result("NVIDIA GeForce RTX 2060, 0, 4637, 6144, 50\n")
        self.assertIn("(대기)", sysadmin.gpu_status()[0])

    def test_gpu_missing_or_odd(self):
        def missing(cmd, **kw):
            raise FileNotFoundError
        subprocess.run = missing
        self.assertIn("nvidia-smi 가 없어", sysadmin.gpu_status()[0])
        subprocess.run = lambda cmd, **kw: Result("이상한 출력")
        self.assertIn("읽지 못함", sysadmin.gpu_status()[0])

    def test_ollama_line(self):
        from unittest import mock
        sysadmin.ollama_models = self._orig_models
        class R:
            def raise_for_status(self): pass
            def json(self): return {"models": [{"name": "llama3.1:8b", "size_vram": 4390314311}]}
        with mock.patch("requests.get", return_value=R()):
            self.assertIn("llama3.1:8b (4186MB)", self.bot.handle("u", "!상태"))
        with mock.patch("requests.get", side_effect=sysadmin.requests.ConnectionError()):
            self.assertIn("Ollama: 응답 없음", self.bot.handle("u", "!상태"))
        self.assertEqual(self.llm_calls, [])

    def test_whitelist_commands(self):
        self.assertIn("df -h", self.bot.handle("u", "!디스크"))
        self.assertIn("free -h", self.bot.handle("u", "!메모리"))
        self.assertIn("docker ps", self.bot.handle("u", "!도커"))
        self.assertIn("거부", sysadmin.run_safe("rm -rf /"))

    def test_service_status_only_allowed(self):
        self.bot.handle("u", "!서비스 nginx")
        self.assertEqual(self.ran[-1][:3], ["systemctl", "status", "nginx"])
        self.ran.clear()
        self.assertIn("거부", self.bot.handle("u", "!서비스 sshd"))
        self.assertEqual(self.ran, [])  # 허용 목록 밖은 아예 실행 안 됨

    def state_run(self, states):
        def run(cmd, **kw):
            self.ran.append(cmd)
            if cmd[0] == "docker":
                return Result("true\n") if states.get("postgres") == "up" else Result("", "Error: No such object", 1)
            load, active = states.get(cmd[2], ("loaded", "active"))
            return Result(f"ActiveState={active}\nLoadState={load}\n")
        subprocess.run = run

    def test_service_list(self):
        self.state_run({"mariadb": ("not-found", "inactive"), "mysql": ("loaded", "inactive"), "postgres": "up"})
        for text in ("!서비스", "!서비스 목록"):
            out = self.bot.handle("u", text)
            self.assertIn("- nginx: 실행 중 / 조회만", out)
            self.assertIn("- mariadb: 없음 / 조회만", out)
            self.assertIn("- mysql: 멈춤 / 조회만", out)
            for name in ("redis-server", "docker", "ollama", "assistant-api", "assistant-bot", "postgres"):
                self.assertIn(f"- {name}: 실행 중 / 조회만", out)
            self.assertIn("- 절전: 서버 절전 / 조회만", out)
        config.SYSADMIN_CONTROL = True
        self.assertIn("- 절전: 서버 절전 / 제어 가능", self.bot.handle("u", "!서비스"))

    def test_service_list_when_state_cannot_be_read(self):
        def broken(cmd, **kw):
            raise FileNotFoundError(cmd[0])
        subprocess.run = broken
        self.assertIn("- nginx: 확인 못 함 / 조회만", self.bot.handle("u", "!서비스"))

    def test_assistant_services_show_no_log_lines(self):
        self.state_run({})
        for name in ("assistant-api", "assistant-bot"):
            self.assertEqual(self.bot.handle("u", f"!서비스 {name}"), f"{name}: 실행 중")
        self.assertFalse(any("status" in c for c in self.ran))

    def test_postgres_status_uses_container(self):
        self.state_run({"postgres": "up"})
        out = self.bot.handle("u", "!서비스 postgres")
        self.assertIn("postgres: 실행 중", out)
        self.assertIn("- DB:", out)
        self.assertEqual(self.ran[-1][:2], ["docker", "inspect"])
        self.state_run({})
        self.assertIn("postgres: 없음", self.bot.handle("u", "!서비스 postgres"))

    def test_every_service_is_view_only(self):
        config.SYSADMIN_CONTROL = True
        for name in ("nginx", "docker", "mysql", "assistant-api", "postgres"):
            for word in ("시작", "중지", "재시작", "restart"):
                self.assertIn("상태만 볼 수 있어요", self.bot.handle("u", f"!서비스 {name} {word}"))
        self.assertIn("없습니다", self.bot.handle("u", "!확인"))
        self.assertEqual(self.ran, [])

    def suspend_setup(self):
        config.SYSADMIN_CONTROL = True
        self.spawned = []
        self._orig_popen = subprocess.Popen
        subprocess.Popen = lambda cmd, **kw: self.spawned.append(cmd)
        self.addCleanup(setattr, subprocess, "Popen", self._orig_popen)

    def test_suspend_off_by_default(self):
        self.assertIn("꺼져", self.bot.handle("u", "!컴퓨터 절전"))
        self.assertNotIn("u", self.bot.pending)

    def test_suspend_asks_once(self):
        self.suspend_setup()
        self.assertIn("서버 앞에서 직접 깨워야", self.bot.handle("u", "!컴퓨터 절전"))
        self.assertEqual(self.spawned, [])
        self.assertIn("절전합니다", self.bot.handle("u", "!확인"))
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("systemctl suspend", self.spawned[0][-1])
        self.assertIn("없습니다", self.bot.handle("u", "!확인"))  # 한 번만
        self.assertEqual(len(self.spawned), 1)
    def test_suspend_accepts_yes_words(self):
        self.suspend_setup()
        for word in ("확인", "!확인", "어", "네", "응", "yes", "YES", "ok", "해줘", "네!", "ㅇㅇ"):
            self.bot.handle("u", "!컴퓨터 절전")
            self.assertIn("초 뒤 서버를 절전합니다", self.bot.handle("u", word), word)
            self.assertNotIn("u", self.bot.pending, word)
        self.assertEqual(len(self.spawned), 11)
    def test_suspend_no_or_other_word_runs_nothing(self):
        self.suspend_setup()
        for word in ("아니", "no", "취소", "됐어", "ㄴㄴ"):
            self.bot.handle("u", "!컴퓨터 절전")
            self.assertEqual(self.bot.handle("u", word), "절전을 취소했습니다.", word)
            self.assertIn("없습니다", self.bot.handle("u", "!확인"))
        for word in ("안녕", "지워", "지워줘", "네 알겠어"):  # '지워'는 절전의 답이 아니다
            self.bot.handle("u", "!컴퓨터 절전")
            out = self.bot.handle("u", word)
            self.assertIn("(절전을 취소했어요.)", out, word)
            self.assertIn("요약됨", out, word)
            self.assertIn("없습니다", self.bot.handle("u", "!확인"))
        self.assertEqual(self.spawned, [])

    def test_restart_yes_words_need_two_answers(self):
        self.suspend_setup()
        self.bot.handle("u", "!컴퓨터 재시작")
        self.assertIn("정말 서버를", self.bot.handle("u", "어"))
        self.assertEqual(self.spawned, [])
        self.assertIn("초 뒤 서버를 재시작", self.bot.handle("u", "yes"))
        self.assertEqual(len(self.spawned), 1)
        self.bot.handle("u", "!컴퓨터 재시작")
        self.bot.handle("u", "네")
        self.assertEqual(self.bot.handle("u", "no"), "재시작을 취소했습니다.")
        self.assertEqual(len(self.spawned), 1)

    def test_yes_word_without_waiting_is_chat(self):
        self.suspend_setup()
        self.assertIn("요약됨", self.bot.handle("u", "어"))
        self.assertEqual(self.spawned, [])

    def test_yes_word_needs_admin(self):
        self.suspend_setup()
        self.bot.handle("u", "!컴퓨터 절전")
        self.assertNotIn("초 뒤", self.bot.handle("u", "어", allow_admin=False))
        self.assertEqual(self.spawned, [])
    def test_suspend_no_answer_does_nothing(self):
        self.suspend_setup()
        self.bot.handle("u", "!컴퓨터 절전")
        self.bot.pending["u"] = (1, 0, None)  # 만료시킴
        self.assertIn("취소", self.bot.handle("u", "!확인"))
        for word in ("어", "네"):
            self.bot.handle("u", "!컴퓨터 절전")
            self.bot.pending["u"] = (1, 0, None)
            self.assertIn("시간이 지나", self.bot.handle("u", word), word)
        self.bot.handle("u", "!컴퓨터 절전")
        self.bot.pending["u"] = (1, 0, None)
        self.assertIn("대답이 없어", self.bot.handle("u", "안녕"))
        self.assertEqual(self.spawned, [])
    def test_suspend_confirm_is_per_user(self):
        self.suspend_setup()
        self.bot.handle("a", "!컴퓨터 절전")
        self.assertIn("없습니다", self.bot.handle("b", "!확인"))
        self.assertEqual(self.spawned, [])

    def test_suspend_needs_sudo_setting(self):
        self.suspend_setup()
        subprocess.run = lambda cmd, **kw: Result("", "sudo: a password is required", 1)
        self.bot.handle("u", "!컴퓨터 절전")
        self.assertIn("sudo 권한", self.bot.handle("u", "!확인"))
        self.assertEqual(self.spawned, [])
    def test_suspend_refused_without_admin(self):
        self.suspend_setup()
        self.assertIn("관리자만", self.bot.handle("u", "!컴퓨터 절전", allow_admin=False))
        self.assertNotIn("u", self.bot.pending)

    def computer_setup(self, up=False, ssh=None):
        """프록시 한 대 등록. 네트워크와 SSH 는 쓰지 않는다."""
        self.suspend_setup()
        self._real = (sysadmin.computer_up, sysadmin.wake, time.sleep, config.COMPUTER_SSH_KEY)
        self.addCleanup(self.computer_restore)
        for name in sysadmin.computers():
            sysadmin.delete_computer(name)
        sysadmin.add_computer("프록시", "ubuntu@10.0.0.9")
        config.COMPUTER_SSH_KEY = "/없는/키"
        sysadmin.computer_up = lambda name: up
        self.woken = []
        sysadmin.wake = lambda mac: self.woken.append(mac)
        time.sleep = lambda s: None
        self.calls = []

        def run(cmd, **kw):
            self.calls.append(cmd)
            if cmd[0] == "ip":
                return Result("10.0.0.9 dev enp2s0 lladdr F0:BF:97:00:00:01 REACHABLE\n")
            if cmd[0] == "ssh" and ssh:
                return ssh
            return Result("")
        subprocess.run = run

    def computer_restore(self):
        sysadmin.computer_up, sysadmin.wake, time.sleep, config.COMPUTER_SSH_KEY = self._real
        for name in sysadmin.computers():
            sysadmin.delete_computer(name)

    def ssh_calls(self):
        return [c for c in self.calls if c[0] == "ssh"]

    def test_computer_sleep_asks_once_then_runs_ssh(self):
        self.computer_setup()
        for text in ("!컴퓨터 프록시 재워", "!컴퓨터 재워 프록시", "!컴퓨터 프록시 절전", "!컴퓨터 프록시 재워 줘"):
            self.assertIn("프록시을(를) 절전합니다", self.bot.handle("u", text), text)
            self.assertEqual(self.ssh_calls(), [])
            self.bot.handle("u", "아니")
        self.bot.handle("u", "!컴퓨터 프록시 재워")
        self.assertIn("잠들었어요", self.bot.handle("u", "어"))
        call = self.ssh_calls()[0]
        self.assertIn("IdentitiesOnly=yes", call)
        self.assertIn("BatchMode=yes", call)
        self.assertEqual(call[-1], "ubuntu@10.0.0.9")
        self.assertEqual(self.spawned, [])  # 이 서버는 재우지 않는다
    def test_computer_sleep_saves_mac_before_ssh(self):
        self.computer_setup()
        self.bot.handle("u", "!컴퓨터 프록시 재워")
        self.bot.handle("u", "!확인")
        names = [c[0] for c in self.calls]
        self.assertLess(names.index("ip"), names.index("ssh"))
        self.assertEqual(sysadmin.computers()["프록시"][1], "f0:bf:97:00:00:01")

    def test_computer_wake_uses_saved_mac_without_asking(self):
        self.computer_setup()
        self.assertIn("MAC 을 아직 몰라요", self.bot.handle("u", "!컴퓨터 프록시 깨워"))
        self.assertEqual(self.woken, [])
        sysadmin.set_mac("프록시", "f0:bf:97:00:00:01")
        for text in ("!컴퓨터 프록시 깨워", "!컴퓨터 깨워 프록시", "!컴퓨터 프록시 깨우기", "!컴퓨터 프록시 깨워줘"):
            self.assertIn("깨우기 신호", self.bot.handle("u", text), text)
        self.assertEqual(self.woken, ["f0:bf:97:00:00:01"] * 4)
        self.assertNotIn("u", self.bot.pending)

    def test_computer_unknown_name_runs_nothing(self):
        self.computer_setup()
        for text in ("!컴퓨터 이거 깨워", "!컴퓨터 이거 재워", "!컴퓨터 깨워", "!컴퓨터 nginx 재워", "!컴퓨터 프록시 이거 재워"):
            out = self.bot.handle("u", text)
            self.assertIn("이름으로 말씀해 주세요", out, text)
            self.assertIn("프록시", out)
            self.assertNotIn("u", self.bot.pending)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(self.woken, [])

    def test_bare_sleep_is_this_server(self):
        self.computer_setup()
        for text in ("!컴퓨터 절전", "!컴퓨터 재워"):
            self.assertIn("서버를 절전합니다", self.bot.handle("u", text), text)
            self.bot.handle("u", "아니")

    def test_computer_needs_control_on(self):
        self.computer_setup()
        config.SYSADMIN_CONTROL = False
        for text in ("!컴퓨터 프록시 재워", "!컴퓨터 프록시 깨워"):
            self.assertIn("꺼져", self.bot.handle("u", text), text)
        self.assertEqual(self.woken, [])

    def test_computer_refused_without_admin(self):
        self.computer_setup()
        for text in ("!컴퓨터 프록시 깨워", "!컴퓨터"):
            self.assertIn("관리자만", self.bot.handle("u", text, allow_admin=False))
        self.assertEqual(self.woken, [])

    def test_computer_other_reply_or_timeout_runs_nothing(self):
        self.computer_setup()
        self.bot.handle("u", "!컴퓨터 프록시 재워")
        self.assertIn("취소", self.bot.handle("u", "아니"))
        self.bot.handle("u", "!컴퓨터 프록시 재워")
        self.assertIn("(절전을 취소했어요.)", self.bot.handle("u", "안녕"))
        self.bot.handle("u", "!컴퓨터 프록시 재워")
        self.bot.pending["u"] = (1, 0, "프록시")
        self.assertIn("취소", self.bot.handle("u", "!확인"))
        self.assertEqual(self.ssh_calls(), [])
    def test_computer_sleep_failures_are_explained(self):
        for err, word in (("sudo: 암호가 필요합니다", "sudo"), ("ubuntu@x: Permission denied (publickey)", "거부"),
                          ("dnova@x: Permission denied (publickey,password,keyboard-interactive).", "거부"),
                          ("Host key verification failed.", "지문"), ("ssh: connect to host x port 22: Connection timed out", "접속하지")):
            self.computer_setup(ssh=Result("", err, 255))
            self.bot.handle("u", "!컴퓨터 프록시 재워")
            self.assertIn(word, self.bot.handle("u", "!확인"), err)
            self.computer_restore()
    def test_computer_sleep_connection_drop_is_not_failure(self):
        self.computer_setup(ssh=Result("", "Connection to x closed by remote host.", 255))
        self.bot.handle("u", "!컴퓨터 프록시 재워")
        self.assertIn("잠들었어요", self.bot.handle("u", "!확인"))
    def test_computer_still_up_is_reported(self):
        self.computer_setup(up=True)
        self.bot.handle("u", "!컴퓨터 프록시 재워")
        self.assertIn("아직 응답해요", self.bot.handle("u", "!확인"))
    def test_restart_this_server_asks_twice(self):
        self.suspend_setup()
        for text in ("!컴퓨터 재시작", "!컴퓨터 재부팅", "!컴퓨터 restart"):
            self.assertIn("서버를 재시작합니다", self.bot.handle("u", text), text)
            self.assertEqual(self.bot.handle("u", "아니"), "재시작을 취소했습니다.")
        self.bot.handle("u", "!컴퓨터 재시작")
        self.assertIn("정말 서버를", self.bot.handle("u", "!확인"))
        self.assertEqual(self.spawned, [])
        self.assertIn("초 뒤 서버를 재시작", self.bot.handle("u", "!확인"))
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("systemctl reboot", self.spawned[0][-1])
        self.assertIn("없습니다", self.bot.handle("u", "!확인"))  # 한 번만

    def test_restart_needs_sudo_setting(self):
        self.suspend_setup()
        subprocess.run = lambda cmd, **kw: Result("", "sudo: a password is required", 1)
        self.bot.handle("u", "!컴퓨터 재시작")
        self.bot.handle("u", "!확인")
        out = self.bot.handle("u", "!확인")
        self.assertIn("sudo 권한", out)
        self.assertIn("systemctl reboot", out)
        self.assertEqual(self.spawned, [])

    def test_restart_computer_sends_word_after_two_asks(self):
        self.computer_setup()
        for text in ("!컴퓨터 프록시 재시작", "!컴퓨터 재시작 프록시", "!컴퓨터 프록시 재부팅해줘"):
            self.assertIn("프록시을(를) 강제 재시작합니다", self.bot.handle("u", text), text)
            self.assertIn("정말 프록시", self.bot.handle("u", "!확인"))
            self.assertEqual(self.ssh_calls(), [])
            self.bot.handle("u", "아니")
        self.bot.handle("u", "!컴퓨터 프록시 재시작")
        self.bot.handle("u", "!확인")
        self.assertIn("재시작 중이에요", self.bot.handle("u", "!확인"))
        call = self.ssh_calls()[0]
        self.assertIn("BatchMode=yes", call)
        self.assertEqual(call[-2:], ["ubuntu@10.0.0.9", "restart"])
        self.assertEqual(self.spawned, [])  # 이 서버는 재시작하지 않는다

    def test_sleep_sends_no_word(self):
        self.computer_setup()
        self.bot.handle("u", "!컴퓨터 프록시 재워")
        self.bot.handle("u", "!확인")
        self.assertEqual(self.ssh_calls()[0][-1], "ubuntu@10.0.0.9")

    def test_restart_computer_still_up_and_failures(self):
        self.computer_setup(up=True)
        self.bot.handle("u", "!컴퓨터 프록시 재시작")
        self.bot.handle("u", "!확인")
        self.assertIn("재시작이 안 됐을 수", self.bot.handle("u", "!확인"))
        self.computer_restore()
        self.computer_setup(ssh=Result("", "sudo: 암호가 필요합니다", 1))
        self.bot.handle("u", "!컴퓨터 프록시 재시작")
        self.bot.handle("u", "!확인")
        self.assertIn("sudo 허용", self.bot.handle("u", "!확인"))

    def test_restart_unknown_name_runs_nothing(self):
        self.computer_setup()
        for text in ("!컴퓨터 이거 재시작", "!컴퓨터 nginx 재시작", "!컴퓨터 프록시 이거 재시작", "!컴퓨터 프록시 재워 재시작"):
            self.assertIn("이름으로 말씀해 주세요", self.bot.handle("u", text), text)
            self.assertNotIn("u", self.bot.pending)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(self.spawned, [])

    def test_restart_other_reply_or_timeout_runs_nothing(self):
        self.computer_setup()
        self.bot.handle("u", "!컴퓨터 프록시 재시작")
        self.assertIn("재시작을 취소", self.bot.handle("u", "아니"))
        self.bot.handle("u", "!컴퓨터 프록시 재시작")
        self.bot.handle("u", "!확인")
        self.bot.pending["u"] = (4, 0, "프록시")
        self.assertIn("취소", self.bot.handle("u", "!확인"))
        self.assertEqual(self.ssh_calls(), [])

    def test_restart_refused_without_control_or_admin(self):
        self.computer_setup()
        self.assertIn("관리자만", self.bot.handle("u", "!컴퓨터 재시작", allow_admin=False))
        config.SYSADMIN_CONTROL = False
        for text in ("!컴퓨터 재시작", "!컴퓨터 프록시 재시작"):
            self.assertIn("꺼져", self.bot.handle("u", text), text)
            self.assertNotIn("u", self.bot.pending)

    def test_service_no_longer_controls_computers(self):
        self.computer_setup()
        sysadmin.set_mac("프록시", "f0:bf:97:00:00:01")
        for text in ("!서비스 절전", "!서비스 재워", "!서비스 재시작", "!서비스 재시작 프록시", "!서비스 컴퓨터", "!서비스 컴퓨터 삭제 프록시",
                     "!서비스 프록시 재워", "!서비스 프록시 깨워"):
            self.assertIn("`!컴퓨터`로", self.bot.handle("u", text), text)
            self.assertNotIn("u", self.bot.pending)
        self.assertIn("`!컴퓨터`로", self.bot.handle("u", "!서비스 프록시 재시작"))  # 나중에 서비스 재시작이 쓸 자리
        self.assertNotIn("u", self.bot.pending)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.woken, [])
        self.assertIn("프록시", sysadmin.computers())

    def test_computer_list_words(self):
        self.computer_setup(up=True)
        for text in ("!컴퓨터", "!컴퓨터 목록"):
            self.assertIn("컴퓨터 목록", self.bot.handle("u", text), text)
        self.assertIn("사용법", self.bot.handle("u", "!컴퓨터 프록시"))
        self.assertIn("관리자만", self.bot.handle("u", "!컴퓨터 절전", allow_admin=False))
        self.assertIn("!컴퓨터", self.bot.handle("u", "!서비스 nginx 중지"))

    def test_computer_sentences(self):
        self.computer_setup()
        for text, say in (("!컴퓨터 프록시 리붓해", "강제 재시작"), ("!컴퓨터 프록시 재시작해 줘", "강제 재시작"),
                          ("!컴퓨터 프록시 재시작 해 줘", "강제 재시작"), ("!컴퓨터 프록시를 reboot", "강제 재시작"),
                          ("!컴퓨터 프록시 재시작해라", "강제 재시작"), ("!컴퓨터 프록시 재시작 해라", "강제 재시작"),
                          ("!컴퓨터 프록시 재부팅해봐", "강제 재시작"), ("!컴퓨터 프록시 재시작하자", "강제 재시작"),
                          ("!컴퓨터 프록시 재시작시켜라", "강제 재시작"), ("!컴퓨터 프록시 리붓해라", "강제 재시작"),
                          ("!컴퓨터 프록시 좀 재워주세요", "절전합니다"), ("!컴퓨터 프록시를 절전해줘", "절전합니다"),
                          ("!컴퓨터 프록시 재워라", "절전합니다"), ("!컴퓨터 프록시 재워봐", "절전합니다"), ("!컴퓨터 프록시 재워 봐", "절전합니다"),
                          ("!컴퓨터 프록시 재워줘라", "절전합니다"), ("!컴퓨터 프록시 재워줄래", "절전합니다"), ("!컴퓨터 프록시 재우자", "절전합니다"),
                          ("!컴퓨터 프록시 절전해라", "절전합니다"), ("!컴퓨터 프록시 절전하자", "절전합니다"),
                          ("!컴퓨터 서버 재시작해", "서버를 재시작합니다"), ("!컴퓨터 이 서버 재부팅해줘", "서버를 재시작합니다"),
                          ("!컴퓨터 서버를 재워줘", "서버를 절전합니다")):
            self.assertIn(say, self.bot.handle("u", text), text)
            self.bot.handle("u", "아니")
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(self.spawned, [])
        sysadmin.set_mac("프록시", "f0:bf:97:00:00:01")
        for text in ("!컴퓨터 프록시를 깨워줘", "!컴퓨터 프록시 좀 켜주세요", "!컴퓨터 프록시 깨워라", "!컴퓨터 프록시 깨워봐",
                     "!컴퓨터 프록시 깨워줄래", "!컴퓨터 프록시 깨우자", "!컴퓨터 프록시 켜라", "!컴퓨터 프록시 켜봐", "!컴퓨터 프록시 켜줄래"):
            self.assertIn("깨우기 신호", self.bot.handle("u", text), text)
        for text in ("!컴퓨터 목록 보여줘", "!컴퓨터 목록 좀 알려줘", "!컴퓨터 보여줘"):
            self.assertIn("컴퓨터 목록", self.bot.handle("u", text), text)
    def test_computer_sentence_with_unknown_word_runs_nothing(self):
        self.computer_setup()
        sysadmin.set_mac("프록시", "f0:bf:97:00:00:01")
        for text in ("!컴퓨터 프록시 재시작하지 마", "!컴퓨터 프록시 재시작 하지 마", "!컴퓨터 프록시 어제 재시작", "!컴퓨터 프록시 재시작해도 돼",
                     "!컴퓨터 프록시 재시작 말고 절전", "!컴퓨터 프록시 안 재워", "!컴퓨터 서버 프록시 재시작", "!컴퓨터 서버 깨워",
                     "!컴퓨터 프록시 재시작할까", "!컴퓨터 프록시 재시작했어", "!컴퓨터 프록시 재웠어", "!컴퓨터 프록시 꺼", "!컴퓨터 프록시 꺼줘",
                     "!컴퓨터 철수를 재시작해", "!컴퓨터 프록시", "!컴퓨터 서버"):
            out = self.bot.handle("u", text)
            self.assertIn("컴퓨터 목록", out, text)
            self.assertNotIn("u", self.bot.pending, text)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.woken, [])
    def test_computer_needs_bang(self):
        self.computer_setup()
        for text in ("프록시 재시작해", "프록시 재워줘", "서버 절전해", "컴퓨터 프록시 재시작", "서비스 nginx 상태"):
            self.assertIn("요약됨", self.bot.handle("u", text), text)  # 일반 대화 (모델은 가짜)
            self.assertNotIn("u", self.bot.pending, text)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(self.spawned, [])

    def test_service_sentences(self):
        config.SYSADMIN_CONTROL = True
        for text in ("!서비스 목록 보여줘", "!서비스 목록 좀 알려줘", "!서비스 보여줘", "!서비스 상태"):
            self.state_run({"nginx": ("loaded", "active")})
            self.assertIn("서비스 목록", self.bot.handle("u", text), text)
        for text in ("!서비스 nginx 상태 알려줘", "!서비스 nginx 어때", "!서비스 nginx를 보여줘", "!서비스 nginx 상태 좀 알려주세요"):
            self.ran = []
            subprocess.run = lambda cmd, **kw: self.ran.append(cmd) or Result("nginx 상태 출력")
            self.assertIn("nginx 상태 출력", self.bot.handle("u", text), text)
            self.assertEqual(self.ran[-1][:3], ["systemctl", "status", "nginx"], text)
        self.ran = []
        for text in ("!서비스 nginx 재시작해줘", "!서비스 nginx 리붓해", "!서비스 nginx 재시작 해 줘"):
            self.assertIn("상태만 볼 수 있어요", self.bot.handle("u", text), text)
        for text in ("!서비스 nginx 중지해줘", "!서비스 nginx 시작해줘", "!서비스 nginx 꺼줘"):
            self.assertIn("상태만 볼 수 있어요", self.bot.handle("u", text), text)
        self.assertIn("`!컴퓨터`로", self.bot.handle("u", "!서비스 소니 재워줘"))
        self.assertEqual(self.ran, [])

    def test_new_words_cannot_be_names(self):
        for name in ("서버", "리붓", "리부트해", "재시작해주세요", "좀", "보여줘"):
            self.assertIn("이름은", sysadmin.check_name(name) or "", name)

    def test_computer_list_hides_address_and_mac(self):
        self.computer_setup(up=True)
        out = self.bot.handle("u", "!컴퓨터")
        self.assertIn("- 이 서버: 켜짐 / 절전 가능 / 깨우기 안 됨", out)
        self.assertIn("- 프록시: 켜짐 / 절전 가능 / 깨우기 준비 안 됨", out)
        sysadmin.set_mac("프록시", "f0:bf:97:00:00:01")
        out = self.bot.handle("u", "!컴퓨터")
        self.assertIn("깨우기 가능", out)
        self.assertNotIn("10.0.0.9", out)
        self.assertNotIn("f0:bf", out)

    def test_register_only_in_private_place(self):
        self.computer_setup()
        out = self.bot.handle("u", "!컴퓨터 등록 작업컴 ubuntu@10.0.0.8", private=False)
        self.assertIn("DM 이나 등록한 서버·채널", out)
        self.assertNotIn("작업컴", sysadmin.computers())
        self.assertNotIn("10.0.0.8", out)
        self.assertIn("등록했어요", self.bot.handle("u", "!컴퓨터 등록 작업컴 ubuntu@10.0.0.8"))
        self.assertEqual(sysadmin.computers()["작업컴"], ("ubuntu@10.0.0.8", None))

    def test_update_only_in_private_place(self):
        self.computer_setup()
        out = self.bot.handle("u", "!컴퓨터 수정 프록시 ubuntu@10.0.0.8", private=False)
        self.assertIn("DM 이나 등록한 서버·채널", out)
        self.assertNotIn("10.0.0.8", out)
        self.assertEqual(sysadmin.computers()["프록시"][0], "ubuntu@10.0.0.9")

    def test_update_changes_target_and_never_shows_it(self):
        self.computer_setup()
        out = self.bot.handle("u", "!컴퓨터 수정 프록시 dnova@10.0.0.8")
        self.assertIn("바꿨어요", out)
        self.assertIn("접속되지 않아요", out)
        self.assertNotIn("10.0.0.8", out)
        self.assertEqual(sysadmin.computers()["프록시"][0], "dnova@10.0.0.8")
        sysadmin.computer_up = lambda name: True
        out = self.bot.handle("u", "!컴퓨터 수정 프록시 ubuntu@10.0.0.8")
        self.assertIn("접속돼요", out)
        self.assertNotIn("10.0.0.8", out)

    def test_update_keeps_mac_only_for_same_address(self):
        self.computer_setup()
        sysadmin.set_mac("프록시", "f0:bf:97:00:00:01")
        self.bot.handle("u", "!컴퓨터 수정 프록시 dnova@10.0.0.9")
        self.assertEqual(sysadmin.computers()["프록시"], ("dnova@10.0.0.9", "f0:bf:97:00:00:01"))
        self.bot.handle("u", "!컴퓨터 수정 프록시 dnova@10.0.0.8")
        self.assertEqual(sysadmin.computers()["프록시"], ("dnova@10.0.0.8", None))

    def test_update_rejects_bad_input(self):
        self.computer_setup()
        self.assertIn("없어요", self.bot.handle("u", "!컴퓨터 수정 없는것 ubuntu@10.0.0.8"))
        for target in ("-oProxyCommand=x", "ubuntu@8.8.8.8", "ubuntu@example.com", "한글@10.0.0.8", "<사용자>@10.0.0.8"):
            self.assertIn("접속 정보는", self.bot.handle("u", f"!컴퓨터 수정 프록시 {target}"), target)
        self.assertIn("사용법", self.bot.handle("u", "!컴퓨터 수정 프록시"))
        self.assertEqual(sysadmin.computers()["프록시"][0], "ubuntu@10.0.0.9")
        self.assertIn("이름은", sysadmin.check_name("수정"))

    def test_update_cancels_waiting_confirm(self):
        self.computer_setup()
        self.bot.handle("u", "!컴퓨터 프록시 재워")
        self.bot.handle("c", "!컴퓨터 수정 프록시 ubuntu@10.0.0.8")
        self.assertIn("없습니다", self.bot.handle("u", "!확인"))
        self.assertEqual(self.ssh_calls(), [])

    def test_update_needs_control_and_admin(self):
        self.computer_setup()
        self.assertIn("관리자만", self.bot.handle("u", "!컴퓨터 수정 프록시 ubuntu@10.0.0.8", allow_admin=False))
        config.SYSADMIN_CONTROL = False
        self.assertIn("꺼져", self.bot.handle("u", "!컴퓨터 수정 프록시 ubuntu@10.0.0.8"))
        self.assertEqual(sysadmin.computers()["프록시"][0], "ubuntu@10.0.0.9")

    def test_register_reply_never_shows_address(self):
        for up in (True, False):
            self.computer_setup(up=up)
            out = self.bot.handle("u", "!컴퓨터 등록 작업컴 ubuntu@10.0.0.8")
            self.assertIn("등록했어요", out)
            self.assertNotIn("10.0.0.8", out)
            self.computer_restore()

    def test_register_rejects_unsafe_targets(self):
        self.computer_setup()
        for target in ("-oProxyCommand=sh", "ubuntu@-oProxyCommand=sh", "ubuntu@8.8.8.8", "ubuntu@example.com", "ubuntu",
                       "우분투@10.0.0.8", "-x@10.0.0.8", "ubuntu@127.0.0.1", "ubuntu@10.0.0.999", "a;b@10.0.0.8"):
            self.assertIn("형식으로", self.bot.handle("u", f"!컴퓨터 등록 작업컴 {target}"), target)
        self.assertNotIn("작업컴", sysadmin.computers())

    def test_register_rejects_bad_or_used_names(self):
        self.computer_setup()
        for name in ("재워", "컴퓨터", "목록", "등록", "삭제", "이거", "-x", "a;b"):
            self.assertIn("이름은", self.bot.handle("u", f"!컴퓨터 등록 {name} ubuntu@10.0.0.8"), name)
        self.assertIn("이미 있어요", self.bot.handle("u", "!컴퓨터 등록 프록시 ubuntu@10.0.0.8"))
        self.assertEqual(sysadmin.computers()["프록시"][0], "ubuntu@10.0.0.9")
        self.assertIn("사용법", self.bot.handle("u", "!컴퓨터 등록 작업컴"))

    def test_admin_commands_need_control_but_list_does_not(self):
        self.computer_setup()
        config.SYSADMIN_CONTROL = False
        for text in ("!컴퓨터 등록 작업컴 ubuntu@10.0.0.8", "!컴퓨터 삭제 프록시", "!컴퓨터 이름변경 프록시 새이름"):
            self.assertIn("꺼져", self.bot.handle("u", text), text)
        self.assertEqual(list(sysadmin.computers()), ["프록시"])
        self.assertIn("- 프록시: 응답 없음 / 조회만 / 조회만", self.bot.handle("u", "!컴퓨터"))

    def test_admin_commands_refused_without_admin(self):
        self.computer_setup()
        for text in ("!컴퓨터 등록 작업컴 ubuntu@10.0.0.8", "!컴퓨터 삭제 프록시", "!컴퓨터 이름변경 프록시 새이름"):
            self.assertIn("관리자만", self.bot.handle("u", text, allow_admin=False), text)
        self.assertEqual(list(sysadmin.computers()), ["프록시"])

    def test_delete_needs_confirm(self):
        self.computer_setup()
        self.assertIn("없어요", self.bot.handle("u", "!컴퓨터 삭제 없는것"))
        self.assertIn("지울까요", self.bot.handle("u", "!컴퓨터 삭제 프록시"))
        self.assertIn("프록시", sysadmin.computers())
        self.assertEqual(self.bot.handle("u", "아니"), "컴퓨터 삭제를 취소했습니다.")
        self.bot.handle("u", "!컴퓨터 삭제 프록시")
        self.assertIn("취소했어요", self.bot.handle("u", "안녕"))
        self.assertIn("프록시", sysadmin.computers())
        self.bot.handle("u", "!컴퓨터 삭제 프록시")
        self.bot.pending["u"] = (0, 0, "프록시")
        self.assertIn("취소", self.bot.handle("u", "!확인"))
        self.assertIn("프록시", sysadmin.computers())
        self.bot.handle("u", "!컴퓨터 삭제 프록시")
        self.assertIn("지웠어요", self.bot.handle("u", "지워"))
        self.assertNotIn("프록시", sysadmin.computers())
    def test_rename_keeps_target_and_mac(self):
        self.computer_setup()
        sysadmin.set_mac("프록시", "f0:bf:97:00:00:01")
        self.assertIn("nginx컴", self.bot.handle("u", "!컴퓨터 이름변경 프록시 nginx컴"))
        self.assertEqual(sysadmin.computers(), {"nginx컴": ("ubuntu@10.0.0.9", "f0:bf:97:00:00:01")})
        self.assertIn("깨우기 신호", self.bot.handle("u", "!컴퓨터 nginx컴 깨워"))

    def test_rename_rejects_bad_names_and_works_in_channel(self):
        self.computer_setup()
        sysadmin.add_computer("작업컴", "ubuntu@10.0.0.8")
        self.assertIn("없어요", self.bot.handle("u", "!컴퓨터 이름변경 없는것 새이름"))
        self.assertIn("이미 있어요", self.bot.handle("u", "!컴퓨터 이름변경 프록시 작업컴"))
        self.assertIn("이름은", self.bot.handle("u", "!컴퓨터 이름변경 프록시 재워"))
        self.assertIn("바꿨어요", self.bot.handle("u", "!컴퓨터 이름변경 프록시 새이름", private=False))

    def test_rename_cancels_waiting_sleep(self):
        self.computer_setup()
        self.bot.handle("u", "!컴퓨터 프록시 재워")
        self.bot.pending["b"] = self.bot.pending["u"]
        self.bot.handle("c", "!컴퓨터 이름변경 프록시 새이름")
        self.assertIn("없습니다", self.bot.handle("b", "!확인"))
        self.assertEqual(self.ssh_calls(), [])

    def test_hidden_columns_in_db_views(self):
        self.computer_setup()
        out = self.bot.handle("u", "!db computers")
        self.assertIn("프록시", out)
        self.assertNotIn("10.0.0.9", out)
        info = self.bot.handle("u", "!db computers 컬럼")
        self.assertNotIn("target", info.split("칸 이름:")[1])
        self.assertIn("실행 실패", self.bot.handle("u", "!sql SELECT target FROM computers"))
        self.assertIn("실행 실패", self.bot.handle("u", "!sql SELECT * FROM computers"))

    def test_read_mac_when_table_is_empty(self):
        self.computer_setup()
        subprocess.run = lambda cmd, **kw: Result("10.0.0.9 dev enp2s0 FAILED\n")
        self.assertIsNone(sysadmin.read_mac("프록시"))

    def test_wake_packet(self):
        sent = []
        real = sysadmin.socket.socket

        class Fake:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def setsockopt(self, *a): pass
            def sendto(self, data, addr): sent.append((data, addr))
        sysadmin.socket.socket = lambda *a: Fake()
        self.addCleanup(setattr, sysadmin.socket, "socket", real)
        sysadmin.wake("f0:bf:97:00:00:01")
        data, addr = sent[0]
        self.assertEqual(addr, ("255.255.255.255", 9))
        self.assertEqual(data, b"\xff" * 6 + bytes.fromhex("f0bf97000001") * 16)

    def test_checkup_uses_model_with_real_data(self):
        out = self.bot.handle("u", "!점검")
        self.assertEqual(out, "요약됨")
        prompt = self.llm_calls[0][0]["content"]
        self.assertIn("CPU", prompt); self.assertIn("df -h", prompt)


if __name__ == "__main__":
    unittest.main()
