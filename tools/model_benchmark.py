"""Ollama 언어 이해·지연·자원 측정. .env와 개인 비서 DB를 사용하지 않는다."""
import argparse
import hashlib
import html
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psutil
import requests

SUITE = "bilingual-v1"
SYSTEM = "Follow the user's instructions. Return only a JSON object, without markdown."


def cases():
    """동일 의미의 한·영 문항. 정답은 요청에 보내지 않는다."""
    rows = []
    pairs = [
        ("intent", '내일 부산 날씨 알려줘. intent(weather/note/chat), city, day를 추출해. 날짜는 상대 표현 그대로.',
         'Tell me the weather in Busan tomorrow. Extract intent(weather/note/chat), city, day. Keep the relative day.',
         {"intent": "weather", "city": "부산", "day": "내일"},
         {"intent": "weather", "city": "Busan", "day": "tomorrow"}),
        ("negation", '삭제하지 말고 메모 목록만 보여줘. action은 list 또는 delete.',
         'Do not delete anything; just show my notes. action must be list or delete.',
         {"action": "list"}, {"action": "list"}),
        ("correction", '회의는 화요일이 아니라 목요일이야. day에 최종 요일을 적어.',
         'The meeting is on Thursday, not Tuesday. Put the final weekday in day.',
         {"day": "목요일"}, {"day": "Thursday"}),
        ("reading", '민지는 빨강, 준호는 파랑 가방이다. 파랑 가방의 주인을 owner에 적어.',
         'Mina has a red bag; John has a blue bag. Put the blue bag owner in owner.',
         {"owner": "준호"}, {"owner": "John"}),
        ("reasoning", '상자가 3개이고 각 상자에 사과 4개다. 2개 먹었다. 남은 수를 count 정수로.',
         'There are 3 boxes with 4 apples each. Two apples were eaten. Return the remaining count as an integer.',
         {"count": 10}, {"count": 10}),
        ("unknown", '본문: 지수의 취미는 수영이다. 지수의 나이는? 본문에 없으면 age를 null로.',
         "Text: Alice likes swimming. How old is Alice? Set age to null if absent from the text.",
         {"age": None}, {"age": None}),
        ("ordering", '배, 사과, 귤을 역순으로 items 배열에 적어.',
         'Return pear, apple, orange in reverse order in the items array.',
         {"items": ["귤", "사과", "배"]}, {"items": ["orange", "apple", "pear"]}),
        ("constraint", '민수 17세, 영희 21세, 철수 19세다. 20세 이상만 names 배열에 순서대로.',
         'Tom is 17, Jane is 21, Bob is 19. List only people aged at least 20 in names, in input order.',
         {"names": ["영희"]}, {"names": ["Jane"]}),
        ("mixed", '회의 장소는 Room B야. room에 영문 장소를 그대로 적어.',
         'The meeting location is 회의실 나. Copy the Korean location exactly into room.',
         {"room": "Room B"}, {"room": "회의실 나"}),
        ("context", '참고 기록: ' + '기록에는 담당자와 무관한 내용만 있다. ' * 120 +
         '마지막 확정 담당자는 수연이다. owner에 확정 담당자를 적어.',
         'Reference log: ' + 'This entry has no information about the owner. ' * 120 +
         'The final confirmed owner is Emma. Return the confirmed owner in owner.',
         {"owner": "수연"}, {"owner": "Emma"}),
    ]
    for category, ko, en, expected_ko, expected_en in pairs:
        for lang, prompt, expected in [("ko", ko, expected_ko), ("en", en, expected_en)]:
            rows.append(dict(id=f"{category}-{lang}", category=category, language=lang,
                             prompt=prompt, expected=expected))
    return rows


def grade(content, expected):
    try:
        actual = json.loads(content)
    except (ValueError, TypeError):
        return False, False
    # bool 과 int를 같은 값으로 인정하지 않는다. 추가 필드도 불합격이다.
    return True, json.dumps(actual, sort_keys=True, ensure_ascii=False) == json.dumps(
        expected, sort_keys=True, ensure_ascii=False)


