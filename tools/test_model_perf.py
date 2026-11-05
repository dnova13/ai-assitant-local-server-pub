"""실제 모델 없이 순수 성능 측정의 요청 순서·지표 계산·건너뛰기·부분 저장·HTML을 검증한다."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from tools import model_perf as perf


class FakeOllama(BaseHTTPRequestHandler):
    fail = False
    requests = []
    capabilities = {'fake:3b': ['completion'], 'embed:latest': ['embedding']}

    def log_message(self, *args):
        pass

    def reply(self, value):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(value).encode() + b'\n')

    def do_GET(self):
        self.reply({'/api/tags': {'models': [{'name': n, 'digest': 'd'} for n in self.capabilities]},
                    '/api/version': {'version': 'test'},
                    '/api/ps': {'models': [{'name': 'fake:3b', 'size': 4 * 2**30, 'size_vram': 3 * 2**30}]}}[self.path])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/api/show':
            self.reply({'capabilities': self.capabilities[body['model']]})
            return
        self.requests.append((self.path, body))
        if self.path == '/api/generate':  # 내리기
            self.reply({'done': True})
            return
        if self.fail and 'Count upward' in body['messages'][-1]['content']:
            self.reply({'error': 'test failure'})
            return
        n = body['options']['num_predict']
        self.reply({'message': {'content': 'x' * n}, 'done': True, 'done_reason': 'length',
                    'load_duration': 2 * 10**9, 'prompt_eval_count': 500, 'prompt_eval_duration': 10**9,
                    'eval_count': n, 'eval_duration': n * 10**7})


class PerfTests(unittest.TestCase):
    def test_prompt_avoids_cache_and_scales(self):
        a, b = perf.prefill_prompt(1000, 'a'), perf.prefill_prompt(1000, 'b')
        self.assertNotEqual(a, b)
        self.assertGreater(len(perf.prefill_prompt(3000, 'a')), 2 * len(a))

    def test_rate_and_placement(self):
        self.assertEqual(perf.rate(100, 2 * 10**9), 50)
        self.assertIsNone(perf.rate(0, 10**9))
        p = perf.placement({'models': [{'name': 'm', 'size': 200, 'size_vram': 150}]}, 'm')
        self.assertEqual(p['gpu_pct'], 75)
        self.assertIsNone(perf.placement({'models': []}, 'm')['gpu_pct'])

    def exercise(self, fail=False, extra=()):
        FakeOllama.fail = fail
        FakeOllama.requests = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {
                'BENCH_OLLAMA_URL': f'http://127.0.0.1:{server.server_port}'
            }), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(perf.bench, 'gpu_sample', return_value=None):
                code = perf.main(['run', '--machine', '<b>x</b>', '--repeats', '2', '--output', folder,
                                  '--prompt-sizes', '128', '3000', '--gen-lengths', '64', *extra])
                run = json.loads((Path(folder) / 'result.json').read_text())
                report = (Path(folder) / 'report.html').read_text()
                self.assertNotIn('<b>x</b>', report)
                self.assertNotIn('xxxx', json.dumps(run))  # 답 내용은 저장하지 않는다
                compare = None
                if not fail:
                    compare = perf.main(['compare', str(Path(folder) / 'result.json'),
                                         '--html', str(Path(folder) / 'c.html')])
                return code, run, list(FakeOllama.requests), compare
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_full_run(self):
        code, run, reqs, compare = self.exercise()
        self.assertEqual((code, run['status'], compare), (0, 'completed', 0))
        self.assertEqual(run['models'], ['fake:3b'])
        self.assertEqual(run['skipped_models'][0]['model'], 'embed:latest')
        # 첫 요청과 마지막 요청은 내리기, 그 사이에 로딩 1 + (입력 2 + 생성 1) x 2회
        self.assertEqual(reqs[0][0], '/api/generate')
        self.assertEqual(reqs[0][1]['keep_alive'], 0)
        self.assertEqual(reqs[-1][0], '/api/generate')
        chats = [b for p, b in reqs if p == '/api/chat']
        self.assertEqual(len(chats), 1 + 3 * 2)
        self.assertFalse(chats[0]['think'])
        self.assertEqual(chats[0]['options']['num_predict'], 1)
        row = perf.summary(run)[0]
        self.assertEqual(row['load_s'], 2)
        self.assertEqual(row['gpu_pct'], 75)
        self.assertEqual(row['prefill_128_tok_s'], 500)
        self.assertEqual(row['decode_64_tok_s'], 100)
        self.assertEqual(row['decode_64_tokens'], 64)
        self.assertEqual(row['errors'], 0)

    def test_skips_prompt_larger_than_context(self):
        code, run, reqs, _ = self.exercise(extra=['--contexts', '2048'])
        self.assertEqual(code, 0)
        self.assertEqual([(s['num_ctx'], s['target']) for s in run['skipped_cases']], [(2048, 3000)])
        self.assertIsNone(perf.summary(run)[0]['prefill_3000_tok_s'])

    def test_no_unload(self):
        _, _, reqs, _ = self.exercise(extra=['--no-unload'])
        self.assertFalse(any(p == '/api/generate' for p, _ in reqs))

    def test_error_stops_and_saves(self):
        code, run, reqs, _ = self.exercise(fail=True)
        self.assertEqual((code, run['status']), (1, 'failed'))
        self.assertEqual(run['results'][-1]['error'], 'ValueError')
        self.assertEqual(perf.summary(run)[0]['errors'], 1)

    def test_remote_requires_opt_in(self):
        with mock.patch.dict(os.environ, {'BENCH_OLLAMA_URL': 'https://example.invalid'}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(perf.main(['run', '--machine', 'remote']), 1)


if __name__ == '__main__':
    unittest.main()
