"""Exercise actual pip, pipx and uv-tool uninstall/reinstall in temporary homes."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('legacy', type=Path)
    parser.add_argument('new', type=Path)
    parser.add_argument('--manager', choices=('pip', 'pipx', 'uv'), required=True)
    options = parser.parse_args()
    old, new = str(options.legacy.resolve()), str(options.new.resolve())
    with tempfile.TemporaryDirectory(prefix='dj-digger-migrate-') as temporary:
        root = Path(temporary)
        env = dict(os.environ, XDG_DATA_HOME=str(root / 'data'), XDG_CONFIG_HOME=str(root / 'config'),
                   PIPX_HOME=str(root / 'pipx'), PIPX_BIN_DIR=str(root / 'bin'),
                   PIPX_MAN_DIR=str(root / 'man'), UV_TOOL_DIR=str(root / 'tools'), UV_TOOL_BIN_DIR=str(root / 'bin'),
                   UV_LINK_MODE='copy')
        sentinel = root / 'data' / 'dj-digger' / 'preserve.txt'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text('library stays')
        if options.manager == 'pip':
            subprocess.run(['uv', 'venv', '--seed', '--python', sys.executable, str(root / 'venv')], check=True, env=env)
            python = root / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            commands = [[str(python), '-m', 'pip', 'install', old],
                        [str(python), '-m', 'pip', 'uninstall', '-y', 'dj-soundcloud-digger'],
                        [str(python), '-m', 'pip', 'install', new],
                        [str(python), '-m', 'dj_digger', '--version']]
        elif options.manager == 'pipx':
            prefix = [shutil.which('pipx')] if shutil.which('pipx') else ['uvx', '--from', 'pipx', 'pipx']
            commands = [prefix + ['install', '--python', sys.executable, old],
                        prefix + ['uninstall', 'dj-soundcloud-digger'],
                        prefix + ['install', '--python', sys.executable, new],
                        [str(root / 'bin' / ('dj-digger.exe' if os.name == 'nt' else 'dj-digger')), '--version']]
        else:
            commands = [['uv', 'tool', 'install', '--python', sys.executable, old],
                        ['uv', 'tool', 'uninstall', 'dj-soundcloud-digger'],
                        ['uv', 'tool', 'install', '--python', sys.executable, new],
                        [str(root / 'bin' / ('dj-digger.exe' if os.name == 'nt' else 'dj-digger')), '--version']]
        for command in commands:
            subprocess.run(command, check=True, cwd=root, env=env)
        assert sentinel.read_text() == 'library stays'


if __name__ == '__main__':
    main()
