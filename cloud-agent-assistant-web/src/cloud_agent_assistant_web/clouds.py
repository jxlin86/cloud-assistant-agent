"""Provider context and shell-free CLI launchers; model hosting stays independent."""
from __future__ import annotations

import configparser
import os
from pathlib import Path
import re
import shutil
import sys
from .runtime import host_os, find_cli

LABELS = {'oci': 'OCI', 'azure': 'Azure', 'gcp': 'Google Cloud', 'aws': 'AWS'}
CLI_NAMES = {'oci': 'oci', 'azure': 'az', 'gcp': 'gcloud', 'aws': 'aws'}


def cli_launcher(cloud, oci_cli='oci'):
    name = CLI_NAMES[cloud]
    executable = oci_cli if cloud == 'oci' and Path(oci_cli).is_file() else find_cli(name)
    if not executable:
        return None
    path = Path(executable)
    windows = host_os() == 'Windows'
    if path.suffix.lower() == '.ps1':
        return None
    if not windows and (path.suffix.lower() in {'.cmd', '.bat', '.exe'} or not os.access(path, os.X_OK)):
        return None
    if not windows or path.suffix.lower() not in {'.cmd', '.bat'}:
        return [str(path)]
    # Do not pass model-controlled argv through Windows batch scripts.
    if cloud == 'azure':
        for python in (path.parent.parent / 'python.exe', path.parent / 'python.exe'):
            if python.is_file():
                return [str(python), '-I', '-m', 'azure.cli']
    if cloud == 'oci':
        for python in (path.parent / 'python.exe', path.parent.parent / 'python.exe'):
            if python.is_file():
                return [str(python), '-I', '-m', 'oci_cli.cli']
    if cloud == 'gcp':
        script = path.parent / 'gcloud.py'
        python = path.parent.parent / 'platform' / 'bundledpython' / 'python.exe'
        if script.is_file():
            return [str(python) if python.is_file() else sys.executable, str(script)]
    return None


def local_profiles(cloud, settings):
    if cloud == 'oci':
        return list(settings.profiles())
    if cloud == 'aws':
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(Path(os.getenv('AWS_CONFIG_FILE', '~/.aws/config')).expanduser(), encoding='utf-8-sig')
            return sorted({'default', *(name[8:] for name in parser.sections() if name.startswith('profile '))})
        except (OSError, configparser.Error):
            return ['default']
    if cloud == 'gcp':
        root = Path(os.getenv('CLOUDSDK_CONFIG', str(Path(os.getenv('APPDATA', str(Path.home() / '.config'))) / 'gcloud')))
        return sorted({'default', *(path.name[7:] for path in (root / 'configurations').glob('config_*'))})
    return ['default']


def providers(settings):
    defaults = {
        'oci': dict(profile=settings.profile, region=settings.region, scope=settings.compartment_id),
        'aws': dict(profile='default', region='us-east-1', scope=''),
        'azure': dict(profile='default', region='eastus', scope=''),
        'gcp': dict(profile='default', region='us-central1', scope=''),
    }
    return {cloud: dict(label=LABELS[cloud], cli=CLI_NAMES[cloud],
                        available=cli_launcher(cloud, settings.cli) is not None,
                        profiles=local_profiles(cloud, settings), defaults=defaults[cloud])
            for cloud in LABELS}


def validate_context(cloud, profile, region, scope):
    if cloud not in LABELS:
        raise ValueError('Choose OCI, Azure, Google Cloud, or AWS.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,99}', profile):
        raise ValueError('Enter a valid local CLI profile or configuration.')
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,79}', region):
        raise ValueError('Enter a valid cloud region.')
    patterns = {'aws': r'\d{12}', 'azure': r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',
                'gcp': r'(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{6,30})',
                'oci': r'ocid1\.(?:compartment|tenancy)\.[a-z0-9.]+'}
    if not re.fullmatch(patterns[cloud], scope):
        raise ValueError('Enter a valid account, subscription, project, or compartment for the selected cloud.')


