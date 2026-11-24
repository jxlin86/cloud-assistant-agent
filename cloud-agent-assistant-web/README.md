# cloud-agent-assistant-web

A browser agent **v0.3.2** for **Windows and Linux (including Oracle Linux 9)**, managing **OCI, Microsoft Azure, Google Cloud, and Amazon Web Services (AWS)**. OCI tasks use the bundled OCI Operations **v0.1.4** engine; the other clouds have separate scoped planners and executors. All planning uses the configured OCI-hosted `openai.gpt-oss-120b` model through the Agents SDK. The original command-line agent remains separate.

Version 0.3.2 fixes systemd WorkingDirectory formatting and validates new units using `systemd-analyze --user verify` before replacing an installed unit. WorkingDirectory paths are not quoted; ExecStart arguments and environment values retain their required quoting.

Version 0.3.1 builds the application in a clean staging directory, checks every wheel payload against current sources, force-reinstalls that wheel, and verifies installed CLI/service imports outside the source path. This prevents newer cached `build/lib` files from producing mixed-version upgrades. The service installer checks user-bus connectivity before writing a unit and prints recovery steps when the bus is unavailable.

## Windows and Linux support

`python manage.py install`, `install-service`, `start`, and `stop` detect the operating system. Windows invokes the existing PowerShell scripts and Task Scheduler setup. Linux invokes Bash scripts and systemd user services. Script-specific arguments after the action use that platform's syntax. Python 3.12 or newer is required. Virtual environments must be created on the target OS; do not copy a Windows `.venv` to Linux.

Cloud operations use the host's native `oci`, `az`, `gcloud`, or `aws` executable. Linux rejects Windows `.exe`, `.cmd`, `.bat`, and `.ps1` launchers. Windows Azure, Google Cloud, and OCI batch launchers use supported Python entry points rather than a command shell. Commands execute as argument arrays with `shell=False` on both systems; JSON, spaces and shell punctuation are preserved as data. Command displays use Windows quoting on Windows and POSIX quoting on Linux. The OS is reported by `/health`.

The dependency lock uses an OS marker for `pywin32`, so one lock works on both platforms. OCI CLI can live in its separate `~/.venvs/cloud-agent-oci` environment; this location is found even outside an interactive login shell. Other cloud CLIs require their own installations and credentials on the machine running the service.

### Oracle Linux 9 installation and service

Oracle Linux 9.4 or later supplies Python 3.12. Install its prerequisites, then install the application as `opc`:

```bash
sudo dnf install -y python3.12 python3.12-pip python3.12-devel gcc make libffi-devel openssl-devel
cd ~/cloud-agent-assistant-web
python3.12 manage.py install --with-oci-cli
```

See [Oracle's Python installation guide](https://docs.oracle.com/en/operating-systems/oracle-linux/9/python/OL9-PYTHON.pdf). OCI CLI uses a separate environment to avoid SDK dependency conflicts, following [Oracle's virtual environment guidance](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/climanualinst.htm).

Configure the OCI profile under `~/.oci/config` and match it to `agent.toml`. Keep the existing Linux credential configuration when upgrading. Use the actual VM address for direct browser access:

```bash
# Stop/disable an older SYSTEM service first, if you installed one previously.
sudo systemctl disable --now cloud-assistant-agent.service

# Run these as opc, without sudo. This creates a USER service.
sudo loginctl enable-linger opc
sudo systemctl start "user@$(id -u).service"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
python3.12 manage.py install-service --host 0.0.0.0 --port 8000 --allowed-host 203.0.113.10
systemctl --user status cloud-assistant-agent --no-pager
curl --fail http://127.0.0.1:8000/health
```

Skip the system-service stop command if no old system service exists. The new unit is at `~/.config/systemd/user/cloud-assistant-agent.service`. It runs as the logged-in application account and uses that account's home directory and credentials. Lingering keeps its user manager running after logout and at boot. Use `systemctl --user restart cloud-assistant-agent` and `journalctl --user -u cloud-assistant-agent -n 60 --no-pager` for operations. For a localhost-only service, omit the host/port/allowed-host options; it defaults to localhost:8766.

The native launcher also works in the foreground:

```bash
.venv/bin/python -m cloud_agent_assistant_web --host 0.0.0.0 --port 8000 --allowed-host 203.0.113.10
```

`run_service.py` now delegates to this launcher. `AGENT_ALLOWED_HOSTS` is optional; `AGENT_BIND_HOST`, `AGENT_PORT`, and `AGENT_ALLOWED_HOSTS` are supported when environment configuration is preferred. CLI options take precedence. A non-loopback listener requires explicit browser hostnames/IPs; wildcards are rejected. For direct access, retain source-restricted OCI and OS firewall rules for TCP 8000. The UI has no user login and direct HTTP is unencrypted; use an SSH tunnel or HTTPS when appropriate. Stop active tasks before upgrading or restarting; submitted cloud operations can outlive the service.

