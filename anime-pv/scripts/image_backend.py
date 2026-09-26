# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.28,<1", "python-dotenv>=1,<2", "pillow>=11,<13"]
# ///
"""Image APIs for anime-pv. POST is never automatically retried."""
import argparse
import base64
import hashlib
import io
import json
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from dotenv import dotenv_values
from PIL import Image

SKILL = Path(__file__).resolve().parents[1]
PREFIX = 'ANIME_PV_'
PROTOCOLS = {'openai', 'gemini', 'seedream', 'dashscope', 'minimax'}


class BackendError(Exception):
    """Safe user-facing error; excludes raw response bodies and credentials."""


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise BackendError('Cannot read JSON input/state file.') from None


def write_json(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def load_config(project, skill=SKILL, home=None):
    home = Path.home() if home is None else Path(home)
    for root in (Path(project), Path(skill), home / '.anime-pv'):
        path = root / '.env'
        if not path.is_file():
            continue
        # No environment interpolation, export to os.environ, or cross-file merge.
        raw = dotenv_values(path, interpolate=False)
        values = {k[len(PREFIX):]: v for k, v in raw.items() if k.startswith(PREFIX)}
        if not values:
            continue
        missing = [k for k in ('PROTOCOL', 'ENDPOINT', 'API_KEY', 'MODEL') if not values.get(k)]
        if missing:
            raise BackendError('First relevant .env is incomplete; missing: ' + ', '.join(missing))
        if values['PROTOCOL'] not in PROTOCOLS:
            raise BackendError('Unsupported protocol; no backend fallback performed.')
        endpoint = values['ENDPOINT'].rstrip('/')
        parts = urlsplit(endpoint)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise BackendError('Endpoint must be an HTTPS API base without credentials, query, or fragment.')
        values['ENDPOINT'] = endpoint
        try:
            values['PARAMS'] = json.loads(values.get('PARAMS') or '{}')
        except ValueError:
            raise BackendError('ANIME_PV_PARAMS must contain a JSON object.') from None
        if not isinstance(values['PARAMS'], dict):
            raise BackendError('ANIME_PV_PARAMS must contain a JSON object.')
        values['SOURCE'] = str(path.resolve())
        return values
    raise BackendError('No relevant .env found in project, skill, or ~/.anime-pv.')


def binding(config):
    public = {k: config.get(k) for k in ('PROTOCOL', 'ENDPOINT', 'MODEL', 'DASHSCOPE_API')}
    return hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()


def local_image(value, project):
    path = Path(value)
    if not path.is_absolute():
        path = Path(project) / path
    try:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            mime = Image.MIME.get(image.format)
            image.verify()
    except (OSError, ValueError):
        raise BackendError('Invalid or inaccessible local image.') from None
    if mime not in ('image/png', 'image/jpeg', 'image/webp'):
        raise BackendError('Local image must be PNG, JPEG, or WebP.')
    return path.name, mime, data


def image_uri(value, project):
    if value.startswith('https://'):
        return value
    if value.startswith('data:image/'):
        return value
    _, mime, data = local_image(value, project)
    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode()


def merge(base, extra):
    result = dict(base)
    for key, value in extra.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def build_request(config, spec, project):
    protocol, endpoint = config['PROTOCOL'], config['ENDPOINT']
    operation = spec.get('operation', 'generate')
    if operation not in ('generate', 'reference', 'edit'):
        raise BackendError('Operation must be generate, reference, or edit.')
    prompt, images = spec.get('prompt'), spec.get('images', [])
    mask = spec.get('mask')
    if not isinstance(prompt, str) or not prompt.strip():
        raise BackendError('A nonempty prompt is required.')
    if not isinstance(images, list) or not all(isinstance(x, str) for x in images):
        raise BackendError('images must be a list of paths or supported URLs.')
    if (operation == 'generate' and images) or (operation != 'generate' and not images):
        raise BackendError('Generate has no input images; reference/edit require images.')
    extra = spec.get('params', {})
    if not isinstance(extra, dict):
        raise BackendError('params must be an object.')
    extra = merge(config['PARAMS'], extra)
    protected = {'model', 'prompt', 'image', 'images', 'mask', 'subject_reference', 'contents'}
    if protected.intersection(extra) or any(k in extra.get('input', {}) for k in ('prompt', 'messages', 'base_image_url', 'mask_image_url')):
        raise BackendError('Native params cannot override model, prompt, or image inputs.')
    if extra.get('stream') or extra.get('parameters', {}).get('stream') or extra.get('parameters', {}).get('enable_interleave'):
        raise BackendError('Streaming/interleaved output is not supported by this adapter.')
    headers = {'Authorization': 'Bearer ' + config['API_KEY']}
    body = {'model': config['MODEL'], 'prompt': prompt}
    files = None
    if mask and not (operation == 'edit' and (protocol == 'openai' or (protocol == 'dashscope' and config.get('DASHSCOPE_API') == 'edit'))):
        raise BackendError('Explicit masks are only supported by OpenAI edits and DashScope edit profile.')
    if protocol == 'openai':
        path = '/images/generations' if operation == 'generate' else '/images/edits'
        if images:
            files = [('image[]' if len(images) > 1 else 'image', local_image(x, project)) for x in images]
            if mask:
                files.append(('mask', local_image(mask, project)))
            # httpx file tuples use name, bytes, content type.
            files = [(field, (name, data, mime)) for field, (name, mime, data) in files]
    elif protocol == 'seedream':
        path = '/images/generations'
        if images:
            body['image'] = [image_uri(x, project) for x in images]
    elif protocol == 'minimax':
        path = '/image_generation'
        if operation == 'edit':
            raise BackendError('MiniMax supports character reference generation, not generic edits.')
        if len(images) > 1:
            raise BackendError('MiniMax adapter accepts one character reference.')
        if images:
            body['subject_reference'] = [{'type': 'character', 'image_file': image_uri(images[0], project)}]
    elif protocol == 'gemini':
        headers = {'x-goog-api-key': config['API_KEY']}
        parts = [{'text': prompt}]
        for value in images:
            _, mime, data = local_image(value, project)
            parts.append({'inlineData': {'mimeType': mime, 'data': base64.b64encode(data).decode()}})
        path = '/models/' + quote(config['MODEL'], safe='') + ':generateContent'
        body = {'contents': [{'role': 'user', 'parts': parts}], 'generationConfig': {'responseModalities': ['TEXT', 'IMAGE']}}
    else:
        profile = config.get('DASHSCOPE_API', 'synthesis')
        body = {'model': config['MODEL'], 'input': {'prompt': prompt}, 'parameters': {}}
        if profile == 'synthesis':
            if images:
                raise BackendError('DashScope synthesis profile is text-to-image only; choose an approved edit profile.')
            path = '/services/aigc/text2image/image-synthesis'
            headers['X-DashScope-Async'] = 'enable'
        elif profile == 'edit':
            if len(images) != 1:
                raise BackendError('DashScope edit profile requires exactly one base image.')
            body['input']['base_image_url'] = image_uri(images[0], project)
            body['input']['function'] = 'description_edit_with_mask' if mask else 'description_edit'
            if mask:
                body['input']['mask_image_url'] = image_uri(mask, project)
            path = '/services/aigc/image2image/image-synthesis'
            headers['X-DashScope-Async'] = 'enable'
        elif profile in ('multimodal', 'multimodal-async'):
            content = [{'text': prompt}] + [{'image': image_uri(x, project)} for x in images]
            body['input'] = {'messages': [{'role': 'user', 'content': content}]}
            path = '/services/aigc/multimodal-generation/generation'
            if profile.endswith('-async'):
                path = '/services/aigc/image-generation/generation'
                headers['X-DashScope-Async'] = 'enable'
        else:
            raise BackendError('Unknown DashScope profile.')
    body = merge(body, extra)
    if files:
        body = {key: (json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)) for key, value in body.items()}
    return endpoint + path, headers, body, files


