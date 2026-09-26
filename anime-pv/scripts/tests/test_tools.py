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
        self.temp = tempfile.TemporaryDirectory()
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
        self.env(self.home / '.anime-pv', protocol='minimax')
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
                url, headers, body, files = b.build_request(config(protocol), {'prompt': 'PV'}, self.project)
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
        for protocol in ('gemini', 'seedream', 'minimax'):
            _, _, body, _ = b.build_request(config(protocol), spec, self.project)
            self.assertIn(base64.b64encode(png()).decode(), json.dumps(body))
        with self.assertRaises(b.BackendError):
            b.build_request(config('minimax'), {**spec, 'operation': 'edit'}, self.project)
        with self.assertRaises(b.BackendError):
            b.build_request(config('gemini'), {**spec, 'mask': '图像.png'}, self.project)

    def test_dashscope_profiles(self):
        spec = {'operation': 'edit', 'prompt': 'pose', 'images': ['图像.png']}
        for profile in ('edit', 'multimodal', 'multimodal-async'):
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
            'minimax': {'data': {'image_urls': ['https://cdn.test/x']}},
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
            self.assertEqual(b.execute(config('dashscope'), {'prompt': 'PV'}, self.project, folder, wait_seconds=0)['status'], 'pending')
        finished = {'output': {'task_status': 'SUCCEEDED', 'results': [{'url': 'https://cdn.test/x'}]}}
        with patch.object(b, 'request_json', return_value=finished) as send, patch.object(b, 'save_images', side_effect=b.BackendError('download failure')):
            with self.assertRaises(b.BackendError):
                b.execute(config('dashscope'), None, self.project, folder, True)
            self.assertEqual(send.call_args.args[1], 'GET')
        with patch.object(b, 'request_json') as send, patch.object(b, 'save_images', return_value=[]):
            self.assertEqual(b.execute(config('dashscope'), None, self.project, folder, True)['status'], 'complete')
            send.assert_not_called()

    def test_init_and_package_portable_files(self):
        p.init_project(self.project, 'replica')
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
