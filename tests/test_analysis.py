"""Algorithm invariants; these do not claim accuracy on real DJ repertoire."""
import importlib.util
import shutil
import subprocess

import pytest

from dj_digger.analysis import analyze_file, analyze_spawned, camelot

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or importlib.util.find_spec('librosa') is None,
                                reason='FFmpeg and analyze extra required')


def make_audio(path, filter):
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', filter, '-t', '5',
                    '-c:a', 'pcm_s16le', str(path)], check=True)
    return path


def test_silence_abstains_and_spawn_returns(tmp_path):
    path = make_audio(tmp_path / 'silence.wav', 'anullsrc=r=22050:cl=stereo')
    result = analyze_spawned(path)
    assert result['bpm'] is None and result['key'] == ''
    assert result['estimated']


def test_antiphase_stereo_and_arbitrary_block_boundaries(tmp_path, monkeypatch):
    import numpy as np

    from dj_digger import analysis
    from dj_digger.media import pcm_blocks
    path = make_audio(tmp_path / 'chord.wav', 'aevalsrc=0.1*(sin(2*PI*261.626*t)+sin(2*PI*329.628*t)+sin(2*PI*391.995*t)):s=22050')
    blocks = b''.join(pcm_blocks(path, rate=analysis.RATE))
    mono = np.frombuffer(blocks, dtype='<f8')
    stereo_path = make_audio(tmp_path / 'stereo.wav', 'anullsrc=r=22050:cl=stereo')

    def run(chunk_frames):
        values = np.column_stack([mono, -mono]).astype('<f8').tobytes()
        monkeypatch.setattr(analysis, 'pcm_blocks', lambda *args, **kwargs: (values[index:index + chunk_frames * 16] for index in range(0, len(values), chunk_frames * 16)))
        return analyze_file(str(stereo_path))

    reference, streaming = run(len(mono)), run(731)
    # A single triad is not a labelled musical key. Check stream and channel invariance.
    assert reference['key'] == streaming['key']
    assert reference['key']
    assert reference['bpm'] == streaming['bpm']
    assert camelot('C') == '8B' and camelot('Am') == '8A'
