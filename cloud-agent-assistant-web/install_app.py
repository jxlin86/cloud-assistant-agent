"""Build only current sources in a clean directory, then replace the installed app."""
import ast
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


def stage_sources(source, destination):
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('pyproject.toml', 'README.md'):
        shutil.copyfile(source/name, destination/name)
    shutil.copytree(source/'src/cloud_agent_assistant_web', destination/'src/cloud_agent_assistant_web',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))


def verify_wheel(wheel, source):
    with zipfile.ZipFile(wheel) as archive:
        for file in (Path(source)/'src/cloud_agent_assistant_web').rglob('*'):
            if file.is_file() and '__pycache__' not in file.parts and file.suffix != '.pyc':
                name = file.relative_to(Path(source)/'src').as_posix()
                if archive.read(name) != file.read_bytes():
                    raise ValueError(f'Built wheel differs from current source: {name}')


def expected_version(source):
    tree = ast.parse((Path(source)/'src/cloud_agent_assistant_web/__init__.py').read_text(encoding='utf-8-sig'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Application version missing')


def build_wheel(source, staging, wheels, isolated=True):
    stage_sources(source, staging)
    command = [sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-cache-dir',
               '--wheel-dir', str(wheels)]
    if not isolated:
        command.append('--no-build-isolation')
    subprocess.run([*command, str(staging)], check=True)
    wheel, = Path(wheels).glob('cloud_agent_assistant_web-*.whl')
    verify_wheel(wheel, source)
    return wheel


def main():
    source = Path(__file__).resolve().parent
    state = source/'.state'
    state.mkdir(exist_ok=True)
    # Only this newly created temporary directory is removed by its context manager.
    with tempfile.TemporaryDirectory(prefix='install-', dir=state) as directory:
        temporary = Path(directory).resolve()
        if temporary.parent != state.resolve():
            raise ValueError('Unexpected build directory')
        wheel = build_wheel(source, temporary/'source', temporary/'wheels')
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-cache-dir',
                        '--force-reinstall', str(wheel)], check=True)
    script = '''import sys
import cloud_agent_assistant_web as app
from cloud_agent_assistant_web.__main__ import parse_args
from cloud_agent_assistant_web.service import render_unit
assert app.__version__ == sys.argv[1], (app.__version__, sys.argv[1])
assert callable(parse_args) and callable(render_unit)
print('Verified installed app:', app.__version__, app.__file__)
'''
    subprocess.run([sys.executable, '-I', '-c', script, expected_version(source)], check=True)


if __name__ == '__main__':
    main()
