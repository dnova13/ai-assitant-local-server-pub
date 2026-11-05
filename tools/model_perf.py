"""Ollama 순수 성능 측정: 로딩 시간, 입력 처리(prefill)·생성(decode) 속도, 첫 응답, GPU 적재 비율, 자원.
답의 내용은 채점하지 않는다. .env와 개인 비서 DB를 사용하지 않는다.
자원 측정·스트리밍 요청·모델 선택은 model_benchmark.py의 것을 재사용한다.

사용: python tools/model_perf.py run --machine home-server [--models 태그 ...] [--contexts 2048 4096]
      python tools/model_perf.py compare a/result.json b/result.json --html comparison.html
"""
import argparse
import html
import json
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model_benchmark as bench  # gpu_sample, Monitor, chat, select_models, percentile 재사용

import psutil
import requests

SUITE = "perf-v1"
FILLER = "The quick brown fox jumps over the lazy dog near the river bank. "  # 약 13토큰
FILLER_TOKENS = 13
GEN_PROMPT = "Count upward from 1, one number per line. Do not stop and do not add any other text."
CTX_MARGIN = 0.8  # 입력이 num_ctx의 80%를 넘으면 그 조건은 건너뛴다 (잘림 방지)


def prefill_prompt(target_tokens, nonce):
    """맨 앞에 매번 다른 값을 넣어 Ollama의 프롬프트 캐시를 피한다."""
    repeat = max(1, target_tokens // FILLER_TOKENS)
    return f"[run {nonce}] Read the text and reply with OK.\n" + FILLER * repeat


def rate(count, duration_ns):
    return count / (duration_ns / 1e9) if count and duration_ns else None


def unload(session, base, model):
    """이 모델만 메모리에서 내린다 (keep_alive 0). 다른 모델은 건드리지 않는다."""
    r = session.post(base + "/api/generate", json={"model": model, "keep_alive": 0}, timeout=60)
    r.raise_for_status()


def placement(ps, model):
    """/api/ps에서 이 모델의 크기와 GPU에 올라간 비율."""
    for item in ps.get("models", []):
        if item.get("name") == model or item.get("model") == model:
            size, vram = item.get("size"), item.get("size_vram")
            return dict(size_mib=size / 2**20 if size else None,
                        vram_mib=vram / 2**20 if vram is not None else None,
                        gpu_pct=100 * vram / size if size and vram is not None else None)
    return dict(size_mib=None, vram_mib=None, gpu_pct=None)


def connect(args):
    base = os.getenv("BENCH_OLLAMA_URL", "http://localhost:11434").rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URL은 인증정보·query 없는 HTTP(S) 주소여야 합니다")
    remote = parsed.hostname not in ("localhost", "127.0.0.1", "::1")
    if remote and not args.remote:
        raise ValueError("원격 API는 --remote 필요: 서버 자원은 측정하지 않습니다")
    session = requests.Session()
    if not remote:
        session.trust_env = False  # 로컬 요청은 프록시를 거치지 않는다
    token = os.getenv("BENCH_OLLAMA_API_KEY")
    if token:
        if parsed.scheme != "https" and remote:
            raise ValueError("원격 인증은 HTTPS가 필요합니다")
        session.headers["Authorization"] = "Bearer " + token
    return session, base, remote


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def summary(run):
    """(컴퓨터, 모델, num_ctx)마다 한 줄."""
    out = []
    for load in run["loads"]:
        model, ctx = load["model"], load["num_ctx"]
        rows = [r for r in run["results"] if r["model"] == model and r["num_ctx"] == ctx]
        good = [r for r in rows if not r.get("error")]
        entry = dict(machine=run["machine"], model=model, num_ctx=ctx, errors=sum(bool(r.get("error")) for r in rows),
                     load_s=load["metrics"].get("load_s"), **load.get("placement", {}))
        for size in run["prompt_sizes"]:
            subset = [r for r in good if r["kind"] == "prefill" and r["target"] == size]
            entry[f"prefill_{size}_tok_s"] = median(r["metrics"].get("prefill_tok_s") for r in subset)
            entry[f"prefill_{size}_ttft_s"] = median(r["metrics"].get("ttft_s") for r in subset)
            entry[f"prefill_{size}_tokens"] = median(r["metrics"].get("prompt_eval_count") for r in subset)
        for length in run["gen_lengths"]:
            subset = [r for r in good if r["kind"] == "decode" and r["target"] == length]
            entry[f"decode_{length}_tok_s"] = median(r["metrics"].get("tokens_s") for r in subset)
            entry[f"decode_{length}_tokens"] = median(r["metrics"].get("eval_count") for r in subset)
        samples = [s for r in rows for s in r.get("resources", [])]
        gpus = [g for s in samples for g in s.get("gpu") or []]
        entry["cpu_peak_pct"] = max((s["cpu_pct"] for s in samples), default=None)
        entry["ram_peak_mib"] = max((s["ram_used_mib"] for s in samples), default=None)
        for key in ["util_pct", "vram_mib", "temp_c", "power_w"]:
            entry["gpu_peak_" + key] = max((g[key] for g in gpus if g.get(key) is not None), default=None)
        out.append(entry)
    return out


def columns(runs):
    sizes = sorted({s for run in runs for s in run["prompt_sizes"]})
    lengths = sorted({n for run in runs for n in run["gen_lengths"]})
    cols = [("machine", "컴퓨터"), ("model", "모델"), ("num_ctx", "num_ctx"),
            ("load_s", "로딩 s"), ("gpu_pct", "GPU 적재 %"), ("size_mib", "모델 메모리 MiB")]
    for size in sizes:
        cols += [(f"prefill_{size}_tok_s", f"입력 {size} prefill tok/s"), (f"prefill_{size}_ttft_s", f"입력 {size} 첫 응답 s")]
    for length in lengths:
        cols += [(f"decode_{length}_tok_s", f"생성 {length} tok/s"), (f"decode_{length}_tokens", f"생성 {length} 실제 토큰")]
    cols += [("gpu_peak_vram_mib", "GPU별 VRAM 최대 MiB"), ("gpu_peak_util_pct", "GPU 최대 %"),
             ("gpu_peak_temp_c", "GPU 최대 °C"), ("gpu_peak_power_w", "GPU 최대 W"),
             ("cpu_peak_pct", "CPU 최대 %"), ("ram_peak_mib", "시스템 RAM 최대 MiB"), ("errors", "오류")]
    return cols


def render(runs, target, announce=True):
    rows = [row for run in runs for row in summary(run)]
    cols = columns(runs)
    show = bench.display
    for row in rows if announce else []:
        decode = " ".join(f"생성{n} {show(row.get(f'decode_{n}_tok_s'))}" for run in runs[:1] for n in run["gen_lengths"])
        prefill = " ".join(f"입력{s} {show(row.get(f'prefill_{s}_tok_s'))}" for run in runs[:1] for s in run["prompt_sizes"])
        print(f"{row['machine']} | {row['model']} | ctx {row['num_ctx']} | 로딩 {show(row['load_s'])}s | "
              f"GPU {show(row['gpu_pct'])}% | prefill tok/s: {prefill} | decode tok/s: {decode} | 오류 {row['errors']}")
    headers = "".join(f"<th>{html.escape(label)}</th>" for _, label in cols)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(show(row.get(key)))}</td>" for key, _ in cols) + "</tr>" for row in rows)
    excluded = [(run["machine"], item) for run in runs for item in run.get("skipped_models", [])]
    skipped = [(run["machine"], item) for run in runs for item in run.get("skipped_cases", [])]
    details = "<h2>제외 모델</h2><ul>" + "".join(
        "<li>" + html.escape(f"{m} / {i['model']}: {i['reason']}") + "</li>" for m, i in excluded) + "</ul>"
    details += "<h2>건너뛴 조건</h2><ul>" + "".join(
        "<li>" + html.escape(f"{m} / {i['model']} ctx {i['num_ctx']} 입력 {i['target']}: {i['reason']}") + "</li>"
        for m, i in skipped) + "</ul>"
    details += "".join("<details><summary>" + html.escape(run["machine"]) + " 원본 결과·설정</summary><pre>" +
                       html.escape(json.dumps(run, ensure_ascii=False, indent=2)) + "</pre></details>" for run in runs)
    page = '''<!doctype html><html lang="ko"><meta charset="utf-8"><title>로컬 모델 성능</title>
<style>body{font:16px system-ui;margin:28px;background:#f5f7fa;color:#172338}table{border-collapse:collapse;background:white}td,th{padding:10px;border:1px solid #ccd;white-space:nowrap}th{cursor:pointer;background:#dde8f4}pre{white-space:pre-wrap;overflow-wrap:anywhere}input{padding:10px;width:300px}.scroll{overflow:auto}details{margin-top:24px}</style>
<h1>Ollama 모델 순수 성능</h1>
<p>답의 내용은 채점하지 않습니다. 값은 반복 측정의 중앙값입니다. 열 제목을 눌러 정렬하세요.</p>
<p>로딩은 모델을 내린 뒤 첫 요청의 load_duration입니다. GPU 적재 %가 100보다 작으면 일부가 CPU에서 돌아 느려집니다.
prefill은 입력을 읽는 속도, decode는 글을 만드는 속도입니다. 자원은 실행 컴퓨터 전체 사용량이며 원격 실행에서는 측정 불가입니다.</p>
<input id="filter" placeholder="컴퓨터 또는 모델 필터"><div class="scroll"><table id="results"><thead><tr>HEADERS</tr></thead><tbody>BODY</tbody></table></div>DETAILS
<script>const table=document.getElementById('results');document.getElementById('filter').oninput=e=>{for(const r of table.tBodies[0].rows)r.hidden=!r.textContent.toLowerCase().includes(e.target.value.toLowerCase())};let descending=false;table.querySelectorAll('th').forEach((h,i)=>h.onclick=()=>{descending=!descending;const rows=[...table.tBodies[0].rows];rows.sort((a,b)=>{const x=a.cells[i].textContent,y=b.cells[i].textContent;return (descending?-1:1)*(/^[-\\d.]+$/.test(x)&&/^[-\\d.]+$/.test(y)?Number(x)-Number(y):x.localeCompare(y))});rows.forEach(r=>table.tBodies[0].append(r))});</script></html>'''
    page = page.replace("HEADERS", headers).replace("BODY", body).replace("DETAILS", details)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")