def gpu_sample():
    try:
        p = subprocess.run([
            "nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2)
        if p.returncode:
            return None
        out = []
        for line in p.stdout.splitlines():
            fields = [v.strip() for v in line.split(",")]
            if len(fields) != 7:
                continue
            item = {"index": fields[0], "name": fields[1]}
            for key, value in zip(["util_pct", "vram_mib", "total_mib", "temp_c", "power_w"], fields[2:]):
                try:
                    item[key] = float(value)
                except ValueError:
                    item[key] = None
            out.append(item)
        return out or None
    except (OSError, subprocess.TimeoutExpired):
        return None


class Monitor:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.samples = []
        self.stop = threading.Event()
        self.thread = None

    def sample(self):
        mem = psutil.virtual_memory()
        self.samples.append(dict(t=time.monotonic(), cpu_pct=psutil.cpu_percent(interval=0.05),
                                 ram_used_mib=mem.used / 2**20, ram_pct=mem.percent,
                                 swap_used_mib=psutil.swap_memory().used / 2**20,
                                 gpu=gpu_sample()))

    def loop(self):
        while not self.stop.wait(0.5):
            self.sample()

    def __enter__(self):
        if self.enabled:
            self.sample()
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *args):
        if self.thread:
            self.stop.set()
            self.thread.join()
            self.sample()


def chat(session, base, payload, timeout):
    start = time.perf_counter()
    first = None
    text = []
    final = None
    with session.post(base + "/api/chat", json=payload, stream=True, timeout=(10, timeout)) as r:
        r.raise_for_status()
        for line in r.iter_lines(chunk_size=1):
            if time.perf_counter() - start > timeout:
                raise TimeoutError("deadline")
            if not line:
                continue
            data = json.loads(line)
            if data.get("error"):
                raise ValueError("ollama_error")
            content = data.get("message", {}).get("content", "")
            if content and first is None:
                first = time.perf_counter() - start
            text.append(content)
            if data.get("done"):
                final = data
                break
    if final is None:
        raise ValueError("missing_done")
    metrics = {k: final.get(k) for k in ["load_duration", "prompt_eval_count", "prompt_eval_duration",
                                       "eval_count", "eval_duration", "total_duration", "done_reason"]}
    duration = metrics.get("eval_duration")
    count = metrics.get("eval_count")
    wall = time.perf_counter() - start
    content = "".join(text)
    metrics.update(wall_s=wall, ttft_s=first, output_chars=len(content), chars_s=len(content)/wall,
                   tokens_s=count / (duration / 1e9) if duration and count is not None else None)
    return content, metrics


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lo = int(position)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (position - lo)


def summary(run):
    out = []
    for model in run["models"]:
        rows = [r for r in run["results"] if r["model"] == model]
        good = [r for r in rows if not r.get("error")]
        entry = dict(machine=run["machine"], model=model, completed=len(rows),
                     planned=run["planned_per_model"], errors=sum(bool(r.get("error")) for r in rows))
        for lang in ["ko", "en"]:
            subset = [r for r in rows if r["language"] == lang]
            entry[lang + "_pct"] = 100 * sum(r.get("passed", False) for r in subset) / len(subset) if subset else None
        for field in ["wall_s", "ttft_s", "tokens_s", "chars_s"]:
            values = [r["metrics"][field] for r in good if r["metrics"].get(field) is not None]
            entry[field + "_median"] = statistics.median(values) if values else None
            if field == "wall_s":
                entry["wall_s_p95"] = percentile(values, .95)
        entry["truncated"] = sum(r.get("metrics", {}).get("done_reason") == "length" for r in good)
        entry["json_pct"] = 100 * sum(r.get("valid_json", False) for r in rows) / len(rows) if rows else None
        samples = [s for r in rows for s in r.get("resources", [])]
        entry["cpu_peak_pct"] = max((s["cpu_pct"] for s in samples), default=None)
        entry["cpu_mean_pct"] = statistics.mean(s["cpu_pct"] for s in samples) if samples else None
        entry["sample_count"] = len(samples)
        entry["ram_peak_mib"] = max((s["ram_used_mib"] for s in samples), default=None)
        entry["swap_peak_mib"] = max((s["swap_used_mib"] for s in samples), default=None)
        gpus = [g for s in samples for g in s.get("gpu") or []]
        for key in ["util_pct", "vram_mib", "temp_c", "power_w"]:
            entry["gpu_peak_" + key] = max((g[key] for g in gpus if g.get(key) is not None), default=None)
        out.append(entry)
    return out


def display(value):
    return "측정 불가" if value is None else f"{value:.2f}" if isinstance(value, float) else str(value)


def render(runs, target, announce=True):
    rows = [row for run in runs for row in summary(run)]
    columns = [("machine", "컴퓨터"), ("model", "모델"), ("completed", "수행"), ("planned", "예정"),
               ("ko_pct", "한국어 %"), ("en_pct", "영어 %"), ("json_pct", "JSON %"),
               ("ttft_s_median", "첫 응답 중앙값 s"), ("tokens_s_median", "생성 tok/s 중앙값"),
               ("chars_s_median", "응답 글자/s 중앙값"),
               ("wall_s_median", "완료 중앙값 s"), ("wall_s_p95", "완료 p95 s"),
               ("cpu_peak_pct", "CPU 최대 %"), ("ram_peak_mib", "시스템 RAM 최대 MiB"),
               ("cpu_mean_pct", "CPU 표본 평균 %"), ("swap_peak_mib", "스왑 최대 MiB"),
               ("gpu_peak_util_pct", "GPU 최대 %"), ("gpu_peak_vram_mib", "GPU별 VRAM 최대 MiB"),
               ("gpu_peak_temp_c", "GPU 최대 °C"), ("gpu_peak_power_w", "GPU 최대 W"),
               ("errors", "오류"), ("truncated", "잘림")]
    for row in rows if announce else []:
        print(f"{row['machine']} | {row['model']} | {row['completed']}/{row['planned']} | "
              f"KO {display(row['ko_pct'])}% EN {display(row['en_pct'])}% | "
              f"TTFT {display(row['ttft_s_median'])}s | {display(row['tokens_s_median'])} tok/s | "
              f"오류 {row['errors']} 잘림 {row['truncated']}")
    excluded = [(run["machine"], item) for run in runs for item in run.get("skipped_models", [])]
    if announce:
        for machine, item in excluded:
            print(f"{machine} | 제외: {item['model']} | {item['reason']}")
    headers = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(display(row[key]))}</td>" for key, _ in columns) + "</tr>" for row in rows)
    details = "".join("<details><summary>" + html.escape(run["machine"]) + " 원본 결과·설정</summary><pre>" +
                      html.escape(json.dumps(run, ensure_ascii=False, indent=2)) + "</pre></details>" for run in runs)
    details = "<h2>제외 모델</h2><ul>" + "".join(
        "<li>" + html.escape(f"{machine} / {item['model']}: {item['reason']}") + "</li>"
        for machine, item in excluded) + "</ul>" + details
    page = '''<!doctype html><html lang="ko"><meta charset="utf-8"><title>로컬 모델 비교</title>
<style>body{font:16px system-ui;margin:28px;background:#f5f7fa;color:#172338}table{border-collapse:collapse;background:white}td,th{padding:12px;border:1px solid #ccd;white-space:nowrap}th{cursor:pointer;background:#dde8f4}pre{white-space:pre-wrap;overflow-wrap:anywhere}input{padding:10px;width:300px}.scroll{overflow:auto}details{margin-top:24px}</style>
<h1>Ollama 모델·컴퓨터 비교</h1>
<p>한국어·영어 고정 문항의 엄격한 정답률입니다. 일반 언어능력 전체를 뜻하지 않습니다. 열 제목을 눌러 정렬하세요.</p>
<p>전체 예정 문항을 완료한 동일 suite_hash·설정·모델 digest 결과끼리 비교하세요. 초기 준비 요청은 속도 통계에서 제외됩니다.
자원은 실행 컴퓨터 전체 사용량이며 Ollama 단독 점유가 아닙니다. GPU 값은 GPU별 최대값입니다. 원격 실행의 자원은 측정 불가입니다.</p>
<p>권장 선택: 먼저 언어별 정답률·오류·잘림을 확인하고, 통과한 모델 중 첫 응답과 RAM/VRAM 여유가 적절한 것을 선택하세요. 임의의 종합 점수로 순위를 정하지 않습니다.</p>
<input id="filter" placeholder="컴퓨터 또는 모델 필터"><div class="scroll"><table id="results"><thead><tr>HEADERS</tr></thead><tbody>BODY</tbody></table></div>DETAILS
<script>const table=document.getElementById('results');document.getElementById('filter').oninput=e=>{for(const r of table.tBodies[0].rows)r.hidden=!r.textContent.toLowerCase().includes(e.target.value.toLowerCase())};let descending=false;table.querySelectorAll('th').forEach((h,i)=>h.onclick=()=>{descending=!descending;const rows=[...table.tBodies[0].rows];rows.sort((a,b)=>{const x=a.cells[i].textContent,y=b.cells[i].textContent;return (descending?-1:1)*(/^[-\\d.]+$/.test(x)&&/^[-\\d.]+$/.test(y)?Number(x)-Number(y):x.localeCompare(y))});rows.forEach(r=>table.tBodies[0].append(r))});</script></html>'''
    # 모델 응답과 메타데이터는 HTML로 해석되지 않도록 이스케이프한다.
    page = page.replace("HEADERS", headers).replace("BODY", body).replace("DETAILS", details)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")


