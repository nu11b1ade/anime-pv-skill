"""Validate distributable skill metadata and local Markdown links without network."""
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'anime-pv'


def main():
    text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
    pieces = text.split('---', 2)
    assert len(pieces) == 3 and pieces[0] == '', 'Missing skill frontmatter'
    meta = yaml.safe_load(pieces[1])
    assert meta['name'] == SKILL.name, 'Skill name/directory mismatch'
    assert re.fullmatch(r'[a-z0-9-]{1,63}', meta['name']), 'Invalid skill name'
    assert isinstance(meta['description'], str) and meta['description'].strip(), 'Missing description'
    assert '[TODO' not in text, 'Unfinished skill scaffold'
    interface = yaml.safe_load((SKILL / 'agents/openai.yaml').read_text(encoding='utf-8'))['interface']
    assert '$anime-pv' in interface['default_prompt'], 'Default prompt must reference skill'
    assert 25 <= len(interface['short_description']) <= 64, 'UI description length'
    docs = [ROOT / 'README.md', ROOT / 'CONTRIBUTING.md', ROOT / 'CHANGELOG.md']
    docs += list((ROOT / 'docs').glob('*.md')) + list(SKILL.rglob('*.md'))
    for path in docs:
        for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
            target = target.split()[0].strip('<>')
            parsed = urlsplit(target)
            if parsed.scheme or not parsed.path:
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            assert resolved.is_relative_to(ROOT), f'Non-portable link in {path.relative_to(ROOT)}'
            assert resolved.exists(), f'Broken link in {path.relative_to(ROOT)}: {target}'
    print(f'Skill metadata and local links validated across {len(docs)} Markdown files.')


if __name__ == '__main__':
    main()
