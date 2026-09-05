"""Strict RIFF PCM inspection and sample-preserving canonicalisation."""
import os
import struct
from pathlib import Path

from .media import MediaError

PCM_GUID = bytes.fromhex('0100000000001000800000aa00389b71')
MAX_RIFF = 0xffffffff


def inspect(path: Path) -> dict:
    size = path.stat().st_size
    with path.open('rb') as source:
        header = source.read(12)
        if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WAVE':
            raise MediaError('Only RIFF/WAVE is supported; RF64 is not a club export')
        if struct.unpack('<I', header[4:8])[0] + 8 != size:
            raise MediaError('Invalid RIFF size')
        fmt, data, skipped, info_chunks = None, None, [], []
        while source.tell() < size:
            chunk = source.read(8)
            if len(chunk) != 8:
                raise MediaError('Truncated RIFF chunk')
            tag, count = struct.unpack('<4sI', chunk)
            start = source.tell()
            if start + count + (count & 1) > size:
                raise MediaError('RIFF chunk outside file')
            if tag == b'fmt ':
                if fmt is not None or count < 16 or count > 4096:
                    raise MediaError('Invalid or repeated WAV fmt chunk')
                raw = source.read(count)
                code, channels, rate, byte_rate, align, bits = struct.unpack('<HHIIHH', raw[:16])
                if code == 0xfffe:
                    if count < 40 or struct.unpack('<H', raw[16:18])[0] != 22 or raw[24:40] != PCM_GUID:
                        raise MediaError('Unsupported EXTENSIBLE PCM subtype')
                    valid, mask = struct.unpack('<HI', raw[18:24])
                    if valid != bits or mask not in (0, 3 if channels == 2 else 4):
                        raise MediaError('Ambiguous valid bits or channel layout')
                elif code != 1:
                    raise MediaError('WAV is not integer PCM')
                if channels not in (1, 2) or bits not in (16, 24) or align != channels * bits // 8 or byte_rate != rate * align:
                    raise MediaError('Unsupported PCM layout')
                fmt = dict(code=code, channels=channels, rate=rate, bits=bits, align=align)
            elif tag == b'data':
                if data is not None:
                    raise MediaError('Repeated WAV data chunk')
                data = (start, count)
            elif tag == b'LIST' and count <= 1024 * 1024:
                raw = source.read(count)
                if raw[:4] == b'INFO':
                    position = 4
                    safe = bytearray(b'INFO')
                    while position + 8 <= len(raw):
                        name, length = struct.unpack('<4sI', raw[position:position + 8])
                        end = position + 8 + length + (length & 1)
                        if end > len(raw):
                            raise MediaError('Malformed WAV INFO metadata')
                        if name in (b'IART', b'INAM', b'IPRD', b'ICRD', b'IGNR', b'ITRK', b'ICMT', b'ICOP', b'ISFT'):
                            safe.extend(raw[position:end])
                        else:
                            skipped.append(name.decode('ascii', errors='replace'))
                        position = end
                    if position != len(raw):
                        raise MediaError('Malformed WAV INFO metadata')
                    info_chunks.append(bytes(safe))
                else:
                    skipped.append('LIST')
            else:
                skipped.append(tag.decode('ascii', errors='replace'))
            source.seek(start + count + (count & 1))
        if not fmt or data is None or data[1] % fmt['align']:
            raise MediaError('Missing or misaligned PCM data')
        return {**fmt, 'offset': data[0], 'size': data[1], 'frames': data[1] // fmt['align'], 'skipped_chunks': skipped, 'info_chunks': info_chunks}


def canonicalise(source: Path, target: Path) -> list[str]:
    info = inspect(source)
    count = info['size']
    extra = sum(8 + len(chunk) + (len(chunk) & 1) for chunk in info['info_chunks'])
    if count + (count & 1) + 36 + extra > MAX_RIFF:
        raise MediaError('Audio exceeds classic RIFF size limit')
    with source.open('rb') as original, target.open('xb') as output:
        output.write(struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + count + (count & 1) + extra, b'WAVE', b'fmt ', 16,
                                 1, info['channels'], info['rate'], info['rate'] * info['align'],
                                 info['align'], info['bits'], b'data', count))
        original.seek(info['offset'])
        remaining = count
        while remaining:
            block = original.read(min(1024 * 1024, remaining))
            if not block:
                raise MediaError('Source changed during WAV repair')
            output.write(block)
            remaining -= len(block)
        if count & 1:
            output.write(b'\0')
        for chunk in info['info_chunks']:
            output.write(struct.pack('<4sI', b'LIST', len(chunk)) + chunk + (b'\0' if len(chunk) & 1 else b''))
        output.flush()
        os.fsync(output.fileno())
    inspect(target)
    return info['skipped_chunks']
