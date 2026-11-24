import importlib.util
from pathlib import Path
import shlex
import subprocess

import pytest

from cloud_agent_assistant_web import runtime
from cloud_agent_assistant_web.__main__ import parse_args, main
from cloud_agent_assistant_web.clouds import cli_launcher
from cloud_agent_assistant_web.jobs import BrowserExecutor, Job, Store
from cloud_agent_assistant_web.service import render_unit, quote
from test_service import settings
from starlette.testclient import TestClient
from cloud_agent_assistant_web.app import create_app


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ('AGENT_BIND_HOST', 'AGENT_PORT', 'AGENT_ALLOWED_HOSTS'):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize('system', ['Linux', 'Windows'])
def test_native_cloud_commands_are_resolved_for_each_os(tmp_path, monkeypatch, system):
    monkeypatch.setattr(runtime.platform, 'system', lambda: system)
    for cloud, command in [('oci', 'oci'), ('azure', 'az'), ('gcp', 'gcloud'), ('aws', 'aws')]:
        path = tmp_path / (command + ('.exe' if system == 'Windows' else ''))
        path.touch()
        path.chmod(0o700)
        monkeypatch.setattr(runtime.shutil, 'which', lambda name, path=path: str(path))
        assert cli_launcher(cloud) == [str(path)]


