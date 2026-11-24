"""Host OS decisions; cloud arguments are always passed without a shell."""
from dataclasses import replace
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess


def host_os():
    system = platform.system()
    if system not in {"Windows", "Linux"}:
        raise ValueError(f"Unsupported host OS: {system}. Use Windows or Linux.")
    return system


def format_command(arguments):
    return subprocess.list2cmdline(arguments) if host_os() == "Windows" else shlex.join(arguments)


def find_cli(name):
    """Include common unattended-service locations, beyond the login-shell PATH."""
    windows = host_os() == "Windows"
    executable = shutil.which(name)
    if executable:
        return executable
    home = Path.home()
    candidates = ([home / "bin" / f"{name}.exe", home / ".local" / "bin" / f"{name}.exe"]
                  if windows else [home / ".local" / "bin" / name, home / "bin" / name])
    if name == "oci":
        candidates.insert(0, home / ".venvs" / "cloud-agent-oci" /
                          ("Scripts/oci.exe" if windows else "bin/oci"))
    for path in candidates:
        if path.is_file() and (windows or os.access(path, os.X_OK)):
            return str(path)
    return None


def load_settings(path):
    from fde_oci_agent.config import load_settings as load_core_settings
    settings = load_core_settings(path)
    # The pinned engine's fallback ends in oci.exe, even on Linux.
    return replace(settings, cli=find_cli("oci") or ("oci.exe" if host_os() == "Windows" else "oci"))
