import base64
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import image_backend as b
import project as p


def png(color='red'):
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), color).save(stream, 'PNG')
    return stream.getvalue()


def config(protocol='openai', **extra):
    return {'PROTOCOL': protocol, 'ENDPOINT': 'https://api.example.test/v1', 'MODEL': 'test-image', 'API_KEY': 'test-secret', 'PARAMS': {}, **extra}


class ToolsTest(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[3] / 'tmp'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        self.project, self.skill, self.home = [self.root / name for name in ('project', 'skill', 'home')]
        for path in (self.project, self.skill, self.home / '.anime-pv'):
            path.mkdir(parents=True)
        (self.project / '图像.png').write_bytes(png())

    def tearDown(self):
        self.temp.cleanup()

    def env(self, root, name='.env', **values):
        values = config(**values)
        (root / name).write_text('\n'.join('ANIME_PV_' + k + '=' + (json.dumps(v) if isinstance(v, dict) else v) for k, v in values.items()), encoding='utf-8')

    def test_config_priority_unrelated_and_incomplete(self):
        self.env(self.skill)
        self.env(self.home / '.anime-pv', protocol='seedream')
        (self.project / '.env').write_text('UNRELATED=yes')
        self.assertEqual(b.load_config(self.project, self.skill, self.home)['PROTOCOL'], 'openai')
        (self.project / '.env').write_text('ANIME_PV_ENDPOINT=https://other.test')
        with self.assertRaises(b.BackendError):
            b.load_config(self.project, self.skill, self.home)
        self.env(self.project, protocol='gemini')
        self.assertEqual(b.load_config(self.project, self.skill, self.home)['PROTOCOL'], 'gemini')

    def test_config_never_interpolates_or_merges_process(self):
        self.env(self.project, API_KEY='${SECRET}')
        with patch.dict('os.environ', {'SECRET': 'real-key'}):
            self.assertEqual(b.load_config(self.project, self.skill, self.home)['API_KEY'], '${SECRET}')

    def test_endpoint_does_not_accept_secret_query(self):
        self.env(self.project, ENDPOINT='https://api.test/v1?key=bad')
        with self.assertRaises(b.BackendError):
            b.load_config(self.project, self.skill, self.home)

    def test_all_generation_protocols(self):
        for protocol in b.PROTOCOLS:
            with self.subTest(protocol=protocol):
                url, headers, body, files = b.build_request(config(protocol, DASHSCOPE_API='multimodal'), {'prompt': 'PV'}, self.project)
                self.assertTrue(url.startswith('https://api.example.test/v1/'))
                self.assertIsNone(files)
                self.assertNotIn('test-secret', json.dumps(body))
                self.assertTrue(headers)

    def test_openai_multipart_mask(self):
        spec = {'operation': 'edit', 'prompt': 'pose', 'images': ['图像.png'], 'mask': '图像.png'}
        url, _, _, files = b.build_request(config(), spec, self.project)
        self.assertTrue(url.endswith('/images/edits'))
        self.assertEqual([x[0] for x in files], ['image', 'mask'])
        self.assertEqual(files[0][1][1], png())

    def test_reference_protocols(self):
        spec = {'operation': 'reference', 'prompt': 'pose', 'images': ['图像.png']}
        for protocol in ('gemini', 'seedream'):
            _, _, body, _ = b.build_request(config(protocol), spec, self.project)
            self.assertIn(base64.b64encode(png()).decode(), json.dumps(body))
        with self.assertRaises(b.BackendError):
            b.build_request(config('minimax'), {**spec, 'operation': 'edit'}, self.project)
        with self.assertRaises(b.BackendError):
            b.build_request(config('gemini'), {**spec, 'mask': '图像.png'}, self.project)

    def test_dashscope_profiles(self):
        spec = {'operation': 'edit', 'prompt': 'pose', 'images': ['图像.png']}
        for profile in ('multimodal', 'multimodal-async'):
            _, headers, body, _ = b.build_request(config('dashscope', DASHSCOPE_API=profile), spec, self.project)
            self.assertIn('input', body)
            self.assertEqual('X-DashScope-Async' in headers, profile != 'multimodal')
        with self.assertRaises(b.BackendError):
            b.build_request(config('dashscope'), spec, self.project)

    def test_native_params_preserved_but_inputs_protected(self):
        spec = {'prompt': 'PV', 'params': {'generationConfig': {'imageConfig': {'aspectRatio': '16:9'}}}}
        body = b.build_request(config('gemini'), spec, self.project)[2]
        self.assertEqual(body['generationConfig']['responseModalities'], ['TEXT', 'IMAGE'])
        with self.assertRaises(b.BackendError):
            b.build_request(config(), {'prompt': 'PV', 'params': {'model': 'other'}}, self.project)
        with self.assertRaises(b.BackendError):
            b.build_request(config('dashscope'), {'prompt': 'PV', 'params': {'input': {'prompt': 'other'}}}, self.project)

    def test_response_parsing_all_protocols(self):
        fixtures = {
            'openai': {'data': [{'b64_json': 'YWJj'}]},
            'seedream': {'data': [{'url': 'https://cdn.test/x'}]},
            'dashscope': {'output': {'results': [{'url': 'https://cdn.test/x'}]}},
            'gemini': {'candidates': [{'content': {'parts': [{'thought': True, 'inlineData': {'data': 'bad'}}, {'inlineData': {'data': 'YWJj'}}]}}]},
        }
        for protocol, response in fixtures.items():
            self.assertEqual(len(b.extract_images(protocol, response)), 1)
        self.assertEqual(b.extract_images('gemini', fixtures['gemini']), [{'base64': 'YWJj'}])
        self.assertEqual(len(b.extract_images('dashscope', {'output': {'choices': [{'message': {'content': [{'image': 'https://cdn.test/x'}]}}]}})), 1)

    def test_post_never_retries_and_hides_server_secrets(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503, text='echo test-secret')
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(b.BackendError) as caught:
                b.request_json(client, 'POST', 'https://api.test', {}, {})
        self.assertEqual(len(calls), 1)
        self.assertNotIn('test-secret', str(caught.exception))

    def test_uncertain_submission_is_not_resubmitted(self):
        folder = self.project / '.image-jobs/uncertain'
        with patch.object(b, 'request_json', side_effect=b.BackendError('timeout')) as send:
            with self.assertRaises(b.BackendError):
                b.execute(config(), {'prompt': 'PV'}, self.project, folder)
            with self.assertRaises(b.BackendError):
                b.execute(config(), None, self.project, folder, True)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(b.read_json(folder / 'job.json')['status'], 'unknown')

    def test_download_does_not_send_platform_auth(self):
        real_client = httpx.Client
        def handler(request):
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('x-goog-api-key', request.headers)
            return httpx.Response(200, content=png())
        def download_client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(b.httpx, 'Client', side_effect=download_client):
            files = b.save_images([{'url': 'https://cdn.test/pic'}], self.project)
        self.assertEqual((self.project / files[0]['path']).read_bytes(), png())

    def test_sync_job_resume_no_resubmit(self):
        folder = self.project / '.image-jobs/one'
        response = {'data': [{'b64_json': base64.b64encode(png()).decode()}]}
        with patch.object(b, 'request_json', return_value=response) as send:
            state = b.execute(config(), {'prompt': 'PV'}, self.project, folder)
            self.assertEqual(state['status'], 'complete')
            self.assertEqual(b.execute(config(), None, self.project, folder, True)['status'], 'complete')
            self.assertEqual(send.call_count, 1)
        self.assertFalse((folder / 'results.private.json').exists())
        self.assertNotIn('test-secret', (folder / 'job.json').read_text())
        with self.assertRaises(b.BackendError):
            b.execute(config(MODEL='other'), None, self.project, folder, True)

    def test_async_resume_and_download_recovery(self):
        folder = self.project / '.image-jobs/async'
        responses = [{'output': {'task_id': 'id'}}, {'output': {'task_status': 'PENDING'}}]
        with patch.object(b, 'request_json', side_effect=responses):
            self.assertEqual(b.execute(config('dashscope', DASHSCOPE_API='multimodal-async'), {'prompt': 'PV'}, self.project, folder, wait_seconds=0)['status'], 'pending')
        finished = {'output': {'task_status': 'SUCCEEDED', 'results': [{'url': 'https://cdn.test/x'}]}}
        with patch.object(b, 'request_json', return_value=finished) as send, patch.object(b, 'save_images', side_effect=b.BackendError('download failure')):
            with self.assertRaises(b.BackendError):
                b.execute(config('dashscope', DASHSCOPE_API='multimodal-async'), None, self.project, folder, True)
            self.assertEqual(send.call_args.args[1], 'GET')
        with patch.object(b, 'request_json') as send, patch.object(b, 'save_images', return_value=[]):
            self.assertEqual(b.execute(config('dashscope', DASHSCOPE_API='multimodal-async'), None, self.project, folder, True)['status'], 'complete')
            send.assert_not_called()

    def test_duplicate_basenames_are_unique_multipart_files(self):
        other = self.project / 'other'
        other.mkdir()
        (other / '图像.png').write_bytes(png())
        _, _, _, files = b.build_request(config(), {
            'operation': 'reference', 'prompt': 'combine',
            'images': ['图像.png', 'other/图像.png']}, self.project)
        self.assertEqual([field for field, _ in files], ['image[]', 'image[]'])
        self.assertEqual([value[0] for _, value in files], ['input-001.png', 'input-002.png'])
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as client:
            request = client.build_request('POST', 'https://example.test', files=files)
            wire = request.read()
        self.assertIn(b'filename="input-001.png"', wire)
        self.assertIn(b'filename="input-002.png"', wire)

    def test_refresh_expired_async_results_via_cli(self):
        cfg = config('dashscope', DASHSCOPE_API='multimodal-async')
        self.env(self.project, protocol='dashscope', DASHSCOPE_API='multimodal-async')
        (self.project / 'state').mkdir()
        b.write_json(self.project / 'state/image-route.json', {
            'route': 'backend', 'protocol': 'dashscope', 'user_choice': 'Synthetic test choice'})
        folder = self.project / '.image-jobs/expired'
        folder.mkdir(parents=True)
        b.write_json(folder / 'job.json', {'binding': b.binding(cfg),
            'protocol': 'dashscope', 'status': 'download', 'task_id': 'existing'})
        b.write_json(folder / 'results.private.json', [{'url': 'https://cdn.test/expired'}])
        calls = []
        real_client = httpx.Client
        def handler(request):
            calls.append((request.method, request.url.host, request.url.path))
            if request.url.host == 'api.example.test':
                self.assertEqual(request.headers['authorization'], 'Bearer test-secret')
                return httpx.Response(200, json={'output': {'task_status': 'SUCCEEDED',
                    'results': [{'url': 'https://cdn.test/fresh'}]}})
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('x-goog-api-key', request.headers)
            return httpx.Response(403) if request.url.path == '/expired' else httpx.Response(200, content=png())
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(b.httpx, 'Client', side_effect=client):
            with self.assertRaises(b.BackendError):
                b.execute(cfg, None, self.project, folder, resume=True)
            self.assertEqual(b.read_json(folder / 'job.json')['status'], 'download')
            with patch.object(sys, 'argv', ['image_backend.py', '--project', str(self.project),
                    '--job', '.image-jobs/expired', '--resume', '--refresh-results']), \
                    redirect_stdout(io.StringIO()) as output:
                b.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['first_diagnostic']['category'], 'download-failure')
        self.assertNotIn('diagnostic', result)
        self.assertEqual(calls, [('GET', 'cdn.test', '/expired'),
            ('GET', 'api.example.test', '/v1/tasks/existing'), ('GET', 'cdn.test', '/fresh')])
        self.assertEqual((folder / 'image-001.png').read_bytes(), png())
        self.assertFalse((folder / 'results.private.json').exists())

    def test_refresh_rejects_ineligible_jobs_without_network_or_state_changes(self):
        cfg = config('dashscope', DASHSCOPE_API='multimodal-async')
        cases = [(config(), 'download', 'existing'),
                 (cfg, 'download', None),
                 *[(cfg, status, 'existing') for status in ('pending', 'complete', 'failed', 'unknown')]]
        for index, (current_config, status, task_id) in enumerate(cases):
            with self.subTest(index=index):
                folder = self.project / '.image-jobs' / str(index)
                folder.mkdir(parents=True)
                state = {'binding': b.binding(current_config), 'status': status,
                         'task_id': task_id, 'diagnostic': {'category': 'original-error'}}
                b.write_json(folder / 'job.json', state)
                with patch.object(b.httpx, 'Client') as network:
                    with self.assertRaises(b.BackendError):
                        b.execute(current_config, None, self.project, folder,
                                  resume=True, refresh_results=True)
                    network.assert_not_called()
                self.assertEqual(b.read_json(folder / 'job.json'), state)
        folder = self.project / '.image-jobs/new'
        with patch.object(b.httpx, 'Client') as network:
            with self.assertRaises(b.BackendError):
                b.execute(cfg, {'prompt': 'PV'}, self.project, folder, refresh_results=True)
            network.assert_not_called()
        self.assertFalse(folder.exists())
        with patch.object(sys, 'argv', ['image_backend.py', '--project', str(self.project),
                '--job', '.image-jobs/new', '--refresh-results']), \
                patch.object(b, 'load_config') as load, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                b.main()
            self.assertEqual(caught.exception.code, 2)
            load.assert_not_called()

    def test_refresh_query_errors_keep_cache_and_allow_plain_resume(self):
        cfg = config('dashscope', DASHSCOPE_API='multimodal-async')
        real_client = httpx.Client
        for failure, category in [('http', 'authentication'), ('empty', 'missing-images')]:
            with self.subTest(failure=failure):
                folder = self.project / '.image-jobs' / failure
                folder.mkdir(parents=True)
                first = {'category': 'download-failure', 'stage': 'download'}
                b.write_json(folder / 'job.json', {'binding': b.binding(cfg),
                    'status': 'download', 'task_id': 'existing', 'diagnostic': first})
                cache = [{'url': 'https://cdn.test/expired'}]
                b.write_json(folder / 'results.private.json', cache)
                calls = []
                def handler(request):
                    calls.append((request.method, request.url.host))
                    if len(calls) == 1:
                        return (httpx.Response(401) if failure == 'http' else
                                httpx.Response(200, json={'output': {'task_status': 'SUCCEEDED'}}))
                    if request.url.host == 'api.example.test':
                        return httpx.Response(200, json={'output': {'task_status': 'SUCCEEDED',
                            'results': [{'url': 'https://cdn.test/fresh'}]}})
                    self.assertNotIn('authorization', request.headers)
                    return httpx.Response(200, content=png())
                def client(**kwargs):
                    return real_client(transport=httpx.MockTransport(handler), **kwargs)
                with patch.object(b.httpx, 'Client', side_effect=client):
                    with self.assertRaises(b.BackendError):
                        b.execute(cfg, None, self.project, folder, resume=True, refresh_results=True)
                    state = b.read_json(folder / 'job.json')
                    self.assertEqual(state['status'], 'pending')
                    self.assertEqual(state['first_diagnostic'], first)
                    self.assertEqual(state['diagnostic']['category'], category)
                    self.assertEqual(b.read_json(folder / 'results.private.json'), cache)
                    state = b.execute(cfg, None, self.project, folder, resume=True)
                self.assertEqual(state['status'], 'complete')
                self.assertEqual(state['first_diagnostic'], first)
                self.assertEqual(calls, [('GET', 'api.example.test'),
                    ('GET', 'api.example.test'), ('GET', 'cdn.test')])

    def test_refresh_does_not_overwrite_existing_image_with_different_result(self):
        cfg = config('dashscope', DASHSCOPE_API='multimodal-async')
        folder = self.project / '.image-jobs/conflict'
        folder.mkdir(parents=True)
        b.write_json(folder / 'job.json', {'binding': b.binding(cfg),
            'status': 'download', 'task_id': 'existing'})
        b.write_json(folder / 'results.private.json', [{'url': 'https://cdn.test/expired'}])
        existing = folder / 'image-001.png'
        existing.write_bytes(png())
        calls = []
        real_client = httpx.Client
        def handler(request):
            calls.append(request.method)
            if request.url.host == 'api.example.test':
                return httpx.Response(200, json={'output': {'task_status': 'SUCCEEDED',
                    'results': [{'url': 'https://cdn.test/fresh'}]}})
            return httpx.Response(200, content=png('blue'))
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(b.httpx, 'Client', side_effect=client):
            with self.assertRaises(b.BackendError) as caught:
                b.execute(cfg, None, self.project, folder, resume=True, refresh_results=True)
        self.assertEqual(caught.exception.diagnostic['category'], 'output-conflict')
        self.assertEqual(existing.read_bytes(), png())
        self.assertEqual(b.read_json(folder / 'job.json')['status'], 'download')
        self.assertTrue((folder / 'results.private.json').exists())
        self.assertEqual(calls, ['GET', 'GET'])

    def test_interrupted_image_writes_resume_without_resubmitting(self):
        real_client, real_write, real_replace = httpx.Client, Path.write_bytes, Path.replace
        images = [png(), png('blue')]
        response = {'data': [{'b64_json': base64.b64encode(data).decode()} for data in images]}
        for failure, index in [('write', 1), ('write', 2), ('replace', 1)]:
            with self.subTest(failure=failure, index=index):
                folder = self.project / '.image-jobs' / f'{failure}-{index}'
                temporary_name = f'image-{index:03d}.png.tmp'
                calls = []
                def handler(request):
                    calls.append(request.method)
                    return httpx.Response(200, json=response)
                def client(**kwargs):
                    return real_client(transport=httpx.MockTransport(handler), **kwargs)
                def interrupted_write(path, data):
                    if failure == 'write' and path.name == temporary_name:
                        real_write(path, data[:12])
                        raise OSError('synthetic private write details')
                    return real_write(path, data)
                def interrupted_replace(path, target):
                    if failure == 'replace' and path.name == temporary_name:
                        raise OSError('synthetic private replace details')
                    return real_replace(path, target)
                with patch.object(b.httpx, 'Client', side_effect=client):
                    with patch.object(Path, 'write_bytes', interrupted_write), \
                            patch.object(Path, 'replace', interrupted_replace):
                        with self.assertRaises(b.BackendError) as caught:
                            b.execute(config(), {'prompt': 'PV', 'params': {'n': 2}}, self.project, folder)
                    self.assertEqual(caught.exception.diagnostic['category'], 'image-write-failure')
                    self.assertNotIn('private', str(caught.exception))
                    self.assertFalse((folder / f'image-{index:03d}.png').exists())
                    self.assertTrue((folder / temporary_name).exists())
                    self.assertEqual(b.read_json(folder / 'job.json')['status'], 'download')
                    state = b.execute(config(), None, self.project, folder, resume=True)
                self.assertEqual(state['status'], 'complete')
                self.assertEqual(state['validation']['status'], 'checks_passed')
                self.assertEqual(state['first_diagnostic']['category'], 'image-write-failure')
                self.assertEqual(calls, ['POST'])
                self.assertFalse(list(folder.glob('*.tmp')))
                self.assertFalse((folder / 'results.private.json').exists())
                for image_index, data in enumerate(images, 1):
                    self.assertEqual((folder / f'image-{image_index:03d}.png').read_bytes(), data)

    def test_safe_diagnostics_distinguish_network_and_json(self):
        for expected, handler in [
            ('ReadTimeout', lambda request: (_ for _ in ()).throw(httpx.ReadTimeout('test-secret'))),
            ('invalid-json', lambda request: httpx.Response(200, text='test-secret')),
        ]:
            calls = []
            def transport(request):
                calls.append(request)
                return handler(request)
            with httpx.Client(transport=httpx.MockTransport(transport)) as client:
                with self.assertRaises(b.BackendError) as caught:
                    b.request_json(client, 'POST', 'https://example.test', {}, {})
            self.assertEqual(caught.exception.diagnostic['category'], expected)
            self.assertNotIn('test-secret', str(caught.exception))
            self.assertEqual(len(calls), 1)

    def test_output_acceptance_is_separate_from_download(self):
        folder = self.project / '.image-jobs/spec'
        response = {'data': [{'b64_json': base64.b64encode(png()).decode()}]}
        with patch.object(b, 'request_json', return_value=response):
            state = b.execute(config(), {'prompt': 'PV', 'params': {
                'size': '1024x1024', 'n': 2, 'background': 'transparent'}}, self.project, folder)
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(state['files'][0]['width'], 2)
        self.assertEqual(state['validation']['status'], 'needs_review')
        self.assertEqual(len(state['validation']['warnings']), 3)
        self.assertEqual(b.execute(config(), None, self.project, folder, True)['validation'], state['validation'])
        self.assertEqual(b.validate_outputs({'transparent': True}, [
            {'path': 'asset.png', 'alpha_range': [0, 254]}])['warnings'], [])

    def test_diagnostic_persistence_and_retired_profiles(self):
        folder = self.project / '.image-jobs/diagnostic'
        with patch.object(b, 'request_json', side_effect=b.BackendError(
                'safe', category='invalid-json', stage='POST', http_status=200)):
            with self.assertRaises(b.BackendError):
                b.execute(config(), {'prompt': 'PV'}, self.project, folder)
        state = b.read_json(folder / 'job.json')
        self.assertEqual(state['diagnostic']['category'], 'invalid-json')
        with self.assertRaises(b.BackendError):
            b.execute(config(), None, self.project, folder, True)
        self.assertEqual(b.read_json(folder / 'job.json')['diagnostic'], state['diagnostic'])
        for profile in ('synthesis', 'edit'):
            with self.assertRaises(b.BackendError):
                b.build_request(config('dashscope', DASHSCOPE_API=profile), {'prompt': 'PV'}, self.project)

    def test_retired_dashscope_job_can_resume_without_post(self):
        folder = self.project / '.image-jobs/legacy'
        folder.mkdir(parents=True)
        legacy = config('dashscope', DASHSCOPE_API='synthesis')
        b.write_json(folder / 'job.json', {'binding': b.binding(legacy),
            'status': 'pending', 'protocol': 'dashscope', 'task_id': 'old-id'})
        response = {'output': {'task_status': 'SUCCEEDED',
            'results': [{'url': 'https://cdn.test/result'}]}}
        with patch.object(b, 'request_json', return_value=response) as request, \
                patch.object(b, 'save_images', return_value=[]):
            state = b.execute(legacy, None, self.project, folder, True)
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], 'GET')
        self.assertEqual(state['validation']['status'], 'not_checked')

    def test_existing_job_is_not_overwritten_by_invalid_new_request(self):
        folder = self.project / '.image-jobs/existing'
        folder.mkdir(parents=True)
        state = {'binding': b.binding(config()), 'status': 'unknown'}
        b.write_json(folder / 'job.json', state)
        with self.assertRaises(b.BackendError):
            b.execute(config(), {'prompt': ''}, self.project, folder)
        self.assertEqual(b.read_json(folder / 'job.json'), state)

    def test_wan26_generation_and_edit_modes(self):
        sync = config('dashscope', MODEL='wan2.6-image', DASHSCOPE_API='multimodal')
        asynchronous = {**sync, 'DASHSCOPE_API': 'multimodal-async'}
        with self.assertRaises(b.BackendError):
            b.build_request(sync, {'prompt': 'PV'}, self.project)
        spec = {'prompt': 'PV'}
        url, headers, body, _ = b.build_request(asynchronous, spec, self.project)
        self.assertTrue(url.endswith('/image-generation/generation'))
        self.assertEqual(headers['X-DashScope-Async'], 'enable')
        self.assertEqual(body['parameters'], {'enable_interleave': True, 'n': 1, 'max_images': 1})
        self.assertEqual(b.output_expectations(asynchronous, spec), {'max_count': 1})
        for count in (1, 4):
            body = b.build_request(sync, {'operation': 'edit', 'prompt': 'PV',
                'images': ['图像.png'] * count}, self.project)[2]
            self.assertNotIn('enable_interleave', body['parameters'])
        for params in ({'enable_interleave': False}, {'n': 2}, {'max_images': 0}):
            with self.assertRaises(b.BackendError):
                b.build_request(asynchronous, {'prompt': 'PV', 'params': {'parameters': params}}, self.project)
        with self.assertRaises(b.BackendError):
            b.build_request(asynchronous, {'operation': 'reference', 'prompt': 'PV',
                'images': ['图像.png'] * 2, 'params': {'parameters': {'enable_interleave': True}}}, self.project)
        response = {'output': {'choices': [{'message': {'content': [
            {'text': 'caption'}, {'image': 'https://cdn.test/one'}, {'image': 'https://cdn.test/two'}]}}]}}
        self.assertEqual(len(b.extract_images('dashscope', response)), 2)
        self.assertEqual(b.validate_outputs({'max_count': 3}, [{'path': 'one'}])['status'], 'checks_passed')

    def test_resume_records_latest_request_error_and_keeps_first(self):
        folder = self.project / '.image-jobs/latest'
        folder.mkdir(parents=True)
        first = {'category': 'ReadTimeout', 'stage': 'GET'}
        b.write_json(folder / 'job.json', {'binding': b.binding(config()),
            'status': 'pending', 'diagnostic': first})
        with patch.object(b, '_execute', side_effect=b.BackendError(
                'safe', category='authentication', stage='GET', http_status=401)):
            with self.assertRaises(b.BackendError):
                b.execute(config(), None, self.project, folder, True)
        state = b.read_json(folder / 'job.json')
        self.assertEqual(state['first_diagnostic'], first)
        self.assertEqual(state['diagnostic'], {'category': 'authentication', 'stage': 'GET', 'http_status': 401})
        with patch.object(b, '_execute', side_effect=b.BackendError('local rejection')):
            with self.assertRaises(b.BackendError):
                b.execute(config(), None, self.project, folder, True)
        self.assertEqual(b.read_json(folder / 'job.json'), state)

    def test_async_response_errors_replace_timeout_through_resume(self):
        cases = [('FAILED', 'task-failed', 'failed'),
                 ('CANCELED', 'task-canceled', 'failed'),
                 ('UNRECOGNIZED', 'task-status-unknown', 'failed'),
                 ('SUCCEEDED', 'missing-images', 'pending')]
        real_client = httpx.Client
        for status, category, final_status in cases:
            with self.subTest(status=status):
                folder = self.project / '.image-jobs' / status
                folder.mkdir(parents=True)
                cfg = config('dashscope', DASHSCOPE_API='multimodal-async')
                b.write_json(folder / 'job.json', {'binding': b.binding(cfg),
                    'protocol': 'dashscope', 'status': 'pending', 'task_id': 'existing'})
                calls = []
                def handler(request):
                    calls.append(request.method)
                    if len(calls) <= 3:
                        raise httpx.ReadTimeout('private timeout details')
                    return httpx.Response(200, json={'output': {'task_status': status}})
                def client(**kwargs):
                    return real_client(transport=httpx.MockTransport(handler), **kwargs)
                with patch.object(b.httpx, 'Client', side_effect=client), patch.object(b.time, 'sleep'):
                    with self.assertRaises(b.BackendError):
                        b.execute(cfg, None, self.project, folder, resume=True)
                    first = b.read_json(folder / 'job.json')['diagnostic']
                    self.assertEqual(first, {'category': 'ReadTimeout', 'stage': 'GET'})
                    with self.assertRaises(b.BackendError):
                        b.execute(cfg, None, self.project, folder, resume=True)
                    state = b.read_json(folder / 'job.json')
                    self.assertEqual(state['first_diagnostic'], first)
                    self.assertEqual(state['diagnostic'], {'category': category, 'stage': 'poll-response'})
                    self.assertEqual(state['status'], final_status)
                    self.assertNotIn('private timeout details', json.dumps(state))
                    self.assertEqual(calls, ['GET'] * 4)
                    if final_status == 'failed':
                        with self.assertRaises(b.BackendError):
                            b.execute(cfg, None, self.project, folder, resume=True)
                        self.assertEqual(b.read_json(folder / 'job.json'), state)
                        self.assertEqual(calls, ['GET'] * 4)

    def test_profiles_select_complete_env_files(self):
        self.env(self.project)
        self.env(self.home / '.anime-pv', '.env.relay-b', protocol='gemini')
        selected = b.load_config(self.project, self.skill, self.home, profile='relay-b')
        self.assertEqual((selected['PROTOCOL'], selected['PROFILE']), ('gemini', 'relay-b'))
        for profile in (None, 'default'):
            selected = b.load_config(self.project, self.skill, self.home, profile=profile)
            self.assertEqual((selected['PROTOCOL'], selected['PROFILE']), ('openai', 'default'))
        for profile in ('example', '../x', 'Upper', '', 'missing'):
            with self.subTest(profile=profile), self.assertRaises(b.BackendError):
                b.load_config(self.project, self.skill, self.home, profile=profile)

    def test_route_file_authorizes_listed_profiles_only(self):
        current = {**config(), 'PROFILE': 'default'}
        relay = {**config('gemini'), 'PROFILE': 'relay-b'}
        legacy = {'route': 'backend', 'protocol': 'openai', 'user_choice': 'User chose the OpenAI API'}
        self.assertTrue(b.route_authorizes(legacy, current))
        self.assertFalse(b.route_authorizes(legacy, relay))
        self.assertFalse(b.route_authorizes({**legacy, 'user_choice': ' '}, current))
        routes = {'user_choice': 'User authorized built-in and two backends', 'routes': [
            {'route': 'builtin'},
            {'route': 'backend', 'profile': 'default', 'protocol': 'openai'},
            {'route': 'backend', 'profile': 'relay-b', 'protocol': 'gemini', 'model': 'test-image'}]}
        self.assertTrue(b.route_authorizes(routes, current))
        self.assertTrue(b.route_authorizes(routes, relay))
        self.assertFalse(b.route_authorizes(routes, {**relay, 'MODEL': 'other-model'}))
        self.assertFalse(b.route_authorizes(routes, {**config('seedream'), 'PROFILE': 'other'}))

    def test_cli_rejects_unauthorized_profile_before_network(self):
        self.env(self.project, '.env.relay-b', protocol='gemini')
        (self.project / 'state').mkdir()
        b.write_json(self.project / 'state/image-route.json',
                     {'route': 'backend', 'protocol': 'gemini', 'user_choice': 'Legacy file: default profile only'})
        b.write_json(self.project / 'state/request.json', {'prompt': 'PV'})
        argv = ['image_backend.py', '--project', str(self.project), '--profile', 'relay-b',
                '--request', str(self.project / 'state/request.json'), '--job', '.image-jobs/blocked']
        with patch.object(sys, 'argv', argv), patch.object(b.httpx, 'Client') as network, \
                redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as caught:
                b.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('image-route.json', error.getvalue())
        network.assert_not_called()
        self.assertFalse((self.project / '.image-jobs/blocked').exists())
        with patch.object(sys, 'argv', ['image_backend.py', '--project', str(self.project), '--profile', 'relay-b', '--check']), \
                redirect_stdout(io.StringIO()) as output:
            b.main()
        self.assertEqual(json.loads(output.getvalue())['profile'], 'relay-b')

    def test_job_profile_is_recorded_and_required_on_resume(self):
        folder = self.project / '.image-jobs/profiled'
        relay = {**config(), 'PROFILE': 'relay-b'}
        response = {'data': [{'b64_json': base64.b64encode(png()).decode()}]}
        with patch.object(b, 'request_json', return_value=response):
            state = b.execute(relay, {'prompt': 'PV'}, self.project, folder)
        self.assertEqual(state['profile'], 'relay-b')
        with self.assertRaises(b.BackendError) as caught:
            b.execute({**config(), 'PROFILE': 'default'}, None, self.project, folder, True)
        self.assertEqual(caught.exception.diagnostic['category'], 'profile-mismatch')
        self.assertIn('--profile relay-b', str(caught.exception))
        self.assertEqual(b.read_json(folder / 'job.json'), state)
        self.assertEqual(b.execute(relay, None, self.project, folder, True)['status'], 'complete')

    def test_request_timeout_reaches_post_client_and_is_bounded(self):
        timeouts = []
        real_client = httpx.Client
        payload = {'data': [{'b64_json': base64.b64encode(png()).decode()}]}
        def client(**kwargs):
            timeouts.append(kwargs['timeout'])
            return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), **kwargs)
        with patch.object(b.httpx, 'Client', side_effect=client):
            b.execute(config(), {'prompt': 'PV'}, self.project, self.project / '.image-jobs/slow', request_timeout=900)
        self.assertEqual((timeouts[0].read, timeouts[0].write, timeouts[0].connect), (900, 900, 30))
        with patch.object(sys, 'argv', ['image_backend.py', '--project', str(self.project), '--check', '--request-timeout', '5']), \
                patch.object(b, 'load_config') as load, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                b.main()
        self.assertEqual(caught.exception.code, 2)
        load.assert_not_called()

    def test_results_are_kept_when_gateways_add_success_codes(self):
        image = {'b64_json': 'YWJj'}
        cases = [({'code': 200, 'msg': 'success', 'data': [image]}, None),
                 ({'base_resp': None, 'data': [image]}, None),
                 ({'code': 'InvalidApiKey', 'message': 'hidden'}, 'provider-error'),
                 ({'code': 500, 'data': None}, 'provider-error'),
                 ({'error': {'message': 'hidden'}, 'data': [image]}, 'provider-error')]
        for body, category in cases:
            with self.subTest(body=body), httpx.Client(transport=httpx.MockTransport(
                    lambda request, body=body: httpx.Response(200, json=body))) as client:
                if category is None:
                    self.assertEqual(b.request_json(client, 'POST', 'https://api.test', {}, {}), body)
                else:
                    with self.assertRaises(b.BackendError) as caught:
                        b.request_json(client, 'POST', 'https://api.test', {}, {})
                    self.assertEqual(caught.exception.diagnostic['category'], category)
        self.assertEqual(b.extract_images('openai', {'code': 200, 'data': [image]}), [{'base64': 'YWJj'}])
        for protocol, body in (('openai', {'data': None}), ('dashscope', {'output': None}), ('gemini', {'candidates': None})):
            self.assertEqual(b.extract_images(protocol, body), [])

    def test_preflight_reports_route_capabilities(self):
        paths = {'ffmpeg': '/bin/ffmpeg', 'ffprobe': '/bin/ffprobe', 'fc-list': '/bin/fc-list'}
        listings = {'-encoders': 'Encoders:\n V..... = Video\n ------\n V....D libx264  H.264\n A....D aac  AAC\n',
                    '-filters': 'Filters:\n  T.. = Timeline support\n TSC xfade  VV->V  Cross fade\n T.. showinfo  V->V  Info\n'}
        def run(command, **kwargs):
            if command[0] == '/bin/fc-list':
                output = 'Noto Sans CJK SC,Noto Sans CJK\nSource Han Sans\\-SC\n' if command[1] == ':lang=zh' else ''
            else:
                output = listings.get(command[-1], 'ffmpeg version 9.0\n')
            return subprocess.CompletedProcess(command, 0, output, '')
        with patch.object(p.shutil, 'which', side_effect=paths.get), patch.object(p.subprocess, 'run', side_effect=run), \
                patch.object(p, 'FULL_FFMPEG', ()), patch.object(p, 'find_browser', return_value=None):
            report = p.preflight()
        capabilities = report['ffmpeg_capabilities']['ffmpeg']
        self.assertTrue(capabilities['encoders']['libx264'])
        self.assertFalse(capabilities['encoders']['libx265'])
        self.assertEqual((capabilities['filters']['xfade'], capabilities['filters']['drawtext']), (True, False))
        self.assertEqual(report['fonts']['zh'], {'families': 2, 'examples': ['Noto Sans CJK SC', 'Source Han Sans-SC']})
        self.assertEqual(report['fonts']['ja']['families'], 0)
        self.assertTrue(any('drawtext' in note for note in report['notes']))
        self.assertIsNone(report['tools']['blender']['path'])
        self.assertEqual(report['local_ai'], 'not started')

    def test_init_and_package_portable_files(self):
        (self.project / '.gitignore').write_text('user-rule\n', encoding='utf-8')
        p.init_project(self.project, 'replica')
        for name in ('tmp', '.cache'):
            self.assertTrue((self.project / name).is_dir())
        rules = (self.project / '.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertTrue({'user-rule', '/tmp/', '/.cache/'}.issubset(rules))
        with self.assertRaises(ValueError):
            p.init_project(self.project, 'original')
        manifest = self.project / 'state/package-files.txt'
        manifest.write_text('HANDOFF.md\n图像.png\n', encoding='utf-8')
        archive = self.root / 'project.zip'
        p.package(self.project, 'state/package-files.txt', archive)
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(set(zipped.namelist()), {'HANDOFF.md', '图像.png'})
        for entry in ('.env', '../secret', '.image-jobs/job.json'):
            manifest.write_text(entry, encoding='utf-8')
            with self.assertRaises(ValueError):
                p.package(self.project, 'state/package-files.txt', self.root / 'bad.zip')


if __name__ == '__main__':
    unittest.main()
