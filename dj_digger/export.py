"""Reviewable club export plans and recoverable per-file replacement commits."""

import array
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from . import wav
from .decks import RULE_VERSION, Profile, compatibility
from .media import (
    MediaError,
    binary,
    digest,
    fsync_directory,
    input_args,
    install_new,
    pcm_blocks,
    probe,
    run,
    signature,
)
from .models import Cancelled, check_cancelled


@dataclass(frozen=True)
class Item:
    source: str
    signature: str
    sha256: str
    destination: str
    action: str
    bits: int
    rate: int
    metadata_json: str
    reason: str = ''


@dataclass(frozen=True)
class ExportPlan:
    id: str
    items: tuple[Item, ...]
    profile: Profile
    mode: str
    folder: str
    rules: str = RULE_VERSION

    def compatibility(self):
        media = []
        for item in self.items:
            if item.action == 'exception':
                media.append({})
            elif item.action == 'copy':
                media.append(json.loads(item.metadata_json))
            else:
                media.append({**self.profile.media(), 'rate': item.rate, 'bits': item.bits,
                              'codec': ('flac' if self.profile.format == 'flac' else
                                        f'pcm_s{item.bits}' + ('be' if self.profile.format == 'aiff' else 'le'))})
        return compatibility(media)


def portable(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', unicodedata.normalize('NFC', name)).rstrip(' .')
    if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        name = '_' + name
    # Leave ample room for collision suffixes and filesystem byte limits.
    while len(name.encode('utf-8')) > 180:
        name = name[:-1]
    return name or '_'


def target_rate(rate: int, cap: int) -> int:
    if rate not in (44100, 48000, 88200, 96000):
        raise MediaError('Non-standard rate requires an explicit resampling decision')
    if rate <= cap:
        return rate
    family = (48000, 96000) if rate % 48000 == 0 else (44100, 88200)
    choices = [value for value in family if value <= cap]
    if not choices:
        # A 44.1 kHz cap explicitly requests a cross-family reduction.
        return 44100
    return max(choices)


def common_root(paths):
    """Separate Windows drives have no shared ancestor; retain drive labels."""
    try:
        return type(paths[0])(os.path.commonpath([str(path.parent) for path in paths]))
    except ValueError:
        return None


def plan_export(paths, folder: Path, profile=Profile(), *, mode='copy', cancel=None) -> ExportPlan:
    if mode not in ('copy', 'replace'):
        raise ValueError('Unknown export mode')
    operation = uuid.uuid4().hex
    source_paths = tuple(dict.fromkeys(Path(path).expanduser().absolute().parent.resolve(strict=True) / Path(path).name for path in paths))
    if not source_paths:
        raise MediaError('No audio files selected')
    root = common_root(source_paths)
    destination_root = folder.absolute() / f'dj-digger-{operation[:12]}' if mode == 'copy' else (root or folder.absolute())
    items, used, directory_owners = [], set(), {}
    for path in source_paths:
        check_cancelled(cancel)
        before, sha = signature(path), digest(path, cancel)
        if signature(path) != before:
            raise MediaError('Source changed while planning')
        try:
            meta = probe(path, cancel)
        except MediaError as exc:
            items.append(Item(str(path), before, sha, str(destination_root / portable(path.name)), 'exception', 0, 0, '{}', str(exc)))
            continue
        action, reason, bits, rate = 'copy', '', meta['bits'], meta['rate']
        try:
            if meta['channels'] not in (1, 2):
                raise MediaError('Multichannel audio requires an explicit downmix decision')
            if meta['codec'] in ('mp3', 'aac'):
                if not any(value == 'compatible' for value in compatibility([meta]).values()):
                    raise MediaError('Lossy file parameters are incompatible or unverified; no automatic lossy transcode')
            else:
                rate = target_rate(meta['rate'], profile.rate)
                bits = min(meta['bits'] or profile.bits, profile.bits)
                if bits not in (16, 24):
                    bits = profile.bits
                accepted_codecs = {'pcm_s16le', 'pcm_s24le', 'pcm_s16be', 'pcm_s24be'}
                if profile.format == 'flac':
                    accepted_codecs |= {'flac', 'alac'}
                if (meta['codec'] not in accepted_codecs or rate != meta['rate'] or bits != meta['bits']
                        or not any(value == 'compatible' for value in compatibility([meta]).values())):
                    action = 'convert'
                if path.suffix.lower() == '.wav' and action == 'copy':
                    try:
                        if wav.inspect(path)['code'] != 1:
                            action = 'convert'
                    except MediaError:
                        action = 'convert'
            expected_size = round(meta['duration'] * rate) * meta['channels'] * bits // 8 + 1048576
            if (action == 'copy' and path.stat().st_size > 0xffffffff) or (action == 'convert' and profile.format in ('wav', 'aiff') and expected_size > 0xffffffff):
                raise MediaError('Result exceeds the FAT32 / classic container size limit')
            if mode == 'replace' and (path.is_symlink() or path.stat().st_nlink != 1):
                raise MediaError('Replacement is disabled for symbolic and hard links')
            if action == 'convert':
                meta['source_pcm'] = pcm_summary(path, cancel=cancel)
                if meta['source_pcm'][2] > 1:
                    raise MediaError('Source clips; explicit level adjustment is required')
                if rate != meta['rate']:
                    meta['resampled_peak'] = pcm_summary(path, rate=rate, cancel=cancel)[2]
                    if meta['resampled_peak'] > 1:
                        raise MediaError('Resampling would clip; automatic normalization is disabled')
        except MediaError as exc:
            action, reason = 'exception', str(exc)
        relative = path.relative_to(root) if root is not None else Path(portable(path.anchor), *path.parts[1:])
        parts = []
        for index, part in enumerate(relative.parts):
            candidate = portable(part)
            if index < len(relative.parts) - 1:
                key = '/'.join([*parts, candidate]).casefold()
                owner = '/'.join(relative.parts[:index + 1])
                if key in directory_owners and directory_owners[key] != owner:
                    candidate += '-' + hashlib.sha256(owner.encode()).hexdigest()[:10]
                    key = '/'.join([*parts, candidate]).casefold()
                directory_owners[key] = owner
            if index == len(relative.parts) - 1:
                candidate = portable(Path(part).stem) + Path(part).suffix.lower()
            parts.append(candidate)
        target = destination_root.joinpath(*parts) if mode == 'copy' else path
        if action == 'convert':
            target = target.with_suffix('.' + profile.format)
        normalized = unicodedata.normalize('NFC', str(target)).casefold()
        if normalized in used:
            if mode == 'replace':
                action, reason = 'exception', 'Destination conflicts with another selected file'
            else:
                target = target.with_name(f'{target.stem}-{hashlib.sha256(str(path).encode()).hexdigest()[:12]}{target.suffix}')
        used.add(unicodedata.normalize('NFC', str(target)).casefold())
        if mode == 'replace' and target != path and target.exists():
            action, reason = 'exception', 'Destination already exists'
        items.append(Item(str(path), before, sha, str(target), action, bits, rate, json.dumps(meta), reason))
    return ExportPlan(operation, tuple(items), profile, mode, str(destination_root))


def pcm_summary(path: Path, *, rate=None, cancel=None):
    hashed, samples, peak = hashlib.sha256(), 0, 0.0
    for block in pcm_blocks(path, rate=rate, cancel=cancel):
        values = array.array('d')
        values.frombytes(block)
        if sys.byteorder != 'little':
            values.byteswap()
        if any(not math.isfinite(value) for value in values):
            raise MediaError('Non-finite audio samples')
        if values:
            peak = max(peak, max(values), -min(values))
        samples += len(values)
        hashed.update(block)
    if not samples:
        raise MediaError('Empty audio stream')
    return hashed.hexdigest(), samples, peak


def prepare(item: Item, profile: Profile, target: Path, *, cancel=None) -> dict:
    source = Path(item.source)
    metadata = json.loads(item.metadata_json)
    if signature(source) != item.signature or digest(source, cancel) != item.sha256:
        raise MediaError('Source changed since export was reviewed')
    if item.action == 'copy':
        # Exclusive creation; never overwrite an unrelated destination.
        with source.open('rb') as original, target.open('xb') as output:
            hashed = hashlib.sha256()
            while block := original.read(1024 * 1024):
                check_cancelled(cancel)
                hashed.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if hashed.hexdigest() != item.sha256 or digest(target, cancel) != item.sha256:
            raise MediaError('Copy verification failed')
        pcm_summary(target, cancel=cancel)  # full decode, including copied files
        if signature(source) != item.signature:
            raise MediaError('Source changed during copy')
        return {'skipped_metadata': [], 'resampler': None}
    old_hash, old_samples, old_peak = metadata.get('source_pcm') or pcm_summary(source, cancel=cancel)
    if old_peak > 1:
        raise MediaError('Source clips; choose an explicit level adjustment before export')
    quantize = (item.rate != metadata['rate'] or item.bits < metadata['bits']
                or metadata['sample_format'].startswith(('flt', 'dbl')))
    resampler = f'aresample={item.rate}:resampler=swr:dither_method={"triangular" if quantize else "none"}:output_sample_bits={item.bits}'
    if item.rate != metadata['rate']:
        resampled_peak = metadata.get('resampled_peak')
        if resampled_peak is None:
            _, _, resampled_peak = pcm_summary(source, rate=item.rate, cancel=cancel)
        if resampled_peak > 1:
            raise MediaError('Resampling would clip; automatic normalization is disabled')
    codec = 'flac' if profile.format == 'flac' else f'pcm_s{item.bits}' + ('be' if profile.format == 'aiff' else 'le')
    encoded = target.with_name(target.name + '.encoded') if profile.format == 'wav' else target
    safe_tags = {'title', 'artist', 'album', 'date', 'genre', 'track', 'comment', 'copyright'}
    tag_args = [part for key, value in metadata.get('tags', {}).items() if key in safe_tags and len(str(value)) <= 65536 for part in ('-metadata', f'{key}={value}')]
    cover = metadata.get('artwork', [])
    cover_args = (['-map', f'0:{cover[0]["index"]}', '-c:v', 'copy', '-disposition:v', 'attached_pic']
                  if profile.format == 'flac' and len(cover) == 1 and cover[0]['codec'] in ('mjpeg', 'png') else [])
    try:
        run([binary('ffmpeg'), '-v', 'error', '-xerror', '-nostdin', '-n', *input_args(source),
             '-map', '0:a:0', *cover_args, '-map_metadata', '-1', *tag_args, '-af', resampler, '-c:a', codec,
             '-sample_fmt', 's16' if item.bits == 16 else 's32', '-f', profile.format,
             *(['-rf64', 'never'] if profile.format == 'wav' else []), str(encoded)], cancel=cancel,
            timeout=max(300, metadata['duration'] * 5))
        skipped = wav.canonicalise(encoded, target) if profile.format == 'wav' else []
        if target.stat().st_size > 0xffffffff:
            raise MediaError('File exceeds FAT32 file size limit')
        result = probe(target, cancel)
        if (result['bits'], result['rate'], result['channels']) != (item.bits, item.rate, metadata['channels']):
            raise MediaError('Converted audio does not match the reviewed parameters')
        new_hash, new_samples, _ = pcm_summary(target, cancel=cancel)
        expected = round(old_samples / metadata['channels'] * item.rate / metadata['rate']) * metadata['channels']
        if abs(new_samples - expected) > 2 * metadata['channels']:
            raise MediaError('Converted audio length verification failed')
        if not quantize and (old_hash != new_hash or old_samples != new_samples):
            raise MediaError('Lossless conversion changed audio samples')
        with target.open('r+b') as stream:
            os.fsync(stream.fileno())
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))
        if signature(source) != item.signature:
            raise MediaError('Source changed during conversion')
        omitted = [key for key in metadata.get('tags', {}) if key not in result.get('tags', {})]
        if cover and not cover_args:
            omitted.append('artwork')
        return {'skipped_metadata': sorted(set(skipped + omitted)), 'resampler': resampler}
    finally:
        if encoded != target:
            encoded.unlink(missing_ok=True)


