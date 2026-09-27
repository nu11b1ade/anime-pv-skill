import base64
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import image_backend as b
import project as p


def png():
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(stream, 'PNG')
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

    def env(self, root, **values):
        values = config(**values)
        (root / '.env').write_text('\n'.join('ANIME_PV_' + k + '=' + (json.dumps(v) if isinstance(v, dict) else v) for k, v in values.items()), encoding='utf-8')

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