### Validation

The test suite exercises Windows and Linux CLI resolution, shell-free argument preservation, redaction, launcher settings, network host checks and systemd unit generation. The included GitHub Actions workflow runs native Windows and Oracle Linux 9 container tests when triggered in a repository. Container tests do not verify systemd, SELinux policy, OCI permissions, or live model access; those require validation on the deployed VM.

## OCI stage limit recovery

OCI tasks retain the configured execution stage budget (eight by default). After the last stage, the browser adapter permits one final model assessment that cannot execute resource commands or request user input. It can report success only when the core engine has passed verification and the model confirms that the recorded evidence answers the request. If work remains, the task stops with an assessment summary; tasks that attempted changes retain a partially completed status. This extra assessment is included in LLM usage metrics.

The adapter normalizes the known invalid `compute instance list-vnic-attachments` path to `compute vnic-attachment list` before normal validation and approval. Planning guidance also explains CLI `data` response paths and distinguishes public IP assignment from verified Internet connectivity. Earlier blocked tasks keep their historical outcomes; submit a new task to assess their current resource state.

## Select a cloud

Choose the execution provider before describing a task. The form changes its account fields and task examples for that provider. History records the provider and scope per task.

For OCI, the compartment field displays the resolved **name** alongside the full editable **OCID**. Changing the OCI profile, region, or OCID refreshes the name using a read-only OCI lookup. Task details display `name (OCID)`; new tasks save the resolved name in history, and older tasks look up the name when viewed. If a lookup fails, the OCID stays usable and the name is marked unavailable. Successful lookups are cached on the service for five minutes.

| Provider | Local CLI | Profile / configuration | Required scope |
| --- | --- | --- | --- |
| OCI | `oci` | OCI profile | Compartment or tenancy OCID |
| Azure | `az` | Existing Azure CLI sign-in | Subscription UUID |
| Google Cloud | `gcloud` | Named gcloud configuration | Project ID or project number |
| AWS | `aws` (CLI v2) | Named AWS profile | 12-digit AWS account ID |

Install and authenticate each provider's CLI locally before executing tasks, then restart the web service so it picks up PATH changes. The UI reports CLI detection; credentials and scope are checked when a task runs. Plan-only tasks can be generated without an installed execution CLI. No cloud CLI is installed and no cloud resource is created by this update.

The `[oci]` and `[model]` settings still provide the model-hosting connection for all providers. AWS profiles, Azure subscriptions, and gcloud configurations do not replace model credentials. AWS uses explicit profile/region and checks STS account identity; Azure pins a subscription and verifies it; Google pins a configuration/project and verifies that project. Regions/locations for Azure and Google resource creation are supplied only on commands that support them.

Recognized inventory reads run automatically. Other commands require acceptance of the displayed plan. Every stage checks scope; asynchronous plans use bounded read polling, and resource assertions must pass before a task completes. Failed/uncertain mutations are never replayed automatically. Interactive login, CLI configuration/installation, shell commands, local source uploads, and file-based CLI input/output for the new providers require a separate operator session. Windows batch wrappers are bypassed through the installed CLI's Python entry point to keep execution shell-free.

## Open the application

On this laptop, after starting the service, open **http://127.0.0.1:8766**.

```powershell
.\Start-CloudAgentWeb.ps1
```

The background process continues when you close the terminal or browser. The default service is bound to this laptop's loopback interface. It runs as the signed-in Windows user and uses that user's `.oci/config`, default profile `OCIPRJ`, task region `us-ashburn-1`, and model region `us-chicago-1`.

To install on another checkout, use Python 3.12 or newer:

```powershell
.\Install-CloudAgentWeb.ps1 -Python python
.\Start-CloudAgentWeb.ps1
```

To start automatically when you sign in, register the background user service:

```powershell
.\Install-UserService.ps1
```

This registers a per-user Task Scheduler entry named `cloud-agent-assistant-web`, with restart-on-failure settings and no stored Windows password. It is a background user service, rather than an elevated Windows SCM service. It starts at user sign-in, so it can access the same OCI profile and private-key files as the terminal agent. Existing task state is never replayed automatically after restart.

To stop:

```powershell
.\Stop-CloudAgentWeb.ps1
```

To remove automatic startup, run `Unregister-ScheduledTask -TaskName cloud-agent-assistant-web -Confirm:$false`. The source, history, and configuration remain available.

## Browser workflow

