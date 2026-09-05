"""Evaluate a human-labelled, licensed corpus; never infer or manufacture labels.

CSV columns: path,bpm,key,annotator,license. Paths are relative to --audio-root.
Outputs include strict tempo accuracy, octave-tolerant accuracy, key accuracy,
abstentions and every prediction. This is separate from synthetic unit tests.
"""
import argparse
import csv
import json
from pathlib import Path

from dj_digger.analysis import analyze_spawned


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('labels', type=Path)
    parser.add_argument('--audio-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.audio_root.resolve(strict=True)
    with args.labels.open(newline='', encoding='utf-8') as source:
        labels = list(csv.DictReader(source))
    if not labels:
        parser.error('A non-empty human-labelled corpus is required')
    results = []
    for label in labels:
        if not label.get('annotator') or not label.get('license'):
            parser.error('Every track needs an annotator and a license/reference')
        path = (root / label['path']).resolve(strict=True)
        if not path.is_relative_to(root):
            parser.error('Audio path escapes --audio-root')
        expected = float(label['bpm']) if label.get('bpm') else None
        result = analyze_spawned(path)
        actual = result.get('bpm')
        strict = bool(actual and expected and abs(actual - expected) / expected <= .01)
        octave = bool(actual and expected and min(abs(actual * factor - expected) / expected for factor in (.5, 1, 2)) <= .01)
        results.append(dict(path=label['path'], expected_bpm=expected, expected_key=label.get('key'),
                            prediction=result, strict_tempo=strict, octave_tempo=octave,
                            correct_key=bool(label.get('key') and result.get('key') == label['key'])))
    tempo_count = sum(row['expected_bpm'] is not None for row in results)
    key_count = sum(bool(row['expected_key']) for row in results)
    report = dict(tracks=len(results), tempo_labels=tempo_count, key_labels=key_count,
                  strict_tempo_accuracy=sum(row['strict_tempo'] for row in results) / tempo_count if tempo_count else None,
                  octave_tempo_accuracy=sum(row['octave_tempo'] for row in results) / tempo_count if tempo_count else None,
                  key_accuracy=sum(row['correct_key'] for row in results) / key_count if key_count else None,
                  tempo_abstentions=sum(row['prediction']['bpm'] is None for row in results),
                  key_abstentions=sum(not row['prediction']['key'] for row in results), results=results)
    with args.output.open('x', encoding='utf-8') as destination:
        json.dump(report, destination, ensure_ascii=False, indent=2)
    print(f'Evaluated {len(results)} labelled tracks; report: {args.output}')


if __name__ == '__main__':
    main()