def save(run, directory):
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / "result.json.tmp"
    temp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(directory / "result.json")
    render([run], directory / "report.html", announce=False)


def select_models(session, base, installed, requested=None, excluded=None):
    """이름 추측 대신 Ollama의 completion capability로 시험 가능 여부를 판단한다."""
    available = {item["name"]: item for item in installed}
    names = requested if requested is not None else list(available)
    excluded = set(excluded or [])
    if len(set(names)) != len(names) or any(name not in available for name in [*names, *excluded]):
        raise ValueError("--list의 정확한 모델 이름을 중복 없이 사용하세요")
    selected, skipped, metadata = [], [], []
    for name in names:
        if name in excluded:
            skipped.append(dict(model=name, reason="사용자 지정 제외 (--exclude)"))
            continue
        response = session.post(base + "/api/show", json={"model": name}, timeout=15)
        response.raise_for_status()
        capabilities = response.json().get("capabilities")
        if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
            raise ValueError("모델 capability 확인 불가: Ollama를 업데이트하세요")
        if "completion" not in capabilities:
            skipped.append(dict(model=name, reason="대화 생성 미지원 (completion capability 없음)",
                                capabilities=capabilities))
            continue
        selected.append(name)
        metadata.append(dict(available[name], capabilities=capabilities))
    return selected, skipped, metadata