@pytest.mark.parametrize('suffix', ['.exe', '.cmd', '.bat', '.ps1'])
def test_linux_never_uses_windows_launchers(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(runtime.platform, 'system', lambda: 'Linux')
    path = tmp_path / ('oci' + suffix)
    path.touch()
    monkeypatch.setattr(runtime.shutil, 'which', lambda name: str(path))
    assert cli_launcher('oci', str(path)) is None


def test_windows_oci_batch_uses_python_entry_point(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.platform, 'system', lambda: 'Windows')
    batch = tmp_path / 'oci.cmd'
    batch.touch()
    python = tmp_path / 'python.exe'
    python.touch()
    assert cli_launcher('oci', str(batch)) == [str(python), '-I', '-m', 'oci_cli.cli']
    python.unlink()
    assert cli_launcher('oci', str(batch)) is None


@pytest.mark.parametrize('system,relative', [('Linux', '.venvs/cloud-agent-oci/bin/oci'),
                                           ('Windows', '.venvs/cloud-agent-oci/Scripts/oci.exe')])
def test_cli_found_without_login_shell_path(tmp_path, monkeypatch, system, relative):
    monkeypatch.setattr(runtime.platform, 'system', lambda: system)
    monkeypatch.setattr(runtime.shutil, 'which', lambda name: None)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    cli = tmp_path / relative
    cli.parent.mkdir(parents=True)
    cli.touch()
    cli.chmod(0o700)
    assert runtime.find_cli('oci') == str(cli)


@pytest.mark.parametrize('system,expected', [('Linux', 'oci'), ('Windows', 'oci.exe')])
def test_settings_fallback_is_native(settings, monkeypatch, system, expected):
    monkeypatch.setattr(runtime.platform, 'system', lambda: system)
    monkeypatch.setattr(runtime, 'find_cli', lambda name: None)
    config = settings.root / 'agent.toml'
    config.write_text(f'''[oci]
profile="OCIPRJ"
config_file="{settings.config_file.as_posix()}"
region="us-ashburn-1"
[model]
name="test-model"
region="us-chicago-1"
''')
    assert runtime.load_settings(config).cli == expected


@pytest.mark.parametrize('system', ['Linux', 'Windows'])
def test_command_display_uses_native_quoting(monkeypatch, system):
    monkeypatch.setattr(runtime.platform, 'system', lambda: system)
    argv = ['oci', 'compute', 'instance', 'list', '--query', 'data[]."display-name"', '--name', 'A B; $HOME']
    formatted = runtime.format_command(argv)
    if system == 'Linux':
        assert shlex.split(formatted) == argv
        assert "'A B; $HOME'" in formatted
    else:
        assert formatted == subprocess.list2cmdline(argv)


@pytest.mark.parametrize('system', ['Linux', 'Windows'])
def test_oci_executor_keeps_arguments_literal_and_redacts_output(settings, tmp_path, monkeypatch, system):
    monkeypatch.setattr(runtime.platform, 'system', lambda: system)
    prefix = ['/usr/bin/oci'] if system == 'Linux' else [r'C:\OCI\python.exe', '-I', '-m', 'oci_cli.cli']
    monkeypatch.setattr('cloud_agent_assistant_web.jobs.cli_launcher', lambda *args: prefix)
    job = Job(Store(tmp_path / 'jobs.sqlite3'), 'Read', settings, False)
    executor = BrowserExecutor(settings, job)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{"data":{"password":"hidden","id":"test"}}', '')
    monkeypatch.setattr('cloud_agent_assistant_web.jobs.subprocess.run', run)
    args = ['compute', 'instance', 'list', '--query', 'data[]."display-name"', '--name', 'A B; $(whoami) & x']
    result = executor._run_command(args, 12)
    assert calls[0][0] == [*prefix, *args]
    assert calls[0][1]['shell'] is False
    assert calls[0][1]['stdin'] == subprocess.DEVNULL
    assert calls[0][1]['timeout'] == 12
    assert 'hidden' not in result.stdout
    assert result.data['data']['password'] == '[REDACTED]'


def test_default_startup_does_not_require_allowed_hosts_environment():
    args = parse_args([])
    assert args.host == '127.0.0.1' and args.port == 8766
    assert 'localhost' in args.allowed_hosts


def test_network_startup_accepts_flags_and_environment(monkeypatch):
    monkeypatch.setenv('AGENT_BIND_HOST', '0.0.0.0')
    monkeypatch.setenv('AGENT_PORT', '8000')
    monkeypatch.setenv('AGENT_ALLOWED_HOSTS', '203.0.113.10,VM.example.com')
    args = parse_args([])
    assert args.port == 8000
    assert 'vm.example.com' in args.allowed_hosts
    args = parse_args(['--port', '8001', '--allowed-host', '203.0.113.20'])
    assert args.port == 8001 and '203.0.113.20' in args.allowed_hosts
    assert '203.0.113.10' not in args.allowed_hosts


@pytest.mark.parametrize('arguments', [
    ['--host', '0.0.0.0'], ['--host', '::'], ['--port', '22'],
    ['--allowed-host', '*'], ['--allowed-host', 'http://host'],
    ['--allowed-host', 'host:8000'], ['--allowed-host', '0.0.0.0'],
])
def test_invalid_network_settings_fail_before_binding(arguments):
    with pytest.raises(SystemExit):
        parse_args(arguments)


def test_launcher_passes_network_settings_and_cleans_runtime(settings, monkeypatch):
    monkeypatch.setattr('cloud_agent_assistant_web.__main__.load_settings', lambda _: settings)
    monkeypatch.setattr('cloud_agent_assistant_web.__main__.create_app', lambda *args, **kw: kw)
    calls = []
    monkeypatch.setattr('cloud_agent_assistant_web.__main__.uvicorn.run', lambda *args, **kw: calls.append((args, kw)))
    main(['--config', str(settings.root/'agent.toml'), '--host', '0.0.0.0', '--port', '8000',
          '--allowed-host', '203.0.113.10'])
    assert calls[0][1]['host'] == '0.0.0.0' and calls[0][1]['port'] == 8000
    assert calls[0][1]['workers'] == 1
    assert '203.0.113.10' in calls[0][0][0]['hosts']
    assert not (settings.root/'.state/runtime.json').exists()


def test_linux_unit_runs_current_user_with_explicit_network_settings():
    unit = render_unit('/home/opc/app/.venv/bin/python', '/home/opc/app/agent.toml',
                       '/home/opc', '0.0.0.0', 8000, ('127.0.0.1', '203.0.113.10'))
    assert 'User=root' not in unit and 'User=' not in unit
    assert 'WantedBy=default.target' in unit
    assert 'WorkingDirectory=/home/opc/app\n' in unit
    assert 'WorkingDirectory="' not in unit
    assert '"--port" "8000"' in unit
    assert '"--allowed-host" "203.0.113.10"' in unit
    assert 'AGENT_ALLOWED_HOSTS' not in unit
    assert '/home/opc/.venvs/cloud-agent-oci/bin' in unit
    assert quote('/home/opc/A B%/python') == '"/home/opc/A B%%/python"'
    with pytest.raises(ValueError):
        quote('a\nExecStart=bad')


def test_systemd_working_directory_preserves_spaces_without_quotes():
    unit = render_unit('/home/opc/my app/.venv/bin/python', '/home/opc/my app/agent.toml',
                       '/home/opc', '127.0.0.1', 8000, ('127.0.0.1',))
    assert 'WorkingDirectory=/home/opc/my app\n' in unit
    assert 'ExecStart="/home/opc/my app/.venv/bin/python"' in unit


def test_generated_unit_passes_native_systemd_parser(tmp_path):
    import shutil
    import sys
    if runtime.host_os() != 'Linux' or not shutil.which('systemd-analyze'):
        pytest.skip('Native systemd parser requires Linux with systemd installed')
    config = tmp_path/'agent.toml'
    config.touch()
    unit = tmp_path/'cloud-assistant-agent.service'
    unit.write_text(render_unit(sys.executable, str(config), str(tmp_path), '127.0.0.1', 8000, ('127.0.0.1',)))
    result = subprocess.run(['systemd-analyze', 'verify', str(unit)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_failed_native_validation_preserves_existing_user_unit(tmp_path, monkeypatch):
    from cloud_agent_assistant_web import service
    monkeypatch.setattr(service, 'host_os', lambda: 'Linux')
    monkeypatch.setattr(service.os, 'geteuid', lambda: 1000, raising=False)
    content = render_unit('/usr/bin/python3.12', '/home/opc/app/agent.toml', '/home/opc',
                          '127.0.0.1', 8766, ('127.0.0.1',))
    monkeypatch.setattr(service, 'render_unit', lambda *args: content)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path/'config'))
    config = tmp_path/'agent.toml'
    config.touch()
    unit = tmp_path/'config/systemd/user/cloud-assistant-agent.service'
    unit.parent.mkdir(parents=True)
    unit.write_text('original unit')
    calls = []
    def reject(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, '', 'Invalid unit setting')
    monkeypatch.setattr(service.subprocess, 'run', reject)
    with pytest.raises(ValueError, match='Invalid unit setting'):
        service.main(['--config', str(config)])
    assert unit.read_text() == 'original unit'
    assert calls[0][:3] == ['systemd-analyze', '--user', 'verify']


def test_management_dispatches_native_tools(monkeypatch):
    spec = importlib.util.spec_from_file_location('agent_manage', Path(__file__).parents[1]/'manage.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.shutil, 'which', lambda name: 'powershell.exe')
    for action in ('install', 'start', 'stop', 'install-service'):
        windows = module.native_command(action, 'Windows')
        linux = module.native_command(action, 'Linux')
        assert windows[0] == 'powershell.exe' and windows[-1].endswith('.ps1')
        assert linux[0] == 'bash' and linux[-1].endswith('.sh')
        assert Path(windows[-1]).is_file() and Path(linux[-1]).is_file()


def test_network_host_allowlist_allows_selected_vm_and_rejects_other_hosts(settings, monkeypatch):
    monkeypatch.setattr(runtime.platform, 'system', lambda: 'Linux')
    class Manager:
        def close(self):
            pass
    app = create_app(settings, settings.root/'state', manager_factory=lambda *args: Manager(),
                     hosts=('127.0.0.1', '203.0.113.10'))
    with TestClient(app, base_url='http://203.0.113.10:8000') as client:
        response = client.get('/health')
        assert response.status_code == 200
        assert response.json()['operating_system'] == 'Linux'
        assert client.get('/health', headers={'host':'unrecognized.example:8000'}).status_code == 400


@pytest.mark.parametrize('failure,code,category', [
    (subprocess.TimeoutExpired('oci', 1), 124, 'uncertain'),
    (FileNotFoundError('CLI missing'), 127, 'local'),
])
def test_native_oci_execution_failure_categories(settings, tmp_path, monkeypatch, failure, code, category):
    monkeypatch.setattr('cloud_agent_assistant_web.jobs.cli_launcher', lambda *args: ['oci'])
    executor = BrowserExecutor(settings, Job(Store(tmp_path/'jobs.sqlite3'), 'Read', settings, False))
    def run(*args, **kwargs):
        raise failure
    monkeypatch.setattr('cloud_agent_assistant_web.jobs.subprocess.run', run)
    result = executor._run_command(['compute', 'instance', 'list'], 1)
    assert result.code == code and result.category == category
