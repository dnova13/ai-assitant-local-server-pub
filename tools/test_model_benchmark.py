"""실제 모델 없이 측정 프로토콜·채점·부분 저장·HTML을 검증한다."""
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

from tools import model_benchmark as bench


class FakeOllama(BaseHTTPRequestHandler):
    fail = False
    payloads = []
    capabilities = {'fake:3b': ['completion'], 'renamed:latest': ['embedding']}

    def log_message(self, *args):
        pass

    def reply(self, value):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(value).encode() + b'\n')

    def do_GET(self):
        self.reply({'/api/tags': {'models': [{'name': name, 'digest': 'test-digest'} for name in self.capabilities]},
                    '/api/version': {'version': 'test'}, '/api/ps': {'models': []}}[self.path])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/api/show':
            self.reply({'capabilities': self.capabilities[body['model']]})
            return
        self.payloads.append(body)
        prompt = body['messages'][-1]['content']
        if self.fail and prompt != 'Return {"ready":true}':
            self.reply({'error': 'test failure'})
            return
        expected = next((c['expected'] for c in bench.cases() if c['prompt'] == prompt), {'ready': True})
        self.reply({'message': {'content': json.dumps(expected, ensure_ascii=False)}, 'done': True,
                    'eval_count': 10, 'eval_duration': 1000000000, 'prompt_eval_count': 100,
                    'load_duration': 123, 'done_reason': 'stop'})