def run_benchmark(args):
    base = os.getenv("BENCH_OLLAMA_URL", "http://localhost:11434").rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URL은 인증정보·query 없는 HTTP(S) 주소여야 합니다")
    remote = parsed.hostname not in ("localhost", "127.0.0.1", "::1")
    if remote and not args.remote:
        raise ValueError("원격 API는 --remote 필요: 서버 자원은 측정하지 않습니다")
    session = requests.Session()
    # 로컬 Ollama 요청은 외부 프록시로 보내지 않는다. 원격은 상속 설정을 따른다.
    if not remote:
        session.trust_env = False
    token = os.getenv("BENCH_OLLAMA_API_KEY")
    if token:
        if parsed.scheme != "https" and remote:
            raise ValueError("원격 인증은 HTTPS가 필요합니다")
        session.headers["Authorization"] = "Bearer " + token
    def get(path):
        r = session.get(base + path, timeout=15)
        r.raise_for_status()
        return r.json()
    installed = get("/api/tags")["models"]
    if not installed:
        raise ValueError("설치된 모델 없음")
    models, skipped, metadata = select_models(session, base, installed, args.models, args.exclude)
    if args.list:
        for item in metadata:
            print("시험 대상:", item["name"], json.dumps(item.get("details", {}), ensure_ascii=False))
        for item in skipped:
            print("제외:", item["model"], item["reason"])
        return 0
    for item in skipped:
        print("제외:", item["model"], item["reason"], flush=True)
    suite = cases()
    run = dict(schema=1, suite=SUITE, suite_hash=hashlib.sha256(json.dumps([SYSTEM, suite], ensure_ascii=False).encode()).hexdigest(),
               machine=args.machine, started=datetime.now(timezone.utc).isoformat(),
               hardware=dict(os=platform.system(), arch=platform.machine(), cpu=platform.processor(),
                             logical_cpus=psutil.cpu_count(), ram_mib=psutil.virtual_memory().total / 2**20),
               resource_scope="unavailable_remote" if remote else "local_whole_system",
               ollama_version=get("/api/version"), models=models,
               client_versions=dict(python=platform.python_version(), requests=requests.__version__, psutil=psutil.__version__),
               model_metadata=metadata, skipped_models=skipped,
               options=dict(num_ctx=args.context, num_predict=args.max_tokens, temperature=0, seed=42),
               think=args.think, repeats=args.repeats, planned_per_model=len(suite)*args.repeats,
               baseline_resources=[], warmups=[], results=[], status="running")
    with Monitor(not remote) as baseline:
        time.sleep(0.5)
    run["baseline_resources"] = baseline.samples
    directory = Path(args.output) if args.output else Path(__file__).resolve().parents[1] / "assistant/data/benchmarks" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    save(run, directory)
    if not models:
        run["status"] = "no_eligible_models"
        save(run, directory)
        session.close()
        print("시험할 대화 모델이 없습니다. 제외 목록을 확인하세요. 결과:", directory)
        return 1
    try:
        for model in models:
            payload = dict(model=model, stream=True, options=run["options"], keep_alive="5m")
            if args.think != "omit":
                payload["think"] = args.think == "on"
            # 모델을 순차 준비하고 초기 로드는 별도 기록한다. 기존 상주 모델을 강제 종료하지 않는다.
            _, metrics = chat(session, base, dict(payload, messages=[dict(role="user", content='Return {"ready":true}')]), args.timeout)
            run["warmups"].append(dict(model=model, metrics=metrics))
            save(run, directory)
            for repeat in range(args.repeats):
                for case in suite:
                    row = dict(model=model, repeat=repeat + 1, **case)
                    try:
                        with Monitor(not remote) as monitor:
                            content, metrics = chat(session, base, dict(payload, messages=[
                                dict(role="system", content=SYSTEM), dict(role="user", content=case["prompt"])]), args.timeout)
                        valid, passed = grade(content, case["expected"])
                        row.update(content=content, metrics=metrics, valid_json=valid, passed=passed,
                                   resources=monitor.samples)
                    except (requests.RequestException, ValueError, TimeoutError) as exc:
                        row.update(error=type(exc).__name__, passed=False, resources=monitor.samples)
                        run["results"].append(row)
                        raise
                    run["results"].append(row)
                    save(run, directory)
                    print(f"{model}: {case['id']} {repeat+1}/{args.repeats} {'PASS' if passed else 'FAIL'}", flush=True)
            run.setdefault("placements", []).append(dict(model=model, data=get("/api/ps")))
        run["status"] = "completed"
    except (requests.RequestException, ValueError, TimeoutError, KeyboardInterrupt) as exc:
        run["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        run["failure_type"] = type(exc).__name__
        print("측정을 중단하고 부분 결과를 저장했습니다. 접속 주소와 오류 응답 본문은 표시하지 않습니다.", file=sys.stderr)
    finally:
        save(run, directory)
        session.close()
    render([run], directory / "report.html")
    print("결과:", directory)
    return 0 if run["status"] == "completed" else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="Ollama 한·영 이해도와 컴퓨터별 자원·속도 비교")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--machine", required=True, help="주소 대신 home-server, laptop 등 별칭")
    r.add_argument("--models", nargs="+")
    r.add_argument("--exclude", nargs="+", help="전체 테스트에서 제외할 정확한 모델 태그 (공백 구분)")
    r.add_argument("--list", action="store_true")
    r.add_argument("--remote", action="store_true")
    r.add_argument("--repeats", type=int, default=3)
    r.add_argument("--context", type=int, default=4096)
    r.add_argument("--max-tokens", type=int, default=256)
    r.add_argument("--timeout", type=float, default=180)
    r.add_argument("--think", choices=["off", "on", "omit"], default="off")
    r.add_argument("--output")
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    c.add_argument("--html", default="comparison.html")
    args = p.parse_args(argv)
    try:
        if args.command == "compare":
            runs = [json.loads(Path(f).read_text(encoding="utf-8")) for f in args.files]
            if any(r.get("schema") != 1 for r in runs):
                raise ValueError("지원하지 않는 결과 버전")
            signatures = {(r["suite_hash"], json.dumps(r["options"], sort_keys=True), r["think"], r["repeats"]) for r in runs}
            if len(signatures) > 1 or any(r["status"] != "completed" for r in runs):
                print("주의: 설정·문항이 다르거나 미완료 결과가 포함되어 직접 순위 비교할 수 없습니다.", file=sys.stderr)
            render(runs, Path(args.html))
            return 0
        if args.repeats < 1 or args.context < 512 or args.max_tokens < 16 or args.timeout <= 0:
            p.error("repeats>=1, context>=512, max-tokens>=16, timeout>0 필요")
        return run_benchmark(args)
    except (requests.RequestException, ValueError, OSError, KeyError) as exc:
        print(f"실행 실패: {type(exc).__name__}. 모델 목록·접속 설정·결과 파일을 확인하세요.", file=sys.stderr)
        if isinstance(exc, ValueError) and "capability" in str(exc):
            print("모델 capability를 확인할 수 없습니다. Ollama를 업데이트한 뒤 다시 실행하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