def request_json(client, method, url, headers, body=None, files=None):
    attempts = 3 if method == 'GET' else 1
    for attempt in range(attempts):
        try:
            kwargs = {'headers': headers}
            if body is not None:
                kwargs.update({'data': body, 'files': files} if files else {'json': body})
            response = client.request(method, url, **kwargs)
            if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            if response.is_error or response.is_redirect:
                category = 'authentication' if response.status_code in (401, 403) else 'rate-limit' if response.status_code == 429 else 'request/server'
                raise BackendError(f'{category} error (HTTP {response.status_code}); response body hidden, no POST retry.')
            data = response.json()
            if not isinstance(data, dict):
                raise BackendError('Unexpected API response shape.')
            if data.get('error') or data.get('code') or data.get('base_resp', {}).get('status_code', 0) != 0:
                raise BackendError('Provider reported an error; raw response hidden. Check model, parameters and account console.')
            return data
        except (httpx.RequestError, ValueError):
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            raise BackendError('Network/response failure; submission may have succeeded. Do not blindly resubmit.') from None


def extract_images(protocol, response):
    result = []
    if protocol in ('openai', 'seedream'):
        for entry in response.get('data', []):
            if entry.get('b64_json'):
                result.append({'base64': entry['b64_json']})
            elif entry.get('url'):
                result.append({'url': entry['url']})
    elif protocol == 'minimax':
        data = response.get('data', {})
        result.extend({'url': x} for x in data.get('image_urls', []))
        result.extend({'base64': x} for x in data.get('image_base64', []))
    elif protocol == 'gemini':
        for candidate in response.get('candidates', []):
            for part in candidate.get('content', {}).get('parts', []):
                inline = part.get('inlineData', part.get('inline_data', {}))
                if not part.get('thought') and inline.get('data'):
                    result.append({'base64': inline['data']})
    else:
        output = response.get('output', {})
        result.extend({'url': x['url']} for x in output.get('results', []) if x.get('url'))
        for choice in output.get('choices', []):
            for part in choice.get('message', {}).get('content', []):
                if part.get('image'):
                    result.append({'url': part['image']})
    return result


