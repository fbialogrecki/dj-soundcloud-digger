# Documented audio compatibility — rules 2026-09-05.1

These are manufacturer documentation rules, not physical-device certification.
Firmware dependencies have not been separately established; use the current
firmware for the particular device. USB format, directory depth and navigation
limits are distinct from audio codec compatibility.

| Models | Lossless audio | Sources |
| --- | --- | --- |
| CDJ-350 | WAV/AIFF, 16/24 bit, 44.1/48 kHz | [Manufacturer product page](https://www.pioneerdj.com/en/product/dj-players-turntables/cdj-350/), [manuals](https://www.pioneerdj.com/en/support/documents/player/cdj-350/) |
| CDJ-850 / 850-K | WAV/AIFF, 16/24 bit, 44.1/48 kHz | [Manufacturer announcement](https://jpn.pioneer/ja/corp/news/press/index/1044) |
| CDJ-2000 / 2000NXS | WAV/AIFF, 16/24 bit, 44.1/48 kHz | [CDJ-2000 manuals](https://www.pioneerdj.com/en/support/documents/player/cdj-2000/), [NXS documentation](https://www.pioneerdj.com/en/product/dj-players-turntables/cdj-2000nxs/) |
| CDJ-2000NXS2 | WAV/AIFF/FLAC/ALAC, 16/24 bit, 44.1/48/88.2/96 kHz | [Manufacturer announcement](https://www.pioneerdj.com/pl/news/2016/meet-the-new-cdj-2000nxs2-and-djm-900nxs2/) |
| CDJ-3000 | WAV/AIFF/FLAC/ALAC, 16/24 bit, 44.1/48/88.2/96 kHz | [Manufacturer specifications](https://www.pioneerdj.com/en/news/2020/cdj-3000-professional-dj-multi-player/) |
| CDJ-3000X | WAV/AIFF/FLAC/ALAC, 16/24 bit, 44.1/48/88.2/96 kHz | [Manufacturer manual, supported file formats](https://www.pioneerdj.com/support/manuals/player/cdj-3000x/en/pdf.pdf) |

MP3 is MPEG-1 Layer III at 32–320 kbps. AAC requires LC, 16–320 kbps;
unknown AAC profiles remain unverified. Older listed decks accept 32/44.1/48
kHz compressed audio, while CDJ-3000/3000X list 44.1/48 kHz. The actual-set
compatibility calculation therefore includes unchanged compressed files.

The CDJ-350 manufacturer forum acknowledges a Bandcamp WAV PCM-flag issue even
with nominally supported 24-bit/48 kHz parameters:
[manufacturer response](https://forums.pioneerdj.com/hc/en-us/community/posts/360061678611-E-8305-error-CDJ-350-but-file-types-are-correct).
Export validates RIFF structure and canonicalizes unambiguous PCM instead of
assuming the filename or FFmpeg codec option proves a compatible header.

The app explicitly configures FFmpeg's swr resampler and triangular dither for
actual quantization; it does not require an optional soxr build:
[FFmpeg resampler documentation](https://ffmpeg.org/ffmpeg-resampler.html).

Exclusive file installation uses Windows rename, Linux `RENAME_NOREPLACE` or
macOS `RENAME_EXCL`; unsupported filesystems refuse the operation:
[Linux rename semantics](https://man7.org/linux/man-pages/man2/renameat2.2.html),
[Apple exclusive rename support](https://developer.apple.com/documentation/foundation/urlresourcekey/volumesupportsexclusiverenamingkey?language=objc).
