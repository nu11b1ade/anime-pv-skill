# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Portable PV project helpers; no shell execution or model installation."""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
ENCODERS = ('libx264', 'libx265', 'libsvtav1', 'prores_ks', 'ffv1', 'aac')
FILTERS = ('drawtext', 'subtitles', 'ass', 'zscale', 'libvmaf', 'xfade', 'scdet', 'blackdetect', 'freezedetect', 'showinfo')
# Homebrew's core ffmpeg omits freetype/libass/zimg; ffmpeg-full is keg-only and must be called by path.
FULL_FFMPEG = ('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg', '/usr/local/opt/ffmpeg-full/bin/ffmpeg')
BROWSERS = ('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'chrome', 'msedge')


def init_project(root, mode):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / 'HANDOFF.md'
    if target.exists():
        raise ValueError('HANDOFF.md already exists; resume it instead of overwriting.')
    template = (SKILL / 'assets/HANDOFF.template.md').read_text(encoding='utf-8')
    template = template.replace('项目：待填写', '项目：' + root.name).replace('模式：待填写', '模式：' + mode)
    for name in ('inputs', 'assets', 'design', 'previews', 'render', 'deliverables', 'state/history', 'tmp', '.cache'):
        (root / name).mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as handle:
        handle.write(template)
    ignore = root / '.gitignore'
    old = ignore.read_text(encoding='utf-8') if ignore.exists() else ''
    rules = ['.env', '.env.*', '!.env.example', '.image-jobs/', '.venv/', '__pycache__/', '/tmp/', '/.cache/']
    with ignore.open('a', encoding='utf-8') as handle:
        handle.write('\n' + '\n'.join(r for r in rules if r not in old.splitlines()) + '\n')
    return {'handoff': str(target), 'stage': 1, 'approved': False}


def output_of(command, timeout=20):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def tool_version(path, flag):
    try:
        result = subprocess.run([path, flag], capture_output=True, text=True, timeout=10, errors='replace')
        return (result.stdout or result.stderr).splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return 'present; version check failed'


def ffmpeg_capabilities(path):
    listed = {}
    for kind in ('encoders', 'filters'):
        output = output_of([path, '-hide_banner', '-' + kind])
        if output is None:
            return {'checked': False}
        listed[kind] = {line.split()[1] for line in output.splitlines() if len(line.split()) >= 2}
    return {'checked': True,
            'encoders': {name: name in listed['encoders'] for name in ENCODERS},
            'filters': {name: name in listed['filters'] for name in FILTERS}}


def cjk_fonts():
    fc_list = shutil.which('fc-list')
    if not fc_list:
        return {'checked': False, 'reason': 'fc-list unavailable; verify fonts with the chosen renderer'}
    result = {'checked': True}
    for lang in ('zh', 'ja'):
        output = output_of([fc_list, ':lang=' + lang, 'family']) or ''
        families = sorted({line.split(',')[0].replace('\\-', '-').strip() for line in output.splitlines() if line.strip()})
        result[lang] = {'families': len(families), 'examples': families[:5]}
    return result


def find_browser():
    for name in BROWSERS:
        path = shutil.which(name)
        if path:
            return path
    candidates = [Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
                  Path('/Applications/Chromium.app/Contents/MacOS/Chromium')]
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / 'Google/Chrome/Application/chrome.exe')
    return next((str(path) for path in candidates if path.is_file()), None)