def _save_report(path: Path, report):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def _replace_one(db, media_id: str, item: Item, result: Path, *, protected=lambda: (), failpoint=lambda phase: None, operation_id=None):
    """Cancellation is intentionally excluded from this short commit section."""
    source, target = Path(item.source), Path(item.destination)
    if source.resolve() in {Path(path).resolve() for path in protected()}:
        raise MediaError('Stop playback and release its prepared next track before replacement')
    if source.is_symlink() or source.stat().st_nlink != 1 or signature(source) != item.signature or digest(source) != item.sha256:
        raise MediaError('Source changed or has links; replacement refused')
    if target != source and target.exists():
        raise MediaError('Destination already exists')
    operation = operation_id or uuid.uuid4().hex
    original = source.with_name(f'.{source.name}.{operation}.original')
    record = dict(stage='prepared', source=str(source), target=str(target), result=str(result), original=str(original),
                  media_id=media_id, old_hash=item.sha256, new_hash=digest(result))
    db.record_media_operation(operation, record)
    failpoint('prepared')
    install_new(source, original)
    fsync_directory(source.parent)
    record['stage'] = 'original_saved'
    db.record_media_operation(operation, record)
    failpoint('original_saved')
    # The app-wide instance lock excludes another exporter; recheck user-created targets.
    if target.exists():
        raise MediaError('Destination appeared during commit; recovery is required')
    install_new(result, target)
    fsync_directory(target.parent)
    record['stage'] = 'installed'
    db.record_media_operation(operation, record)
    failpoint('installed')
    db.commit_media_replacement(media_id, str(source), str(target), signature(target), probe(target))
    record['stage'] = 'committed'
    db.record_media_operation(operation, record)
    failpoint('committed')
    original.unlink()
    fsync_directory(original.parent)
    record['stage'] = 'done'
    db.record_media_operation(operation, record)
    failpoint('done')



