"""Local media inspection. No decoder runs on the UI or audio callback thread."""

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .media_processes import register, unregister
from .models import check_cancelled

FORMATS = {'.wav': 'wav', '.aif': 'aiff', '.aiff': 'aiff', '.mp3': 'mp3',
           '.flac': 'flac', '.fla': 'flac', '.m4a': 'mov', '.aac': 'aac', '.mp4': 'mov'}


class MediaError(RuntimeError):
    pass


def signature(path: Path) -> str:
    stat = path.stat()
    if not path.is_file():
        raise MediaError('Select a regular audio file')
    return json.dumps([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])


def digest(path: Path, cancel=None) -> str:
    value = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            check_cancelled(cancel)
            value.update(chunk)
    return value.hexdigest()


def binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise MediaError(f'{name} is required for this action; install FFmpeg')
    return found


def input_args(path: Path) -> list[str]:
    fmt = FORMATS.get(path.suffix.lower())
    if fmt is None:
        raise MediaError('Unsupported local audio container')
    # An explicit demuxer excludes HLS/concat/other playlist formats. A file
    # masquerading as an audio file cannot make the decoder access the network.
    return ['-protocol_whitelist', 'file,pipe', '-f', fmt, '-i', str(path.absolute())]


def run(args: list[str], *, cancel=None, timeout=300, output_limit=2 * 1024 * 1024) -> bytes:
    """Bounded output, cooperative cancellation and unconditional process reaping."""
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=os.name != "nt" and os.environ.get("DJ_DIGGER_ANALYSIS_CHILD") != "1")
    register(process)
    output, errors = bytearray(), bytearray()
    overflow = threading.Event()

    def drain(pipe, target, limit, fail):
        while chunk := pipe.read(65536):
            if len(target) + len(chunk) > limit:
                if fail:
                    overflow.set()
                target.extend(chunk[:max(0, limit - len(target))])
            else:
                target.extend(chunk)

    readers = [threading.Thread(target=drain, args=(process.stdout, output, output_limit, True)),
               threading.Thread(target=drain, args=(process.stderr, errors, 65536, False))]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            check_cancelled(cancel)
            if overflow.is_set() or time.monotonic() > deadline:
                raise MediaError('Decoder output limit or timeout exceeded')
            time.sleep(.025)
        for reader in readers:
            reader.join()
        check_cancelled(cancel)
        if process.returncode or overflow.is_set():
            raise MediaError(errors.decode(errors='replace')[-2000:] or 'Decoder failed')
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        unregister(process)
        for reader in readers:
            reader.join()
        process.stdout.close()
        process.stderr.close()


def probe(path: Path, cancel=None) -> dict:
    before = signature(path)
    payload = json.loads(run([binary('ffprobe'), '-v', 'error', *input_args(path),
                             '-show_streams', '-show_format', '-of', 'json'], cancel=cancel, timeout=30))
    audio = [stream for stream in payload.get('streams', []) if stream.get('codec_type') == 'audio']
    if len(audio) != 1:
        raise MediaError('Exactly one audio stream is required')
    stream = audio[0]
    tags = {key.lower(): value for key, value in
            {**payload.get('format', {}).get('tags', {}), **stream.get('tags', {})}.items()}
    result = {'container': FORMATS[path.suffix.lower()], 'extension': path.suffix.lower(), 'artwork': [dict(index=s['index'], codec=s.get('codec_name')) for s in payload.get('streams', []) if s.get('disposition', {}).get('attached_pic')], 'codec': stream.get('codec_name', ''), 'rate': int(stream.get('sample_rate') or 0),
              'channels': int(stream.get('channels') or 0),
              'bits': int(stream.get('bits_per_raw_sample') or stream.get('bits_per_sample') or 0),
              'sample_format': stream.get('sample_fmt', ''), 'profile': stream.get('profile', ''), 'index': stream['index'],
              'bit_rate': int(stream.get('bit_rate') or 0),
              'duration': float(stream.get('duration') or payload.get('format', {}).get('duration') or 0),
              'tags': tags, 'signature': before}
    if signature(path) != before:
        raise MediaError('File changed during inspection')
    return result


def fsync_directory(path: Path) -> None:
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def pcm_blocks(path: Path, *, rate=None, sample_format='f64le', filters=None, channels=None, seek=0, cancel=None):
    """Decode continuously, with a bounded queue and interruptible pipe reads."""
    import queue

    args = [binary('ffmpeg'), '-v', 'error', '-xerror', '-nostdin', *(['-ss', str(seek)] if seek else []), *input_args(path), '-map', '0:a:0']
    if channels:
        args += ['-ac', str(channels)]
    if rate:
        args += ['-ar', str(rate)]
    if filters:
        args += ['-af', filters]
    args += ['-f', sample_format, '-']
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=os.name != "nt" and os.environ.get("DJ_DIGGER_ANALYSIS_CHILD") != "1")
    register(process)
    chunks = queue.Queue(maxsize=8)
    stopped = threading.Event()
    errors = bytearray()

    def produce():
        try:
            while not stopped.is_set():
                chunk = process.stdout.read(65536)
                while not stopped.is_set():
                    try:
                        chunks.put(chunk, timeout=.1)
                        break
                    except queue.Full:
                        pass
                if not chunk:
                    return
        finally:
            process.stdout.close()

    def drain():
        while chunk := process.stderr.read(4096):
            errors.extend(chunk)
            del errors[:-65536]
        process.stderr.close()

    readers = [threading.Thread(target=produce), threading.Thread(target=drain)]
    for reader in readers:
        reader.start()
    last = time.monotonic()
    try:
        while True:
            check_cancelled(cancel)
            try:
                chunk = chunks.get(timeout=.1)
            except queue.Empty:
                if time.monotonic() - last > 30:
                    raise MediaError('Decoder stalled')
                continue
            last = time.monotonic()
            if not chunk:
                break
            yield chunk
        process.wait(timeout=5)
        readers[1].join()
        if process.returncode:
            raise MediaError(errors.decode(errors='replace')[-2000:] or 'Decoder failed')
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        unregister(process)
        for reader in readers:
            reader.join()


def install_new(source: Path, target: Path) -> None:
    """Atomic rename that refuses to replace a target created concurrently."""
    if os.name == 'nt':
        os.rename(source, target)  # Windows rename refuses an existing target.
        return
    import ctypes
    import errno
    import sys

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    elif hasattr(libc, 'renameat2'):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
    else:
        raise MediaError('This platform cannot guarantee exclusive file installation')
    if result:
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise MediaError('This filesystem does not support exclusive rename; original preserved')
        raise OSError(error, os.strerror(error), str(target))
