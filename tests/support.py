"""Shared fixtures: a fake yt-dlp that really produces audio via the real ffmpeg."""
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'app' / 'helper'
sys.path.insert(0, str(HELPER))
sys.path.insert(0, str(ROOT / 'app' / 'installer'))

FAKE_YTDLP = r'''#!{python}
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
state = Path(os.environ['FAKE_STATE']); state.mkdir(parents=True, exist_ok=True)
mode = (state / 'mode').read_text().strip() if (state / 'mode').exists() else 'ok'
template = Path(args[args.index('-o') + 1])
work = template.parent
vid = args[-1].split('v=')[-1]
with open(state / 'calls.log', 'a') as log:
    log.write(json.dumps({{'part_existed': any(work.glob('*.part')), 'continue': '--continue' in args}}) + '\n')
if mode == 'unavailable':
    print('ERROR: [youtube] %s: Video unavailable' % vid); sys.exit(1)
if mode == 'sleep':
    for i in range(300):
        print('[download]  %d.0%% of 3.00MiB' % (i % 90), flush=True); time.sleep(.1)
    sys.exit(0)
final = work / 'source.m4a'
part = work / 'source.m4a.part'
if mode == 'part_then_ok' and not part.exists():
    part.write_bytes(b'x' * 4096)
    print('[download]  40.0% of 3.00MiB', flush=True)
    print('ERROR: unable to download video data: HTTP Error 503'); sys.exit(1)
if part.exists():
    part.unlink()
print('[download]  50.0% of 3.00MiB', flush=True)
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                '-c:a', 'aac', str(final)], check=True)
print('[download] 100%% of 3.00MiB', flush=True)
(work / 'source.info.json').write_text(json.dumps(
    {{'id': vid, 'title': 'Test Song', 'uploader': 'Test Artist', 'duration': 3.0,
      'upload_date': '20240101', 'acodec': 'aac'}}))
if '--write-thumbnail' in args:
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=red:s=64x64',
                    '-frames:v', '1', str(work / 'source.jpg')], check=True)
'''


def make_tools(folder, mode='ok'):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / 'yt-dlp'
    script.write_text(FAKE_YTDLP.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    state = folder.parent / 'fake-state'
    state.mkdir(exist_ok=True)
    (state / 'mode').write_text(mode)
    os.environ['FAKE_STATE'] = str(state)
    return state


def set_mode(state, mode):
    (Path(state) / 'mode').write_text(mode)


def calls(state):
    import json
    path = Path(state) / 'calls.log'
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def ffprobe_json(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                          'stream=codec_name,sample_rate:format=duration:format_tags',
                          '-of', 'json', str(path)], capture_output=True, text=True, check=True).stdout
    import json
    return json.loads(out)


def tmpdir():
    return Path(tempfile.mkdtemp(prefix='bmd-test-'))