def replace_one(db, media_id, item, result, *, protected=lambda: (), failpoint=lambda phase: None, operation_id=None):
    from .local_audio import LEASE_LOCK, LEASES
    with LEASE_LOCK:
        return _replace_one(db, media_id, item, result, protected=lambda: (*protected(), *LEASES), failpoint=failpoint, operation_id=operation_id)


def recover(db) -> list[str]:
    """Finish only states proved by hashes; preserve all ambiguous files."""
    messages = []
    for operation, record in db.media_operations().items():
        if record.get('kind') == 'copy' or record['stage'] == 'done':
            continue
        source, target, original = (Path(record[key]) for key in ('source', 'target', 'original'))
        try:
            if target.is_file() and digest(target) == record['new_hash'] and original.is_file() and digest(original) == record['old_hash']:
                db.commit_media_replacement(record['media_id'], str(source), str(target), signature(target), probe(target))
                original.unlink()
                fsync_directory(original.parent)
                record['stage'] = 'done'
            elif original.is_file() and digest(original) == record['old_hash'] and not source.exists() and not target.exists():
                install_new(original, source)
                fsync_directory(source.parent)
                record['stage'] = 'done'
            elif source.is_file() and digest(source) == record['old_hash'] and not original.exists() and record['stage'] in ('preparing', 'prepared'):
                record['stage'] = 'done'
            elif record['stage'] == 'committed' and not original.exists() and target.is_file() and digest(target) == record['new_hash']:
                record['stage'] = 'done'
            else:
                messages.append(f'{source}: ambiguous recovery; files preserved')
                continue
            db.record_media_operation(operation, record)
        except Exception as exc:
            messages.append(f'{source}: {exc}')
    return messages