class BenchmarkTests(unittest.TestCase):
    def test_grading(self):
        self.assertEqual(bench.grade('{"x": 1}', {'x': 1}), (True, True))
        self.assertEqual(bench.grade('{"x": true}', {'x': 1}), (True, False))
        self.assertEqual(bench.grade('```json\n{}\n```', {}), (False, False))
        self.assertEqual(bench.grade('{"x":1,"extra":2}', {'x': 1}), (True, False))

    def test_suite_balance_and_percentile(self):
        self.assertEqual(len(bench.cases()), 20)
        self.assertEqual(sum(c['language'] == 'ko' for c in bench.cases()), 10)
        self.assertEqual(bench.percentile([1, 2, 3], .5), 2)
        self.assertIsNone(bench.percentile([], .95))

    def test_missing_gpu(self):
        with mock.patch.object(bench.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertIsNone(bench.gpu_sample())

    def test_stream_and_missing_done(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.iter_lines.return_value = [
            json.dumps({'message': {'thinking': 'internal'}}).encode(),
            json.dumps({'message': {'content': '{"x":'}}).encode(),
            json.dumps({'message': {'content': '1}'}, 'done': True,
                        'eval_count': 4, 'eval_duration': 2000000000}).encode()]
        session = mock.Mock()
        session.post.return_value = response
        content, metrics = bench.chat(session, 'http://unused', {}, 5)
        self.assertEqual(content, '{"x":1}')
        self.assertEqual(metrics['tokens_s'], 2)
        self.assertIsNotNone(metrics['ttft_s'])
        response.iter_lines.return_value = [b'{"message":{"content":"partial"}}']
        with self.assertRaisesRegex(ValueError, 'missing_done'):
            bench.chat(session, 'http://unused', {}, 5)

    def test_nvidia_optional_fields(self):
        completed = mock.Mock(returncode=0, stdout='0, GPU Test, 50, 2000, 6000, 60, [N/A]\n')
        with mock.patch.object(bench.subprocess, 'run', return_value=completed):
            gpu = bench.gpu_sample()[0]
            self.assertEqual(gpu['vram_mib'], 2000)
            self.assertIsNone(gpu['power_w'])

    def exercise(self, fail=False, all_excluded=False):
        FakeOllama.fail = fail
        FakeOllama.payloads = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {
                'BENCH_OLLAMA_URL': f'http://127.0.0.1:{server.server_port}'
            }), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(bench, 'gpu_sample', return_value=None):
                args = ['run', '--machine', '<script>alert(1)</script>',
                        '--repeats', '1', '--output', folder]
                if all_excluded:
                    args.extend(['--exclude', 'fake:3b'])
                code = bench.main(args)
                run = json.loads((Path(folder) / 'result.json').read_text())
                report = (Path(folder) / 'report.html').read_text()
                self.assertNotIn('<script>alert(1)</script>', report)
                self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', report)
                self.assertNotIn('BENCH_OLLAMA_URL', json.dumps(run))
                if all_excluded:
                    self.assertEqual(code, 1)
                    self.assertEqual(run['status'], 'no_eligible_models')
                    self.assertEqual(run['models'], [])
                    self.assertEqual(len(run['skipped_models']), 2)
                    self.assertEqual(FakeOllama.payloads, [])
                    return
                self.assertEqual(code, 1 if fail else 0)
                self.assertEqual(run['status'], 'failed' if fail else 'completed')
                self.assertEqual(len(run['results']), 1 if fail else 20)
                self.assertEqual(run['models'], ['fake:3b'])
                self.assertEqual(run['skipped_models'][0]['model'], 'renamed:latest')
                self.assertIn('completion', report)
                self.assertTrue(all(p['model'] == 'fake:3b' for p in FakeOllama.payloads))
                result = bench.summary(run)[0]
                self.assertIsNone(result['gpu_peak_vram_mib'])
                if fail:
                    self.assertEqual(result['errors'], 1)
                    self.assertEqual(len(FakeOllama.payloads), 2)
                else:
                    self.assertEqual(result['ko_pct'], 100)
                    self.assertEqual(result['en_pct'], 100)
                    self.assertEqual(result['tokens_s_median'], 10)
                    self.assertEqual(len(FakeOllama.payloads), 21)
                    self.assertEqual(bench.main(['compare', str(Path(folder)/'result.json'),
                                                 '--html', str(Path(folder)/'compare.html')]), 0)
                self.assertNotIn('expected', FakeOllama.payloads[-1])
                self.assertFalse(FakeOllama.payloads[-1]['think'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_full_run_and_report(self):
        self.exercise()

    def test_error_stops_and_saves(self):
        self.exercise(fail=True)

    def test_all_excluded_is_not_a_pass(self):
        self.exercise(all_excluded=True)

    def test_capabilities_and_manual_exclusions(self):
        installed = [{'name': n} for n in ['custom-embed', 'both', 'manual', 'embedding-in-name']]
        session = mock.Mock()
        caps = {'custom-embed': ['embedding'], 'both': ['embedding', 'completion'],
                'embedding-in-name': ['completion']}
        session.post.side_effect = lambda *a, **kw: mock.Mock(
            json=lambda: {'capabilities': caps[kw['json']['model']]})
        selected, skipped, metadata = bench.select_models(session, 'http://unused', installed,
                                                         excluded=['manual'])
        self.assertEqual(selected, ['both', 'embedding-in-name'])
        self.assertEqual([s['model'] for s in skipped], ['custom-embed', 'manual'])
        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(metadata[0]['capabilities'], ['embedding', 'completion'])

    def test_missing_capabilities_fails_closed(self):
        session = mock.Mock()
        session.post.return_value.json.return_value = {}
        with self.assertRaisesRegex(ValueError, 'capability'):
            bench.select_models(session, 'http://unused', [{'name': 'unknown'}])

    def test_unknown_manual_exclusion(self):
        with self.assertRaises(ValueError):
            bench.select_models(mock.Mock(), 'http://unused', [{'name': 'valid'}], excluded=['typo'])

    def test_remote_requires_opt_in(self):
        with mock.patch.dict(os.environ, {'BENCH_OLLAMA_URL': 'https://example.invalid'}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bench.main(['run', '--machine', 'remote']), 1)


if __name__ == '__main__':
    unittest.main()
