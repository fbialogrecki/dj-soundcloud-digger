"""Build/install smoke checks using an isolated venv and temporary user data."""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('wheel', type=Path, nargs='?')
    parser.add_argument('--legacy-wheel', type=Path)
    options = parser.parse_args()
    if options.wheel is None:
        project = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads((project / 'pyproject.toml').read_text())['project']
        name = re.sub(r'[-_.]+', '_', metadata['name']).lower()
        options.wheel = project / 'dist' / f"{name}-{metadata['version']}-py3-none-any.whl"
    wheel = options.wheel.resolve()
    with tempfile.TemporaryDirectory(prefix='dj-digger-install-') as directory:
        root = Path(directory)
        environment = root / 'venv'
        subprocess.run(['uv', 'venv', '--python', sys.executable, str(environment)], check=True)
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        env = dict(os.environ, XDG_DATA_HOME=str(root / 'data'), XDG_CONFIG_HOME=str(root / 'config'))
        sentinel = root / 'data' / 'dj-digger' / 'preserved.txt'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text('user data stays')
        if options.legacy_wheel:
            subprocess.run(['uv', 'pip', 'install', '--python', str(python), str(options.legacy_wheel.resolve())], check=True, env=env)
            subprocess.run(['uv', 'pip', 'uninstall', '--python', str(python), 'dj-soundcloud-digger'], check=True, env=env)
        subprocess.run(['uv', 'pip', 'install', '--python', str(python), str(wheel)], check=True, env=env)
        subprocess.run([str(python), '-c', 'import dj_digger.cli; import dj_digger.tui; import dj_digger.analysis'], check=True, cwd=root, env=env)
        subprocess.run([str(python), '-m', 'dj_digger', '--version'], check=True, cwd=root, env=env)
        subprocess.run([str(python), '-m', 'dj_digger', '--help'], check=True, cwd=root, env=env, stdout=subprocess.DEVNULL)
        assert sentinel.read_text() == 'user data stays'


if __name__ == '__main__':
    main()