def save(run, directory):
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / "result.json.tmp"
    temp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(directory / "result.json")
    render([run], directory / "report.html", announce=False)


def measure(session, base, payload, prompt, timeout, remote):
    """요청 하나를 보내고 (지표, 자원 표본). 답의 내용은 저장하지 않고 길이만 남긴다."""
    with bench.Monitor(not remote) as monitor:
        _, metrics = bench.chat(session, base, dict(payload, messages=[dict(role="user", content=prompt)]), timeout)
    metrics["prefill_tok_s"] = rate(metrics.get("prompt_eval_count"), metrics.get("prompt_eval_duration"))
    metrics["load_s"] = metrics["load_duration"] / 1e9 if metrics.get("load_duration") else None
    return metrics, monitor.samples


def run_perf(args):
    session, base, remote = connect(args)

    def get(path):
        r = session.get(base + path, timeout=15)
        r.raise_for_status()
        return r.json()

    installed = get("/api/tags")["models"]
    if not installed:
        raise ValueError("설치된 모델 없음")
    models, skipped, metadata = bench.select_models(session, base, installed, args.models, args.exclude)
    if args.list:
        for item in metadata:
            print("시험 대상:", item["name"], json.dumps(item.get("details", {}), ensure_ascii=False))
        for item in skipped:
            print("제외:", item["model"], item["reason"])
        return 0
    for item in skipped:
        print("제외:", item["model"], item["reason"], flush=True)
    resident = [m.get("name") for m in get("/api/ps").get("models", [])]
    if resident:
        print("주의: 이미 메모리에 올라간 모델이 있어 VRAM을 나눠 쓸 수 있습니다:", ", ".join(map(str, resident)), flush=True)
    run = dict(schema=1, suite=SUITE, machine=args.machine, started=datetime.now(timezone.utc).isoformat(),
               hardware=dict(os=platform.system(), arch=platform.machine(), cpu=platform.processor(),
                             logical_cpus=psutil.cpu_count(), ram_mib=psutil.virtual_memory().total / 2**20),
               resource_scope="unavailable_remote" if remote else "local_whole_system",
               ollama_version=get("/api/version"), models=models, model_metadata=metadata, skipped_models=skipped,
               resident_at_start=resident, contexts=args.contexts, prompt_sizes=args.prompt_sizes,
               gen_lengths=args.gen_lengths, repeats=args.repeats, think=args.think, unload=not args.no_unload,
               client_versions=dict(python=platform.python_version(), requests=requests.__version__, psutil=psutil.__version__),
               loads=[], results=[], skipped_cases=[], status="running")
    directory = Path(args.output) if args.output else Path(__file__).resolve().parents[1] / "assistant/data/benchmarks" / (
        "perf-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    save(run, directory)
    if not models:
        run["status"] = "no_eligible_models"
        save(run, directory)
        session.close()
        print("시험할 대화 모델이 없습니다. 제외 목록을 확인하세요. 결과:", directory)
        return 1
    try:
        for ctx in args.contexts:
            for model in models:
                payload = dict(model=model, stream=True, keep_alive="5m",
                               options=dict(num_ctx=ctx, temperature=0, seed=42))
                if args.think != "omit":
                    payload["think"] = args.think == "on"
                if not args.no_unload:
                    unload(session, base, model)
                # 로딩: 내린 뒤 첫 요청. 출력 1토큰
                metrics, samples = measure(session, base, dict(payload, options=dict(payload["options"], num_predict=1)),
                                           "Reply with OK.", args.timeout, remote)
                run["loads"].append(dict(model=model, num_ctx=ctx, metrics=metrics, resources=samples,
                                         placement=placement(get("/api/ps"), model)))
                save(run, directory)
                for repeat in range(args.repeats):
                    for size in args.prompt_sizes:
                        if size > ctx * CTX_MARGIN:
                            if repeat == 0:
                                run["skipped_cases"].append(dict(model=model, num_ctx=ctx, target=size,
                                                                 reason="입력이 num_ctx의 80%를 넘음"))
                            continue
                        row = dict(model=model, num_ctx=ctx, kind="prefill", target=size, repeat=repeat + 1)
                        try:
                            metrics, samples = measure(session, base, dict(payload, options=dict(payload["options"], num_predict=16)),
                                                       prefill_prompt(size, uuid.uuid4().hex), args.timeout, remote)
                        except (requests.RequestException, ValueError, TimeoutError) as exc:
                            run["results"].append(dict(row, error=type(exc).__name__))
                            raise
                        run["results"].append(dict(row, metrics=metrics, resources=samples))
                        save(run, directory)
                        print(f"{model} ctx{ctx}: 입력 {size} {repeat+1}/{args.repeats} "
                              f"prefill {bench.display(metrics['prefill_tok_s'])} tok/s", flush=True)
                    for length in args.gen_lengths:
                        row = dict(model=model, num_ctx=ctx, kind="decode", target=length, repeat=repeat + 1)
                        try:
                            metrics, samples = measure(session, base, dict(payload, options=dict(payload["options"], num_predict=length)),
                                                       f"[run {uuid.uuid4().hex}] {GEN_PROMPT}", args.timeout, remote)
                        except (requests.RequestException, ValueError, TimeoutError) as exc:
                            run["results"].append(dict(row, error=type(exc).__name__))
                            raise
                        run["results"].append(dict(row, metrics=metrics, resources=samples))
                        save(run, directory)
                        print(f"{model} ctx{ctx}: 생성 {length} {repeat+1}/{args.repeats} "
                              f"decode {bench.display(metrics['tokens_s'])} tok/s ({metrics.get('eval_count')}토큰)", flush=True)
                if not args.no_unload:
                    unload(session, base, model)  # 다음 모델이 VRAM을 혼자 쓰게
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
    p = argparse.ArgumentParser(description="Ollama 모델 순수 성능 (로딩·prefill·decode·자원)")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--machine", required=True, help="주소 대신 home-server, laptop 등 별칭")
    r.add_argument("--models", nargs="+")
    r.add_argument("--exclude", nargs="+", help="제외할 정확한 모델 태그 (공백 구분)")
    r.add_argument("--list", action="store_true")
    r.add_argument("--remote", action="store_true")
    r.add_argument("--repeats", type=int, default=3)
    r.add_argument("--contexts", type=int, nargs="+", default=[4096], help="num_ctx 값들 (예: 2048 4096 8192)")
    r.add_argument("--prompt-sizes", type=int, nargs="+", default=[128, 1000, 3000], help="입력 길이(토큰, 대략)")
    r.add_argument("--gen-lengths", type=int, nargs="+", default=[256, 512], help="생성 길이(토큰)")
    r.add_argument("--timeout", type=float, default=300)
    r.add_argument("--think", choices=["off", "on", "omit"], default="off")
    r.add_argument("--no-unload", action="store_true", help="모델을 내리지 않는다 (로딩 시간은 정확하지 않음)")
    r.add_argument("--output")
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    c.add_argument("--html", default="perf-comparison.html")
    args = p.parse_args(argv)
    try:
        if args.command == "compare":
            runs = [json.loads(Path(f).read_text(encoding="utf-8")) for f in args.files]
            if any(r.get("schema") != 1 or r.get("suite") != SUITE for r in runs):
                raise ValueError("model_perf 결과 파일이 아닙니다")
            signatures = {json.dumps([r["contexts"], r["prompt_sizes"], r["gen_lengths"], r["think"], r["repeats"]]) for r in runs}
            if len(signatures) > 1 or any(r["status"] != "completed" for r in runs):
                print("주의: 설정이 다르거나 미완료 결과가 포함되어 직접 비교할 수 없습니다.", file=sys.stderr)
            render(runs, Path(args.html))
            return 0
        if (args.repeats < 1 or args.timeout <= 0 or min(args.contexts) < 512
                or min(args.prompt_sizes) < 16 or min(args.gen_lengths) < 16):
            p.error("repeats>=1, timeout>0, contexts>=512, prompt-sizes>=16, gen-lengths>=16 필요")
        return run_perf(args)
    except (requests.RequestException, ValueError, OSError, KeyError) as exc:
        print(f"실행 실패: {type(exc).__name__}. 모델 목록·접속 설정·결과 파일을 확인하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
