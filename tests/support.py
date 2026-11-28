"""Shared fixtures: a fake yt-dlp that really produces audio via the real ffmpeg.

The fake has to be launchable the way the helper launches tools: on POSIX a shebang script named
`yt-dlp`; on Windows `helper.tool()` falls back to `shutil.which('yt-dlp')`, which honors PATHEXT,
so the fake is a `yt-dlp.bat` shim next to a plain `yt-dlp.py` (CreateProcess runs batch files
through cmd.exe; the folder is put on PATH so `which` finds the shim).
"""
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

ON_WINDOWS = os.name == 'nt'

FAKE_YTDLP_CODE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
state = Path(os.environ['FAKE_STATE']); state.mkdir(parents=True, exist_ok=True)
mode = (state / 'mode').read_text().strip() if (state / 'mode').exists() else 'ok'
template = Path(args[args.index('-o') + 1])
work = template.parent
vid = args[-1].split('v=')[-1]
with open(state / 'calls.log', 'a') as log:
    log.write(json.dumps({'part_existed': any(work.glob('*.part')), 'continue': '--continue' in args}) + '\n')
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
print('[download] 100% of 3.00MiB', flush=True)
(work / 'source.info.json').write_text(json.dumps(
    {'id': vid, 'title': 'Test Song', 'uploader': 'Test Artist', 'duration': 3.0,
     'upload_date': '20240101', 'acodec': 'aac'}))
if '--write-thumbnail' in args:
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=red:s=64x64',
                    '-frames:v', '1', str(work / 'source.jpg')], check=True)
'''


def tool_data_file(folder, name):
    """The file `helper.tool(name)` picks up directly in tools_dir ('<name>.exe' on Windows,
    '<name>' elsewhere). For fakes that are only hashed or reported, never launched."""
    return Path(folder) / (name + '.exe' if ON_WINDOWS else name)


def fake_tool_file(folder, name='yt-dlp'):
    """Where make_launchable_tool puts the *launchable* fake for `name`."""
    return Path(folder) / (name + '.bat' if ON_WINDOWS else name)


def make_launchable_tool(folder, body, name='yt-dlp'):
    """Write `body` (python code, no shebang) as a tool the helper can launch.

    POSIX: a shebang script named `<name>` (helper.tool() finds it in tools_dir). Windows:
    `<name>.py` plus a `<name>.bat` shim — helper.tool() falls back to shutil.which(), which
    honors PATHEXT, and CreateProcess runs batch files through cmd.exe, which passes the exit
    code of the interpreter on. The folder joins PATH so `which` finds the shim.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if ON_WINDOWS:
        (folder / (name + '.py')).write_text(body, encoding='utf-8')
        shim = ('@echo off\r\n@"' + sys.executable + '" "%~dp0' + name + '.py" %*\r\n'
                '@exit /b %ERRORLEVEL%\r\n')
        fake_tool_file(folder, name).write_text(shim, encoding='utf-8')
        os.environ['PATH'] = str(folder) + os.pathsep + os.environ.get('PATH', '')
    else:
        script = folder / name
        script.write_text('#!' + sys.executable + '\n' + body, encoding='utf-8')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)


def make_tools(folder, mode='ok'):
    folder = Path(folder)
    state = folder.parent / 'fake-state'
    state.mkdir(exist_ok=True)
    (state / 'mode').write_text(mode)
    os.environ['FAKE_STATE'] = str(state)
    make_launchable_tool(folder, FAKE_YTDLP_CODE)
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
