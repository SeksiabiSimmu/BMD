"""Fixtures for installer tests: fake payload, fake tool archives, local HTTP server."""
import hashlib
import http.server
import io
import json
import socketserver
import struct
import threading
import zipfile
from pathlib import Path

from support import ROOT, tmpdir

import installer_core as ic  # noqa: E402  (path set up by support)
import payload_container as pc  # noqa: E402


def sha(data):
    return hashlib.sha256(data).hexdigest()


PE_SIZE = 512               # one file-alignment unit
PE_DIRECTORIES = {0x10B: 96, 0x20B: 112}      # where the data directories start inside the optional header


def fake_pe(cert_offset=0, cert_size=0, magic=0x20B):
    """The smallest header the container reader accepts: MZ, PE signature, one optional header
    (0x20B = PE32+, 0x10B = PE32) with a Certificate Table entry. A real launcher header is
    larger, but the reader looks only at these fields."""
    pe = bytearray(PE_SIZE)
    pe[0:2] = b'MZ'
    struct.pack_into('<I', pe, 0x3c, 0x40)                              # e_lfanew
    pe[0x40:0x44] = b'PE\0\0'
    struct.pack_into('<HH', pe, 0x44, 0x8664, 1)                        # Machine, NumberOfSections
    struct.pack_into('<HH', pe, 0x54, 240, 0x22)                        # SizeOfOptionalHeader, Characteristics
    directories = PE_DIRECTORIES[magic]
    struct.pack_into('<H', pe, 0x58, magic)
    struct.pack_into('<I', pe, 0x58 + directories - 4, 16)              # NumberOfRvaAndSizes
    struct.pack_into('<II', pe, 0x58 + directories + 4 * 8, cert_offset, cert_size)   # directory 4: Certificate Table
    return bytes(pe)


def fake_sign(data, table_bytes=256):
    """What signtool does to the file layout, without any cryptography: the file is padded with
    zeros to a multiple of 8 (the certificate table starts on an 8-byte boundary), the table is
    appended at the very end and the header's Certificate Table entry points at it. Works for the
    fake header of fake_pe() and for a real program.
    The table deliberately contains the container magic: the footer must be found through the
    header, not by searching backwards for the magic."""
    data = bytearray(data)
    while len(data) % 8:
        data.append(0)
    table = bytes((k * 7 + 3) & 0xff for k in range(table_bytes)) + pc.MAGIC
    pe = struct.unpack_from('<I', data, 0x3c)[0]                    # e_lfanew
    magic = struct.unpack_from('<H', data, pe + 24)[0]
    directory = pe + 24 + PE_DIRECTORIES[magic] + 4 * 8             # directory 4: Certificate Table (8 bytes each)
    data[directory:directory + 8] = struct.pack('<II', len(data), len(table))
    return bytes(data) + table


def sha(data):
    return hashlib.sha256(data).hexdigest()


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def make_payload(folder, extra=None, drop=()):
    manifest = (ROOT / 'app' / 'extension' / 'manifest.json').read_text()
    files = {'extension/manifest.json': manifest, 'extension/background.js': '//bg',
             'helper/native_host.py': '#host', 'helper/helper.py': '#h', 'helper/bmdfs.py': '#fs',
             'bridge/NativeLauncher.cs': '// launcher', 'installer/installer_core.py': '#core',
             'installer/payload_container.py': '#container',
             'README.md': 'readme', 'Uninstall.ps1': '# un', 'tools.lock.json': '{}'}
    files.update(extra or {})
    for name in drop:
        files.pop(name, None)
    path = Path(folder) / 'payload.zip'
    with zipfile.ZipFile(path, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server.owner
        body = srv.routes.get(self.path)
        srv.hits.append(self.path)
        if body is None:
            self.send_error(404)
            return
        start = 0
        header = self.headers.get('Range')
        if header and header.startswith('bytes='):
            start = int(header[6:].split('-')[0])
        if start >= len(body) and start:
            self.send_error(416)
            return
        chunk = body[start:]
        self.send_response(206 if start else 200)
        self.send_header('Content-Length', str(len(chunk)))
        if start:
            self.send_header('Content-Range', f'bytes {start}-{len(body) - 1}/{len(body)}')
        self.end_headers()
        limit = srv.drop.pop(self.path, None)
        if limit is not None:
            self.wfile.write(chunk[:limit])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(chunk)


class Server:
    def __init__(self, routes=None):
        self.routes, self.drop, self.hits = dict(routes or {}), {}, []
        self.httpd = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.owner = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def fake_components(server):
    """Serve fake python/yt-dlp/deno/ffmpeg and return a matching lock."""
    python = zip_bytes({'python.exe': b'PY', 'python312.dll': b'DLL', 'python312._pth': 'python312.zip\n.\n#import site\n'})
    ytdlp = b'YTDLP-EXE' * 1000
    deno = zip_bytes({'deno-x/deno.exe': b'DENO' * 100})
    ffmpeg = zip_bytes({'ffmpeg-7/bin/ffmpeg.exe': b'FFMPEG', 'ffmpeg-7/bin/ffprobe.exe': b'FFPROBE'})
    server.routes.update({'/py.zip': python, '/yt-dlp.exe': ytdlp, '/deno.zip': deno,
                          '/ffmpeg.zip': ffmpeg,
                          '/SHA2-256SUMS': f'{sha(ytdlp)}  yt-dlp.exe\n{"0" * 64}  other.exe\n'.encode(),
                          '/deno.sha256sum': f'{sha(deno)}  deno.zip\n'.encode()})
    b = server.base
    return {'schema': 1, 'components': [
        {'name': 'python', 'version': '3.12.10', 'url': b + '/py.zip', 'kind': 'zip', 'extract_to': 'python',
         'provides': ['python/python.exe'], 'sha256': sha(python)},
        {'name': 'yt-dlp', 'version': 'latest', 'url': b + '/yt-dlp.exe', 'kind': 'exe',
         'provides': ['tools/yt-dlp.exe'], 'checksum': {'url': b + '/SHA2-256SUMS', 'filename': 'yt-dlp.exe'}},
        {'name': 'deno', 'version': 'latest', 'url': b + '/deno.zip', 'kind': 'zip',
         'pick': {'deno.exe': 'tools/deno.exe'}, 'provides': ['tools/deno.exe'],
         'checksum': {'url': b + '/deno.sha256sum', 'filename': 'deno.zip'}},
        {'name': 'ffmpeg', 'version': 'x', 'url': b + '/ffmpeg.zip', 'kind': 'zip',
         'pick': {'ffmpeg.exe': 'tools/ffmpeg.exe', 'ffprobe.exe': 'tools/ffprobe.exe'},
         'provides': ['tools/ffmpeg.exe', 'tools/ffprobe.exe'], 'sha256': sha(ffmpeg)}]}


def tree_snapshot(folder):
    folder = Path(folder)
    if not folder.exists():
        return None
    return {str(p.relative_to(folder)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(folder.rglob('*'))}


EXT_ID = (ROOT / 'extension_id.txt').read_text().strip()