def preflight():
    system = platform.system()
    names = ('uv', 'ffmpeg', 'ffprobe', 'node', 'npx', 'magick', 'blender')
    tools = {}
    for name in names:
        path = shutil.which(name)
        version = tool_version(path, '-version' if name in ('ffmpeg', 'ffprobe', 'magick') else '--version') if path else None
        tools[name] = {'path': path, 'version': version, 'required_for': 'media inspection/rendering' if name.startswith('ff') else 'route-dependent'}
    full = next((path for path in FULL_FFMPEG if Path(path).is_file()), None)
    if full:
        tools['ffmpeg-full'] = {'path': full, 'version': tool_version(full, '-version'), 'required_for': 'optional full-feature ffmpeg; call by path'}
    capabilities = {name: ffmpeg_capabilities(tools[name]['path']) for name in ('ffmpeg', 'ffmpeg-full') if tools.get(name, {}).get('path')}
    notes = []
    missing = [name for name in ('drawtext', 'subtitles', 'ass') if capabilities.get('ffmpeg', {}).get('filters', {}).get(name) is False]
    if missing:
        if full:
            remedy = 'or call the detected ffmpeg-full binary by its path'
        elif system == 'Darwin':
            remedy = 'or brew install ffmpeg-full (keg-only) and call $(brew --prefix ffmpeg-full)/bin/ffmpeg explicitly without changing global PATH'
        else:
            remedy = 'or install an ffmpeg build with freetype/libass'
        notes.append('PATH ffmpeg lacks ' + ', '.join(missing) + ': render text as Pillow/browser/vector layers, ' + remedy + '.')
    if system in ('Darwin', 'Linux'):
        install = 'If Homebrew is available: brew install uv ffmpeg; install node/imagemagick/blender only if the chosen renderer needs them. Linux without Homebrew: use the distribution package manager or official installers.'
    elif system == 'Windows':
        install = 'PowerShell: winget install --id astral-sh.uv -e; winget install --id Gyan.FFmpeg -e. Reopen the terminal after PATH changes. Install Node.js/ImageMagick/Blender only when selected.'
    else:
        install = 'Use official installers appropriate for this platform.'
    return {'system': system, 'machine': platform.machine(), 'python': sys.version.split()[0], 'tools': tools,
            'ffmpeg_capabilities': capabilities, 'fonts': cjk_fonts(), 'browser': find_browser(), 'notes': notes,
            'installation_hint': install, 'renderer_smoke_test': 'not performed', 'local_ai': 'not started'}


def probe(path):
    executable = shutil.which('ffprobe')
    if not executable:
        raise ValueError('ffprobe missing; run preflight for installation guidance.')
    result = subprocess.run([executable, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(Path(path).resolve())], capture_output=True, text=True, errors='replace', timeout=60)
    if result.returncode:
        raise ValueError('ffprobe failed; check input existence and media integrity.')
    return json.loads(result.stdout)


def package(root, manifest, output):
    root = Path(root).resolve()
    manifest = root / manifest
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Archive already exists; choose a new version.')
    selected = []
    for line in manifest.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        rel = Path(line)
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('Package entries must be project-relative without traversal.')
        lowered = [part.lower() for part in rel.parts]
        if any(p.startswith('.env') or p in ('.image-jobs', '.git', '.venv', '__pycache__') or p.endswith(('.pem', '.key', '.p12', '.pfx')) or p in ('credentials.json', 'secrets.json') for p in lowered):
            raise ValueError('Credential/cache file rejected from package.')
        path = root / rel
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root and root in parent.parents):
            raise ValueError('Symlinks are not portable package inputs.')
        if not path.resolve().is_relative_to(root) or not path.is_file() or path.resolve() == output:
            raise ValueError('Package entry is missing, outside root, or the output itself.')
        selected.append((path, rel.as_posix()))
    if not selected:
        raise ValueError('Package manifest is empty.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, path in dict((name, path) for path, name in selected).items():
            archive.write(path, name)
    return {'archive': str(output), 'files': len(set(name for _, name in selected))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('root')
    init.add_argument('--mode', choices=['replica', 'original'], required=True)
    commands.add_parser('preflight')
    media = commands.add_parser('probe')
    media.add_argument('path')
    pack = commands.add_parser('pack')
    pack.add_argument('root')
    pack.add_argument('--manifest', required=True)
    pack.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'init':
            result = init_project(args.root, args.mode)
        elif args.command == 'preflight':
            result = preflight()
        elif args.command == 'probe':
            result = probe(args.path)
        else:
            result = package(args.root, args.manifest, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
