import argparse
import json
import os
import ipaddress
import re
from pathlib import Path

import uvicorn
from .runtime import load_settings, host_os
from .app import create_app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Cloud Agent Assistant Web — OCI, Azure, Google Cloud, and AWS")
    parser.add_argument("--config", type=Path, default=Path("agent.toml"))
    parser.add_argument("--port", type=int, default=os.getenv("AGENT_PORT", "8766"))
    parser.add_argument("--host", default=os.getenv("AGENT_BIND_HOST", "127.0.0.1"))
    parser.add_argument("--allowed-host", action="append", default=[], help="Browser hostname or IP; repeat for multiple hosts")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be 1024-65535")
    try:
        listen = ipaddress.ip_address(args.host) if args.host != 'localhost' else ipaddress.ip_address('127.0.0.1')
    except ValueError:
        parser.error('--host must be an IP address or localhost')
    configured = args.allowed_host or [h.strip() for h in os.getenv('AGENT_ALLOWED_HOSTS', '').split(',') if h.strip()]
    if not listen.is_loopback and not configured:
        parser.error('Network binding requires --allowed-host with the VM IP or DNS name used by your browser')
    for host in configured:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', host):
                parser.error('Allowed hosts must be hostnames or IP addresses, without a scheme, port, or wildcard')
        else:
            if ip.is_unspecified:
                parser.error('Use the actual browser hostname/IP for --allowed-host, not a wildcard bind address')
    args.allowed_hosts = tuple(dict.fromkeys(['127.0.0.1', 'localhost', '::1', *(h.lower() for h in configured)]))
    return args


def main(argv=None):
    host_os()
    args = parse_args(argv)
    config = args.config.resolve()
    settings = load_settings(config)
    state = config.parent / ".state"
    state.mkdir(parents=True, exist_ok=True)
    runtime = state / "runtime.json"
    runtime.write_text(json.dumps(dict(pid=os.getpid(), port=args.port, service="cloud-agent-assistant-web", config=str(config))), encoding="utf-8")
    try:
        uvicorn.run(create_app(settings, state, hosts=args.allowed_hosts), host=args.host, port=args.port,
                    workers=1, access_log=False, log_level="warning", timeout_graceful_shutdown=5)
    finally:
        if runtime.exists() and json.loads(runtime.read_text(encoding="utf-8")).get("pid") == os.getpid():
            runtime.unlink()


if __name__ == "__main__":
    main()