1. Select a cloud, its local profile/configuration, region/location, and account/subscription/project/compartment; describe your task.
2. Choose **Run task**, or enable **Plan only** to inspect a proposed plan without execution.
3. Read-only commands execute automatically. For changes, review the command plan and select **Accept plan** or **Decline**.
4. Clarification questions and hidden secret fields appear when the engine needs input. Use `{{secret:ADMIN_PASSWORD}}` in task descriptions; enter the actual value only in the hidden input field.
5. Follow commands, readable results, and asynchronous status updates in the activity panel. Each executed command has a **Readable output** section and an expandable **JSON command and output** section containing its arguments, exit code, response, standard output, and standard error. Both views redact sensitive values. Tasks continue if the browser closes; reopen the task in history to respond or view results. Command cards are recorded for new executions; earlier history retains its original summaries.

Cancellation stops future commands and releases input waits. A command already submitted to a cloud may continue. After an interrupted service or uncertain mutation result, inspect actual cloud state before retrying.

The task panel displays provider-reported **input tokens**, **output tokens**, **total tokens**, and **LLM calls**. Counts update after each model response and accumulate across tool turns and plan repairs. They remain in saved task history. Calls without usage (including failed calls) are marked separately; partial totals include only responses with reported input and output counts. Earlier tasks without recorded usage show an unavailable indicator. No token estimates or prompt content are stored for these metrics.

Hidden values are sent only to the local service, registered for redaction before the worker resumes, and supplied directly to the CLI. They are excluded from browser history, application event records, core audit storage, and model context. The browser clears its input after submission; the worker clears secret input after the task. Never paste private keys, passwords, or tokens into ordinary task descriptions or clarification answers.

## Capability parity with v0.1.4

- OCI-hosted LLM planning, official Oracle CLI reference/help, and bounded corrections.
- Named profile and region selection; pinned CLI connection flags and shell-free execution.
- Automatic authorization for read-only tasks; explicit approval before writes and changed write plans.
- Human-readable results, separate job histories, safe generated database names, local secret input, provisioning defaults, and structured audit evidence.
- Asynchronous polling, final-state checks, timeout/failure reporting, and no automatic mutation replay.
- Scalar-root binding validation and KMS Vault endpoint handling; AES byte lengths and ENABLED key readiness.

The default poll interval is 15 seconds and the completion timeout is one hour. Configure the engine in this application's `agent.toml`.

For an approved file input/output task, place files in `.state/jobs/<task-id>/.fde-agent/files`. The engine's existing file scope checks still apply. OCI CLI file operations remain visible in the accepted plan.

## Architecture and local data

```text
Browser (HTML/CSS/JS)
   | same-origin HTTP + local session + CSRF
Starlette / Uvicorn (127.0.0.1:8766)
   | job queue, browser approval/question/secret bridge
Single background worker (one cloud task at a time)
   | provider router: OCI core / Azure / Google Cloud / AWS
   +-- OCI Generative AI: openai.gpt-oss-120b, us-chicago-1
   +-- Official CLI documentation for the selected provider
   +-- Scoped oci / az / gcloud / aws commands
   +-- Bounded read-only resource/work-request polling
```

`.state/web-history.sqlite3` holds sanitized job metadata, provider context, command/output events, and LLM usage. OCI core audit history is isolated in each job directory; the original terminal agent's history is untouched. Pending input is held only in memory. On restart, unfinished records are marked interrupted and require a new user task; no approval or hidden value is restored. Process diagnostics are in `.state/service.out.log` and `.state/service.err.log` when started in the background.

The service has no remote-listening option, CORS access, public deployment, or cloud resource provisioning at startup. Host validation, same-origin checks, SameSite/HttpOnly browser cookies, CSRF tokens, request limits, and a restrictive content security policy apply to the browser API. Use a separately designed authenticated deployment if access from other devices is needed.

## Verify

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m fde_oci_agent --config agent.toml doctor
```

Service tests simulate model responses and all cloud commands. Tests must not provision real cloud resources. The bundled core engine's own validation record is 211 tests in the original v0.1.4 project.

Validated for v0.2.1: 109 service tests passed. Browser tests covered OCI, Azure, Google Cloud, and AWS selection and routing, compartment name/OCID display, scoped results, command/JSON views, approvals, hidden input, token counters, history reload, and mobile layout without script errors or overflow. Model-tool integration tests use an HTTP mock. The configured OCI compartment name was also verified through a live read-only lookup; no cloud resources were changed. Azure, Google Cloud, and AWS execution still require locally installed and authenticated CLIs.

Provider CLI references: [AWS](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-options.html), [Azure](https://learn.microsoft.com/en-us/cli/azure/azure-cli-global-parameters?view=azure-cli-latest), [Google Cloud](https://docs.cloud.google.com/sdk/gcloud/reference).
