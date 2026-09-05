"""Versioned manufacturer audio rules, separate from USB filesystem support.

These rules describe documented formats, never a claim of a hardware test.
"""
from dataclasses import dataclass

RULE_VERSION = '2026-09-05.1'
SOURCES = {
    'legacy': 'https://jpn.pioneer/ja/corp/news/press/index/1044',
    'nxs2': 'https://www.pioneerdj.com/pl/news/2016/meet-the-new-cdj-2000nxs2-and-djm-900nxs2/',
    '3000': 'https://www.pioneerdj.com/en/news/2020/cdj-3000-professional-dj-multi-player/',
    '3000x': 'https://downloads.support.alphatheta.com/manuals/dj-players/CDJ-3000X/html/en/000_CDJ-3000X_IM_01_EN_DRI1956-A_en/Product_overview/Product_overview.htm',
}


@dataclass(frozen=True)
class Deck:
    name: str
    high_resolution: bool = False
    mp3_32khz: bool = True

    def accepts(self, media: dict) -> str:
        codec, rate, channels = media.get('codec'), media.get('rate'), media.get('channels')
        if not codec or not rate or not channels:
            return 'unverified'
        if channels not in (1, 2):
            return 'incompatible'
        if codec == 'mp3':
            if rate not in ((32000, 44100, 48000) if self.mp3_32khz else (44100, 48000)):
                return 'incompatible'
            bitrate = media.get('bit_rate', 0)
            return 'compatible' if 32000 <= bitrate <= 320000 else 'unverified'
        if codec == 'aac':
            if rate not in ((32000, 44100, 48000) if self.mp3_32khz else (44100, 48000)):
                return 'incompatible'
            # ffprobe's codec name alone does not distinguish HE-AAC from LC.
            if media.get('profile') != 'LC':
                return 'unverified'
            bitrate = media.get('bit_rate', 0)
            return 'compatible' if 16000 <= bitrate <= 320000 else 'unverified'
        allowed = {'pcm_s16le', 'pcm_s24le', 'pcm_s16be', 'pcm_s24be'}
        if self.high_resolution:
            allowed |= {'flac', 'alac'}
        container = media.get('container')
        if container == 'aiff' and codec not in ('pcm_s16be', 'pcm_s24be'):
            return 'incompatible'
        if container == 'mov' and codec == 'alac' and media.get('extension', '.m4a') != '.m4a':
            return 'unverified'
        if codec not in allowed or media.get('bits') not in (16, 24):
            return 'incompatible'
        rates = (44100, 48000, 88200, 96000) if self.high_resolution else (44100, 48000)
        return 'compatible' if rate in rates else 'incompatible'


DECKS = tuple(Deck(name) for name in ('CDJ-350', 'CDJ-850 / 850-K', 'CDJ-2000', 'CDJ-2000NXS')) + (
    Deck('CDJ-2000NXS2', True), Deck('CDJ-3000', True, False), Deck('CDJ-3000X', True, False))


def compatibility(media_files) -> dict[str, str]:
    results = {}
    for deck in DECKS:
        states = [deck.accepts(media) for media in media_files]
        results[deck.name] = ('incompatible' if 'incompatible' in states else
                              'unverified' if not states or 'unverified' in states else 'compatible')
    return results


@dataclass(frozen=True)
class Profile:
    format: str = 'wav'
    bits: int = 24
    rate: int = 48000

    def __post_init__(self):
        if self.format not in ('wav', 'aiff', 'flac') or self.bits not in (16, 24) or self.rate not in (44100, 48000, 88200, 96000):
            raise ValueError('Unsupported export profile')

    def media(self):
        return dict(container=self.format, codec=('flac' if self.format == 'flac' else f'pcm_s{self.bits}' +
                           ('be' if self.format == 'aiff' else 'le')),
                    bits=self.bits, rate=self.rate, channels=2)
