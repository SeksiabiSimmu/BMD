#!/usr/bin/env python3
"""Build the setup launcher: the Windows program that is the start of Setup.exe.

    python scripts/build_launcher.py [-o <file.exe>] [--check-reproducible]

It needs Zig 0.14 (set ZIG=/path/to/zig, or have `zig` on the PATH) and does two steps:

    zig rc   setup.rc  -> icon and version information
    zig cc   setup_launcher.c + launcher_core.c -> a 64-bit Windows program without any C runtime

The same sources and Zig version always give the same bytes: the program is stripped, so no time
stamp or build path is left in it. --check-reproducible builds twice and compares. The file built
here is only the program; scripts/build_release.py appends the files to install.
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pe_tools import launcher_problems, pe_info   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / 'app' / 'installer' / 'launcher'
DEFAULT_OUTPUT = ROOT / 'dist' / 'launcher' / 'BraveMusicDownloader-Setup.exe'
SOURCES = ('setup_launcher.c', 'launcher_core.c')
LIBRARIES = ('kernel32', 'user32', 'shell32')     # every function the launcher imports
ZIG_VERSION = '0.14'


class BuildError(Exception):
    pass


def setup_version(folder=LAUNCHER):
    """The version text from setup_version.h."""
    for line in (Path(folder) / 'setup_version.h').read_text().splitlines():
        if line.startswith('#define SETUP_VERSION_TEXT'):
            return line.split('"')[1]
    raise BuildError('setup_version.h does not define SETUP_VERSION_TEXT')


def find_zig(value=None):
    for candidate in (value, os.environ.get('ZIG'), shutil.which('zig')):
        if not candidate:
            continue
        found = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if found:
            return str(Path(found).resolve())
    raise BuildError('Zig was not found. Download it from https://ziglang.org/download/ '
                     f'(version {ZIG_VERSION}.x) and set ZIG=/path/to/zig.')


def run(args, cwd=None, env=None):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise BuildError(f'command failed ({result.returncode}): {" ".join(str(a) for a in args)}\n'
                         f'{result.stdout}{result.stderr}'.strip())
    return result


def windows_headers(zig):
    """The MinGW-w64 headers Zig ships (for <windows.h>)."""
    directory = Path(zig).parent / 'lib' / 'libc' / 'include' / 'any-windows-any'
    if not (directory / 'windows.h').is_file():
        raise BuildError(f'{directory} has no windows.h: this does not look like a Zig installation')
    return directory


def build(zig, output, work=None, folder=None):
    """Compile the launcher and write it to `output`. Returns (sha256 hex, size).

    `folder` is the folder holding the launcher's sources (default: this repository's); `work`, when
    given, is used for the intermediate files and kept."""
    zig = find_zig(zig)
    version = run([zig, 'version']).stdout.strip()
    if not version.startswith(ZIG_VERSION + '.'):
        raise BuildError(f'Zig {version} found, {ZIG_VERSION}.x expected: a different compiler gives different bytes')
    headers = windows_headers(zig)
    folder = Path(folder or LAUNCHER)
    temporary = work is None
    work = Path(work or tempfile.mkdtemp(prefix='bmd-launcher-'))
    try:
        work.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.setdefault('ZIG_LOCAL_CACHE_DIR', str(ROOT / '.zig-cache'))      # keep the build files inside the project
        env.setdefault('ZIG_GLOBAL_CACHE_DIR', str(ROOT / '.zig-cache' / 'global'))
        resource = work / 'setup.res'
        run([zig, 'rc', '/i', headers, '/fo', resource, folder / 'setup.rc'], cwd=folder, env=env)
        image = work / 'setup.exe'
        run([zig, 'cc', '-target', 'x86_64-windows-gnu', '-std=c11', '-O1', '-nostdlib', '-isystem', headers,
             '-DUNICODE', '-D_UNICODE', '-Wall', '-Wextra', '-Werror',
             '-Wl,--subsystem,windows', '-Wl,-e,mainCRTStartup', '-s', '-o', image,
             *[folder / name for name in SOURCES], resource, folder / 'setup.manifest',
             *[f'-l{library}' for library in LIBRARIES]], env=env)
        data = image.read_bytes()
        problems = launcher_problems(data, setup_version(folder))
        if problems:
            raise BuildError('the built program is not a usable setup launcher:\n  ' + '\n  '.join(problems))
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image, output)
        return hashlib.sha256(data).hexdigest(), len(data)
    finally:
        if temporary:
            shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='build the setup launcher with Zig')
    parser.add_argument('-o', '--output', type=Path, default=DEFAULT_OUTPUT, help='where to write the program')
    parser.add_argument('--zig', help='the zig executable (default: $ZIG, or zig on the PATH)')
    parser.add_argument('--check-reproducible', action='store_true',
                        help='build twice and fail when the two files differ')
    args = parser.parse_args(argv)
    try:
        first = build(args.zig, args.output)
        info = pe_info(args.output.read_bytes())
        print(f'{args.output}  {first[1]} bytes  sha256 {first[0]}')
        print(f'  zig {run([find_zig(args.zig), "version"]).stdout.strip()}  entry {info["entry_rva"]:#x}  '
              f'imports {", ".join(info["imports"])}  version {info["version"]["file"]}')
        if args.check_reproducible:
            with tempfile.TemporaryDirectory(prefix='bmd-launcher-check-') as folder:
                second = build(args.zig, Path(folder) / 'again.exe')
            if second != first:
                raise BuildError(f'two builds differ: {first} and {second}')
            print('  reproducible: a second build gave the same bytes')
        return 0
    except BuildError as exc:
        print(f'BUILD FAILED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
