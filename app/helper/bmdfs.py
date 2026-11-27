"""Filesystem safety primitives shared by the helper, installer and uninstaller.

Everything that deletes or replaces files goes through this module so that the
deletion boundaries are enforced in exactly one place.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path


class BoundaryError(Exception):
    """Raised when an operation would leave its permitted directory."""


def _norm(path):
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def is_link(path):
    """True for symlinks and (on Windows) directory junctions."""
    p = os.fspath(path)
    if os.path.islink(p):
        return True
    junction = getattr(os, 'isjunction', None)
    return bool(junction and junction(p))


def is_within(path, boundary, allow_equal=False):
    """True when the *resolved* path is inside the resolved boundary."""
    child, root = _norm(path), _norm(boundary)
    if child == root:
        return allow_equal
    try:
        return os.path.commonpath([child, root]) == root
    except ValueError:  # different drives
        return False


def require_within(path, boundary, allow_equal=False):
    if not is_within(path, boundary, allow_equal):
        raise BoundaryError(f'Refusing to touch {path}: outside {boundary}')


def _remove_link(path):
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError, OSError):
        os.rmdir(path)  # directory junction


def safe_unlink(path, boundary):
    """Delete one file (or link) that lives strictly inside boundary."""
    p = Path(path)
    if is_link(p):
        # A link is removed as a link and never followed; only its parent
        # directory has to be inside the boundary.
        require_within(p.parent, boundary, allow_equal=True)
        _remove_link(p)
        return True
    require_within(p, boundary)
    if p.is_file():
        _make_writable(p)
        p.unlink()
        return True
    return False


def safe_rmtree(path, boundary):
    """Delete a directory tree that lives strictly inside boundary.

    Links and junctions inside the tree are unlinked, never followed.
    Returns the number of filesystem entries removed.
    """
    p = Path(path)
    if not os.path.lexists(p):
        return 0
    if is_link(p):
        require_within(p.parent, boundary, allow_equal=True)
        _remove_link(p)
        return 1
    require_within(p, boundary)
    removed = 0
    for current, dirs, files in os.walk(p, topdown=False, followlinks=False):
        for name in files:
            target = os.path.join(current, name)
            _make_writable(target)
            os.unlink(target)
            removed += 1
        for name in dirs:
            target = os.path.join(current, name)
            if is_link(target):
                _remove_link(target)
            else:
                os.rmdir(target)
            removed += 1
    os.rmdir(p)
    return removed + 1


def _make_writable(path):
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass


def fsync_file(path):
    with open(path, 'rb+') as handle:
        handle.flush()
        os.fsync(handle.fileno())


def write_json_atomic(path, value):
    """Write JSON so readers see the old or the new file, never a torn one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    # utf-8-sig: a byte-order mark (Notepad, Windows PowerShell's Set-Content -Encoding UTF8) is
    # not a reason to ignore a file.
    try:
        with open(path, 'r', encoding='utf-8-sig') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def commit_new_file(source, destination):
    """Move source to destination atomically and never overwrite destination.

    Raises FileExistsError when destination already exists. Prefers a hard link
    (atomic on NTFS/ext4); falls back to rename, which on Windows refuses to
    replace an existing file. The source name is removed afterwards.
    """
    source, destination = os.fspath(source), os.fspath(destination)
    try:
        os.link(source, destination)
    except FileExistsError:
        raise
    except (OSError, NotImplementedError, AttributeError):
        # Hard links unsupported (FAT/exFAT/network share). On Windows rename
        # fails if the target exists; elsewhere reserve the name first.
        if os.name != 'nt':
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        os.replace(source, destination) if os.name != 'nt' else os.rename(source, destination)
        return destination
    try:
        os.unlink(source)
    except OSError:
        pass
    return destination


def pid_alive(pid):
    """True when a process with this id exists (never signals it)."""
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def dir_size(path, limit=10 ** 12):
    total = 0
    for current, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                pass
            if total > limit:
                return total
    return total
