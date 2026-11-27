#!/usr/bin/env python3
"""Audit CSS selectors against the HTML/JS that use them (informational, not a gate)."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / 'app' / 'extension'


def css_classes(css_text):
    names = set()
    # strip comments
    css_text = re.sub(r'/\*.*?\*/', '', css_text, flags=re.S)
    for rule in re.finditer(r'([^{}]+)\{', css_text):
        for sel in rule.group(1).split(','):
            sel = sel.strip()
            if sel.startswith('@'):
                continue
            for m in re.finditer(r'\.([A-Za-z_][\w-]*)', sel):
                names.add(m.group(1))
    return names


def used_names():
    used = set()
    for f in EXT.glob('*.html'):
        text = f.read_text(encoding='utf-8')
        used.update(re.findall(r'class="([^"]+)"', text))
        used.update(re.findall(r'class=([A-Za-z_][\w-]*)', text))
    for f in EXT.glob('*.js'):
        text = f.read_text(encoding='utf-8')
        # className='job-state '+x / classList.add('online') / E('x').className='...'
        used.update(re.findall(r"className\s*=\s*['\"`]([^'\"`]+)", text))
        used.update(re.findall(r"className\s*=\s*['\"`]([^'\"`]+)['\"`]", text))
        used.update(re.findall(r"classList\.(?:add|remove|toggle)\(\s*['\"]([\w -]+)['\"]", text))
    flat = set()
    for entry in used:
        for piece in re.split(r'[\s+]+', entry):
            if piece:
                flat.add(piece)
    return flat


def main():
    for css in ('popup.css', 'dashboard.css', 'content.css'):
        text = (EXT / css).read_text(encoding='utf-8')
        classes = css_classes(text)
        used = used_names()
        if css == 'content.css':
            # content.css targets the injected button id and YouTube's DOM; classes are for our own
            # button only.
            unused = sorted(c for c in classes if c not in used and c != 'brave-music-download-wav')
        else:
            unused = sorted(c for c in classes if c not in used)
        print(f'{css}: {len(classes)} class selectors, unused: {unused if unused else "none"}')


if __name__ == '__main__':
    main()
