# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Portable PV project helpers; no shell execution or model installation."""
import argparse
import json
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


def init_project(root, mode):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / 'HANDOFF.md'
    if target.exists():
        raise ValueError('HANDOFF.md already exists; resume it instead of overwriting.')
    template = (SKILL / 'assets/HANDOFF.template.md').read_text(encoding='utf-8')
    template = template.replace('项目：待填写', '项目：' + root.name).replace('模式：待填写', '模式：' + mode)
    for name in ('inputs', 'assets', 'design', 'previews', 'render', 'deliverables', 'state/history'):
        (root / name).mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as handle:
        handle.write(template)
    ignore = root / '.gitignore'
    old = ignore.read_text(encoding='utf-8') if ignore.exists() else ''
    rules = ['.env', '.env.*', '!.env.example', '.image-jobs/', '.venv/', '__pycache__/']
    with ignore.open('a', encoding='utf-8') as handle:
        handle.write('\n' + '\n'.join(r for r in rules if r not in old.splitlines()) + '\n')
    return {'handoff': str(target), 'stage': 1, 'approved': False}


def preflight():
    system = platform.system()
    names = ('uv', 'ffmpeg', 'ffprobe', 'node', 'magick')
    tools = {}
    for name in names:
        path = shutil.which(name)
        version = None
        if path:
            try:
                flag = '-version' if name in ('ffmpeg', 'ffprobe', 'magick') else '--version'
                result = subprocess.run([path, flag], capture_output=True, text=True, timeout=10, errors='replace')
                version = (result.stdout or result.stderr).splitlines()[0]
            except (OSError, subprocess.TimeoutExpired, IndexError):
                version = 'present; version check failed'
        tools[name] = {'path': path, 'version': version, 'required_for': 'media inspection/rendering' if name.startswith('ff') else 'route-dependent'}
    if system in ('Darwin', 'Linux'):
        install = 'If Homebrew is available: brew install uv ffmpeg; install node/imagemagick only if the chosen renderer needs them. Linux without Homebrew: use the distribution package manager or official installers.'
    elif system == 'Windows':
        install = 'PowerShell: winget install --id astral-sh.uv -e; winget install --id Gyan.FFmpeg -e. Reopen the terminal after PATH changes. Install Node.js/ImageMagick only when selected.'
    else:
        install = 'Use official installers appropriate for this platform.'
    return {'system': system, 'machine': platform.machine(), 'python': sys.version.split()[0], 'tools': tools, 'installation_hint': install, 'renderer_smoke_test': 'not performed', 'local_ai': 'not started'}


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