def execute(plan: ExportPlan, db, *, resume=False, cancel=None, protected=lambda: (), progress=lambda done, total: None):
    if plan.rules != RULE_VERSION:
        raise MediaError('Export rules changed; prepare a new plan')
    report = {'plan': asdict(plan), 'status': 'running', 'results': [], 'compatibility': plan.compatibility()}
    folder = Path(plan.folder)
    completed = {}
    if plan.mode == 'copy':
        if resume:
            previous = db.media_operations().get(plan.id, {})
            if previous.get('kind') != 'copy' or json.dumps(previous.get('plan'), sort_keys=True) != json.dumps(asdict(plan), sort_keys=True):
                raise MediaError('No matching trusted journal for this export')
            completed = {result['source']: result for result in previous.get('results', []) if result.get('status') == 'complete'}
            if not folder.is_dir():
                raise MediaError('Export folder is unavailable; reconnect its volume')
        else:
            folder.mkdir(parents=True, exist_ok=False)
        db.record_media_operation(plan.id, {**report, 'kind': 'copy', 'stage': 'copy'})
    for index, item in enumerate(plan.items):
        temporary = None
        working_directory = None
        commit_started = False
        try:
            check_cancelled(cancel)
            if item.source in completed:
                previous = completed[item.source]
                if digest(Path(item.destination), cancel) != previous.get('sha256'):
                    raise MediaError('Previously exported file changed; preserved for inspection')
                report['results'].append(previous)
                continue
            if item.action == 'exception':
                raise MediaError(item.reason)
            target = Path(item.destination)
            if plan.mode == 'replace' and item.action == 'copy':
                if signature(Path(item.source)) != item.signature or digest(Path(item.source), cancel) != item.sha256:
                    raise MediaError('Source changed since review')
                pcm_summary(Path(item.source), cancel=cancel)
                report['results'].append({'source': item.source, 'status': 'unchanged'})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            working_directory = Path(tempfile.mkdtemp(prefix=f'.dj-digger-{plan.id}-', dir=target.parent))
            temporary = working_directory / target.name
            operation_id = uuid.uuid4().hex
            if plan.mode == 'replace':
                record = db.register_media(str(Path(item.source).resolve()), item.signature)
                db.record_media_operation(operation_id, dict(stage='preparing', source=item.source, target=item.destination,
                    result=str(temporary), original=str(Path(item.source).with_name(f'.{Path(item.source).name}.{operation_id}.original')),
                    media_id=record['id'], old_hash=item.sha256, new_hash=''))
            details = prepare(item, plan.profile, temporary, cancel=cancel)
            check_cancelled(cancel)
            if plan.mode == 'replace':
                commit_started = True
                replace_one(db, record['id'], item, temporary, protected=protected, operation_id=operation_id)
            else:
                if target.exists():
                    raise MediaError('Destination already exists')
                install_new(temporary, target)
                fsync_directory(target.parent)
                source_record = db.register_media(str(Path(item.source).resolve()), item.signature)
                db.register_media(str(target), signature(target), parent_id=source_record['id'])
            report['results'].append({'source': item.source, 'status': 'complete', 'sha256': digest(target), **details})
        except Cancelled:
            report['status'] = 'cancelled'
            break
        except Exception as exc:
            report['results'].append({'source': item.source, 'status': 'failed', 'error': str(exc)})
        finally:
            # A replacement journal may own this path after a failed commit.
            if working_directory and (not commit_started or not temporary.exists()):
                shutil.rmtree(working_directory)
            progress(index + 1, len(plan.items))
            if plan.mode == 'copy':
                _save_report(folder / 'dj-digger-report.json', report)
                db.record_media_operation(plan.id, {**report, 'kind': 'copy', 'stage': 'copy'})
    if report['status'] != 'cancelled':
        report['status'] = 'partial' if any(item['status'] == 'failed' for item in report['results']) else 'complete'
    report['missing'] = [item.source for item in plan.items if item.source not in {result['source'] for result in report['results'] if result['status'] in ('complete', 'unchanged')}]
    if plan.mode == 'copy':
        _save_report(folder / 'dj-digger-report.json', report)
        db.record_media_operation(plan.id, {**report, 'kind': 'copy', 'stage': 'done' if report['status'] == 'complete' else 'copy'})
    return report


def resume_plan(record):
    """Only call with records loaded from the application's own operation table."""
    raw = record['plan']
    return ExportPlan(raw['id'], tuple(Item(**item) for item in raw['items']), Profile(**raw['profile']),
                      raw['mode'], raw['folder'], raw['rules'])
