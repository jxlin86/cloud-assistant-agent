"""Install a Linux user unit, running as the account invoking this module."""
import os
from pathlib import Path, PurePosixPath
import sys
import subprocess
import tempfile

from .__main__ import parse_args
from .runtime import host_os


def quote(value):
    value = str(value)
    if any(char in value for char in '\r\n\x00'):
        raise ValueError('Service settings cannot contain newlines or NUL.')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render_unit(python, config, home, host, port, allowed_hosts):
    root = PurePosixPath(config).parent
    if not root.is_absolute() or any(c in str(root) for c in '\r\n\x00'):
        raise ValueError('The service working directory must be an absolute path without control characters.')
    argv = [str(python), '-m', 'cloud_agent_assistant_web', '--config', str(config),
            '--host', host, '--port', str(port)]
    for allowed in allowed_hosts:
        argv.extend(['--allowed-host', allowed])
    search_path = ':'.join(map(str, [PurePosixPath(python).parent, PurePosixPath(home)/'.venvs/cloud-agent-oci/bin',
                                    PurePosixPath(home)/'.local/bin', PurePosixPath(home)/'bin',
                                    '/usr/local/bin', '/usr/bin', '/bin']))
    return '\n'.join([
        '[Unit]', 'Description=Cloud Assistant Agent (user service)', '', '[Service]',
        # Unlike ExecStart/Environment, systemd's WorkingDirectory parser does
        # not remove quotes. The entire RHS is the path, including spaces.
        'Type=simple', 'WorkingDirectory=' + str(root).replace('%', '%%'),
        'Environment=' + quote('HOME=' + str(home)), 'Environment=' + quote('PATH=' + search_path),
        'Environment=PYTHONUNBUFFERED=1', 'Environment=PYTHONUTF8=1',
        'Environment=OPENAI_AGENTS_DISABLE_TRACING=1',
        'ExecStart=' + ' '.join(map(quote, argv)), 'Restart=on-failure', 'RestartSec=5',
        'TimeoutStopSec=30', 'UMask=0077', 'NoNewPrivileges=true', '',
        '[Install]', 'WantedBy=default.target', ''])


def main(argv=None):
    if host_os() != 'Linux':
        raise ValueError('Use Install-UserService.ps1 on Windows.')
    if os.geteuid() == 0:
        raise ValueError('Run as opc or your application account, without sudo.')
    args = parse_args(argv)
    config = args.config.resolve()
    if not config.is_file():
        raise ValueError(f'Configuration not found: {config}')
    directory = Path(os.getenv('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'systemd/user'
    directory.mkdir(parents=True, exist_ok=True)
    unit = directory / 'cloud-assistant-agent.service'
    content = render_unit(sys.executable, config, Path.home(), args.host, args.port, args.allowed_hosts)
    # Validate before replacing an existing unit; the temporary directory belongs
    # to this operation and is the only directory cleaned by the context manager.
    with tempfile.TemporaryDirectory(prefix='cloud-agent-unit-', dir=directory) as staging:
        candidate = Path(staging)/unit.name
        candidate.write_text(content, encoding='utf-8')
        checked = subprocess.run(['systemd-analyze', '--user', 'verify', str(candidate)],
                                 capture_output=True, text=True)
        if checked.returncode:
            raise ValueError('systemd rejected the generated unit:\n' + checked.stdout + checked.stderr)
        candidate.replace(unit)
    print(f'Installed user unit: {unit}')


if __name__ == '__main__':
    main()
