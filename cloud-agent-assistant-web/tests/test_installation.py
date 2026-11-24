"""Exercise a real wheel and installed imports, not just the source checkout."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


def installer():
    spec = importlib.util.spec_from_file_location('install_app', Path(__file__).parents[1]/'install_app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clean_build_ignores_newer_stale_build_and_installs_matching_modules(tmp_path):
    pytest.importorskip('setuptools')
    module = installer()
    source = tmp_path/'checkout'
    module.stage_sources(Path(__file__).parents[1], source)
    current = source/'src/cloud_agent_assistant_web/__main__.py'
    os.utime(current, (1, 1))
    stale = source/'build/lib/cloud_agent_assistant_web/__main__.py'
    stale.parent.mkdir(parents=True)
    stale.write_text('def main(): pass\n')
    wheel = module.build_wheel(source, tmp_path/'clean', tmp_path/'wheels', isolated=False)
    assert not (tmp_path/'clean/build/lib/cloud_agent_assistant_web/__main__.py').read_bytes() == stale.read_bytes()
    target = tmp_path/'installed'
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps',
                    '--target', str(target), str(wheel)], check=True, capture_output=True, text=True)
    script = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import cloud_agent_assistant_web as app
from cloud_agent_assistant_web.__main__ import parse_args
from cloud_agent_assistant_web.service import render_unit
assert Path(app.__file__).is_relative_to(Path(sys.argv[1]))
assert app.__version__ == sys.argv[2]
assert parse_args([]).port == 8766
'''
    result = subprocess.run([sys.executable, '-I', '-c', script, str(target), module.expected_version(source)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_service_installer_checks_bus_before_writing_unit():
    script = (Path(__file__).parents[1]/'Install-UserService.sh').read_text()
    assert script.index('systemctl --user show-environment') < script.index('-m cloud_agent_assistant_web.service')
    assert 'loginctl enable-linger' in script and 'XDG_RUNTIME_DIR' in script