def save_images(results, folder):
    paths = []
    # Dedicated download client: never forward provider auth to image CDN.
    with httpx.Client(timeout=120, follow_redirects=True) as downloader:
        for index, item in enumerate(results):
            if 'base64' in item:
                try:
                    data = base64.b64decode(item['base64'].split(',')[-1], validate=True)
                except ValueError:
                    raise BackendError('Invalid base64 image in provider response.') from None
            else:
                url = item['url']
                if not url.startswith('https://'):
                    raise BackendError('Result download URL must use HTTPS.')
                try:
                    response = downloader.get(url)
                    response.raise_for_status()
                    data = response.content
                except httpx.HTTPError:
                    raise BackendError('Image download failed; resume cached results without resubmitting.') from None
            try:
                with Image.open(io.BytesIO(data)) as image:
                    suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}.get(image.format)
                    image.verify()
                if not suffix:
                    raise ValueError()
            except (OSError, ValueError):
                raise BackendError('Provider result is not a valid supported image.') from None
            path = Path(folder) / f'image-{index + 1:03d}{suffix}'
            # A resumed download may replace only identical content, never a modified asset.
            if path.exists() and path.read_bytes() != data:
                raise BackendError('Existing result differs; preserve it and choose a separate job.')
            path.write_bytes(data)
            paths.append({'path': path.name, 'sha256': hashlib.sha256(data).hexdigest()})
    return paths