def read_only(cloud, command):
    if any(re.search(r'key|secret|credential|token|password|connection-string|sas', word) for word in command):
        return False
    if cloud == 'aws':
        return command == ['sts', 'get-caller-identity'] or command[-1].startswith(('describe-', 'list-'))
    return command[-1] in ({'list', 'show'} if cloud == 'azure' else {'list', 'describe'})


MANAGED = {
    '--profile', '--region', '--output', '--endpoint-url', '--endpoint', '--no-sign-request',
    '--no-verify-ssl', '--ca-bundle', '--debug', '--cli-input-json', '--cli-input-yaml',
    '--generate-cli-skeleton', '--cli-auto-prompt', '--no-cli-auto-prompt', '--no-cli-pager',
    '--subscription', '--tenant', '--scope', '--management-group-id', '--cloud', '--only-show-errors',
    '--configuration', '--project', '--billing-project', '--account', '--impersonate-service-account',
    '--access-token-file', '--flags-file', '--format', '--quiet', '--log-http', '--trace-token',
    '--verbosity', '--help', '--version', '--file', '--output-file', '--outfile', '--log-file',
    '--output-directory', '--from-file', '--cli-rc-file', '--defaults-file', '--auth',
    '--source', '--code', '--user-output-enabled', '--defaults', '--cli-read-timeout', '--cli-connect-timeout',
    '--path', '--directory', '--destination', '--destination-path', '--generate-ssh-keys',
}
BLOCKED = {
    'aws': {'configure', 'sso', 'sso-oidc', 'history', 's3', 'help'},
    'azure': {'login', 'logout', 'config', 'configure', 'extension', 'upgrade', 'interactive', 'rest',
              'cloud', 'alias', 'bicep', 'ad', 'tenant', 'management-group'},
    'gcp': {'auth', 'config', 'components', 'init', 'topic', 'alpha', 'beta', 'interactive'},
}


def validate_step(cloud, step, resolved=False):
    if not 2 <= len(step.command) <= 12 or not all(re.fullmatch(r'[a-z][a-z0-9-]*', word) for word in step.command):
        raise ValueError('Use a CLI subcommand path without an executable or shell syntax.')
    if step.command[0] in BLOCKED[cloud] or any(word in {'ssh', 'scp', 'exec', 'run-command', 'cloud-shell', 'submit', 'up', 'deploy'} for word in step.command):
        raise ValueError('This command requires a separate local operator session.')
    if step.vault_id is not None:
        raise ValueError('vault_id is only supported for OCI.')
    for index, arg in enumerate(step.arguments):
        flag = arg.split('=', 1)[0]
        if (flag in MANAGED or flag.startswith('--no-') and flag[5:] in {'log-http', 'user-output-enabled'}
                or flag.startswith('--') and any(item.startswith(flag) for item in MANAGED)
                or re.search(r'--.*(?:file|directory|credential|endpoint|impersonat)', flag)
                or arg == '--' or arg.startswith('-') and not arg.startswith('--')):
            raise ValueError(f'Connection, output, authentication, or file option is managed or unsupported: {flag}')
        if any(char in arg for char in '\x00\r\n') or arg.startswith(('@', 'file://', 'fileb://')):
            raise ValueError('Local file arguments and control characters are not supported.')
        if '{{connection.' in arg or '{{task_defaults.' in arg:
            raise ValueError('Use the selected cloud context, not OCI connection placeholders.')
        if not resolved and re.search(r'--.*(?:password|secret-string|client-secret|access-token|api-key)', flag):
            value = arg.split('=', 1)[1] if '=' in arg else step.arguments[index + 1] if index + 1 < len(step.arguments) else ''
            if not re.fullmatch(r'\{\{secret:[A-Z][A-Z0-9_]*\}\}', value) or '=' in arg:
                raise ValueError('Use a whole hidden-input placeholder in a separate argv value for secrets.')
