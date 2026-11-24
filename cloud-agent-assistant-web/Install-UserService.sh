#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then echo 'Run bash Install-CloudAgentWeb.sh first.' >&2; exit 1; fi
.venv/bin/python -c 'from cloud_agent_assistant_web.__main__ import parse_args; from cloud_agent_assistant_web.service import render_unit' || {
  echo 'Installed app files are inconsistent. Run: python3.12 manage.py install' >&2
  exit 1
}
if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo 'The current account user-service bus is unavailable. Run these commands as your application account:' >&2
  printf 'sudo loginctl enable-linger %q\n' "$(id -un)" >&2
  printf 'sudo systemctl start user@%s.service\n' "$(id -u)" >&2
  printf 'export XDG_RUNTIME_DIR=/run/user/%s\n' "$(id -u)" >&2
  printf 'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"\n' >&2
  echo 'Then retry install-service. If it still fails, inspect the user@UID.service journal.' >&2
  exit 1
fi
if systemctl --user is-active --quiet cloud-assistant-agent.service; then
  echo 'The user service is active. Finish active tasks and stop it before changing service settings.' >&2
  exit 1
fi
.venv/bin/python -m cloud_agent_assistant_web.service --config "$PWD/agent.toml" "$@"
systemctl --user daemon-reload
systemctl --user enable --now cloud-assistant-agent.service
echo 'To keep the service running after logout and start it at boot, run:'
printf 'sudo loginctl enable-linger %q\n' "$(id -un)"