def execute(config, spec, project, folder, resume=False, wait_seconds=40):
    folder = Path(folder)
    state_path = folder / 'job.json'
    result_path = folder / 'results.private.json'
    if resume:
        state = read_json(state_path)
        if state.get('binding') != binding(config):
            raise BackendError('Backend/model/endpoint changed; restore original config before resuming.')
        if state.get('status') == 'complete':
            for item in state.get('files', []):
                path = folder / item['path']
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                    raise BackendError('Completed output missing or modified.')
            return state
        if state.get('status') in ('failed', 'unknown'):
            raise BackendError('Job failed or submission is uncertain; inspect account console before creating a new job.')
    else:
        url, headers, body, files = build_request(config, spec, project)
        folder.mkdir(parents=True, exist_ok=False)
        state = {'binding': binding(config), 'protocol': config['PROTOCOL'], 'status': 'unknown'}
        write_json(state_path, state)
        with httpx.Client(timeout=180, follow_redirects=False) as client:
            response = request_json(client, 'POST', url, headers, body, files)
        task = response.get('output', {}).get('task_id')
        if task:
            if config['PROTOCOL'] != 'dashscope':
                raise BackendError('Unexpected async task from this protocol.')
            state.update(task_id=task, status='pending')
        else:
            results = extract_images(config['PROTOCOL'], response)
            if not results:
                state['status'] = 'failed'
                write_json(state_path, state)
                raise BackendError('No final images returned; possible policy/model/response mismatch.')
            write_json(result_path, results)
            state['status'] = 'download'
        write_json(state_path, state)
    if state['status'] == 'pending':
        deadline = time.monotonic() + wait_seconds
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            while True:
                url = config['ENDPOINT'] + '/tasks/' + quote(state['task_id'], safe='')
                response = request_json(client, 'GET', url, {'Authorization': 'Bearer ' + config['API_KEY']})
                status = response.get('output', {}).get('task_status')
                if status == 'SUCCEEDED':
                    results = extract_images('dashscope', response)
                    if not results:
                        raise BackendError('Task succeeded without readable images.')
                    write_json(result_path, results)
                    state['status'] = 'download'
                    write_json(state_path, state)
                    break
                if status not in ('PENDING', 'RUNNING'):
                    state['status'] = 'failed'
                    write_json(state_path, state)
                    raise BackendError('Task failed, was canceled, or is unknown; no resubmission performed.')
                if time.monotonic() >= deadline:
                    return state
                time.sleep(min(10, max(0, deadline - time.monotonic())))
    if state['status'] == 'download':
        state['files'] = save_images(read_json(result_path), folder)
        state['status'] = 'complete'
        write_json(state_path, state)
        result_path.unlink(missing_ok=True)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='Validate config only; no network/key output')
    parser.add_argument('--request', type=Path, help='JSON request file; paths inside resolve from project')
    parser.add_argument('--job', type=Path, help='New job directory (relative paths resolve from project)')
    parser.add_argument('--resume', action='store_true', help='Resume job without a new POST')
    parser.add_argument('--wait-seconds', type=int, default=40, choices=range(0, 51), metavar='0..50')
    args = parser.parse_args()
    try:
        config = load_config(args.project)
        if args.check:
            print(json.dumps({'source': config['SOURCE'], 'protocol': config['PROTOCOL'], 'model': config['MODEL'], 'configuration_valid': True, 'network_tested': False}, ensure_ascii=False))
            return
        route = read_json(args.project / 'state/image-route.json')
        if route.get('route') != 'backend' or route.get('protocol') != config['PROTOCOL'] or not route.get('user_choice'):
            raise BackendError('Record the user-selected backend route/protocol and choice evidence in state/image-route.json first.')
        if args.job is None or (not args.resume and args.request is None):
            raise BackendError('--job and a new --request (or --resume) are required.')
        folder = args.job if args.job.is_absolute() else args.project / args.job
        spec = None if args.resume else read_json(args.request)
        result = execute(config, spec, args.project, folder, args.resume, args.wait_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except BackendError as error:
        parser.exit(1, str(error) + '\n')
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        parser.exit(1, 'Local I/O or invalid request/response structure; no automatic resubmission.\n')


if __name__ == '__main__':
    main()
