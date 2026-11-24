"""Select the native installer/service commands before dependencies are installed."""
import argparse
from pathlib import Path
import platform
import shutil
import subprocess


def native_command(action, system=None):
    system = system or platform.system()
    root = Path(__file__).resolve().parent
    scripts = {'install': 'Install-CloudAgentWeb', 'start': 'Start-CloudAgentWeb',
               'stop': 'Stop-CloudAgentWeb', 'install-service': 'Install-UserService'}
    if system == 'Windows':
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            raise ValueError('PowerShell is required on Windows.')
        return [shell, '-NoProfile', '-File', str(root / (scripts[action] + '.ps1'))]
    if system == 'Linux':
        return ['bash', str(root / (scripts[action] + '.sh'))]
    raise ValueError(f'Unsupported host OS: {system}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'start', 'stop', 'install-service'))
    args, native_args = parser.parse_known_args()
    raise SystemExit(subprocess.run([*native_command(args.action), *native_args], shell=False).returncode)


if __name__ == '__main__':
    main()
