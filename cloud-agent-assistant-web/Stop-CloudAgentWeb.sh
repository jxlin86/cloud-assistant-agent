#!/usr/bin/env bash
set -euo pipefail
systemctl --user stop cloud-assistant-agent.service
echo 'Stopped. Already submitted cloud operations may continue; inspect state before retrying.'
