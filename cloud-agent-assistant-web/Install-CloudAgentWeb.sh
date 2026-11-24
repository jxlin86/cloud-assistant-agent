#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -f agent.toml && -f agent.example.toml ]]; then cp -- agent.example.toml agent.toml; fi
python_command=${PYTHON:-python3.12}
if ! command -v "$python_command" >/dev/null; then
  echo 'Install Python first: sudo dnf install python3.12 python3.12-pip python3.12-devel gcc make libffi-devel openssl-devel' >&2
  exit 1
fi
"$python_command" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12 or newer is required"'
if [[ -e .venv && ! -x .venv/bin/python ]]; then
  echo '.venv is not a working Linux environment. Preserve it and create a fresh Linux deployment directory.' >&2
  exit 1
fi
if [[ ! -d .venv ]]; then "$python_command" -m venv .venv; fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock.txt ./vendor/fde_oci_agent-0.1.4-py3-none-any.whl
.venv/bin/python install_app.py
.venv/bin/python -m pip check
if [[ ${1:-} == --with-oci-cli ]]; then
  cli="$HOME/.venvs/cloud-agent-oci"
  if [[ ! -d $cli ]]; then "$python_command" -m venv "$cli"; fi
  "$cli/bin/python" -m pip install 'oci-cli==3.96.3'
  "$cli/bin/python" -m pip check
fi
echo 'Installed. Use python3.12 manage.py install-service, or bash Start-CloudAgentWeb.sh.'
