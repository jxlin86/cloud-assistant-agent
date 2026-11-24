#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then echo 'Run bash Install-CloudAgentWeb.sh first.' >&2; exit 1; fi
if [[ $# -gt 0 ]]; then
  # Explicit settings launch in the foreground; systemd is used for persistence.
  exec .venv/bin/python -m cloud_agent_assistant_web --config "$PWD/agent.toml" "$@"
fi
systemctl --user start cloud-assistant-agent.service
systemctl --user --no-pager status cloud-assistant-agent.service
