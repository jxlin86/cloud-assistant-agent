"""Single-worker browser adapter for OCI and scoped multi-cloud workflows."""
from __future__ import annotations

import asyncio
import json
import os
import queue
import secrets
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fde_oci_agent.audit import Audit, SENSITIVE_KEY
from .redaction import Redactor
from .compartments import lookup_compartment
from fde_oci_agent.config import Settings
from fde_oci_agent.executor import Executor, CommandResult, classify, response_preview
from .runtime import format_command, host_os
from .clouds import cli_launcher
from .workflow import Workflow, OCI_PLANNING_HINTS


FINAL = {"succeeded", "planned", "failed", "blocked", "cancelled", "partially_completed", "interrupted"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id,id);
            """)

    def connect(self):
        return closing(sqlite3.connect(self.path, timeout=10, isolation_level=None))

    def save(self, metadata: dict):
        with self.lock, self.connect() as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?)", (metadata["id"], json.dumps(metadata)))

    def event(self, job_id: str, payload: dict):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO events(job_id,payload) VALUES(?,?)", (job_id, json.dumps(payload)))

    def listing(self):
        with self.lock, self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT metadata FROM jobs ORDER BY rowid DESC LIMIT 100")]

    def get(self, job_id: str):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT metadata FROM jobs WHERE id=?", (job_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def events(self, job_id: str, after: int):
        with self.lock, self.connect() as db:
            return [dict(json.loads(payload), cursor=cursor) for cursor, payload in db.execute(
                "SELECT id,payload FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT 250", (job_id, after))]


class Job:
    def __init__(self, store: Store, request: str, settings: Settings, plan_only: bool,
                 *, cloud='oci', profile=None, region=None, scope=None):
        self.store, self.settings, self.plan_only = store, settings, plan_only
        self.redactor = Redactor()
        if os.getenv("OCI_GENAI_API_KEY"):
            self.redactor.add(os.environ["OCI_GENAI_API_KEY"])
        self.request = self.redactor.text(request)
        self.id = secrets.token_hex(12)
        self.condition = threading.Condition(threading.RLock())
        self.pending: dict | None = None
        self.answer: str | None = None
        self.cancelled = False
        self.metadata = dict(id=self.id, request=self.request, profile=settings.profile, region=settings.region,
                             compartment_id=settings.compartment_id, status="queued", created_at=now(),
                             updated_at=now(), plan_only=plan_only, summary="Waiting for the worker")
        self.metadata.update(cloud=cloud, profile=profile or settings.profile, region=region or settings.region,
                             scope=scope or settings.compartment_id,
                             compartment_id=settings.compartment_id if cloud == 'oci' else '')
        self.metadata['compartment_name'] = ''
        self.metadata['llm_usage'] = dict(input_tokens=0, output_tokens=0, total_tokens=0,
                                          requests=0, missing_usage_requests=0)
        self.store.save(self.metadata)

    def record_usage(self, raw):
        # Only numeric counters enter history; no prompts or model content.
        raw = raw if isinstance(raw, dict) else {}
        incoming = raw.get('prompt_tokens', raw.get('input_tokens'))
        outgoing = raw.get('completion_tokens', raw.get('output_tokens'))
        known = all(type(value) is int and value >= 0 for value in (incoming, outgoing))
        with self.condition:
            usage = self.metadata['llm_usage']
            usage['requests'] += 1
            if known:
                usage['input_tokens'] += incoming
                usage['output_tokens'] += outgoing
                usage['total_tokens'] = usage['input_tokens'] + usage['output_tokens']
            else:
                usage['missing_usage_requests'] += 1
            self.metadata['updated_at'] = now()
            self.store.save(self.metadata)

    def update(self, status: str, summary: str | None = None):
        with self.condition:
            self.metadata.update(status=status, updated_at=now())
            if summary is not None:
                self.metadata["summary"] = self.redactor.text(summary)
            self.store.save(self.metadata)

    def emit(self, text: str):
        self.store.event(self.id, {"kind": "output", "text": self.redactor.text(text), "time": now()})

    def prompt(self, kind: str, text: str) -> str:
        with self.condition:
            if self.cancelled:
                raise EOFError("Task cancelled")
            self.pending = {"id": secrets.token_hex(12), "kind": kind, "text": self.redactor.text(text), "time": now()}
            self.answer = None
            self.update({"approval": "awaiting_approval", "secret": "awaiting_secret", "question": "awaiting_answer"}[kind])
            self.store.event(self.id, {"kind": "prompt", "prompt_kind": kind, "text": self.pending["text"], "time": now()})
            expires = time.monotonic() + 1800
            try:
                while self.answer is None and not self.cancelled:
                    remaining = expires - time.monotonic()
                    if remaining <= 0:
                        raise EOFError("Input expired after 30 minutes")
                    self.condition.wait(min(remaining, 10))
                if self.cancelled:
                    raise EOFError("Task cancelled")
                answer, self.answer = self.answer, None
                return answer
            finally:
                self.pending = None
                self.update("running")

    def respond(self, prompt_id: str, answer: str):
        with self.condition:
            if not self.pending or self.pending["id"] != prompt_id or self.answer is not None or self.cancelled:
                raise ValueError("This input request is no longer active. Refresh the task.")
            kind = self.pending["kind"]
            if kind == "secret":
                if not answer:
                    raise ValueError("Enter a value or cancel the task.")
                # Register before releasing the worker. No secret value enters history.
                self.redactor.add(answer)
            self.answer = answer
            self.store.event(self.id, {"kind": "input_received", "text": "Hidden value supplied" if kind == "secret" else
                                      "Approval submitted" if kind == "approval" else "Answer submitted", "time": now()})
            self.condition.notify_all()

    def cancel(self):
        with self.condition:
            self.cancelled = True
            self.answer = None
            self.condition.notify_all()
            if self.metadata["status"] == "queued":
                self.update("cancelled", "Cancelled before execution")
            elif self.metadata["status"] not in FINAL:
                self.emit("Cancellation requested. Future commands will stop; an already submitted cloud operation may continue.")

    def public(self):
        with self.condition:
            return dict(self.metadata, llm_usage=dict(self.metadata['llm_usage']),
                        pending=dict(self.pending) if self.pending and self.answer is None else None)


class BrowserExecutor(Executor):
    def __init__(self, settings: Settings, job: Job):
        self.job = job
        if not hasattr(self, 'command_prefix'):
            launch = cli_launcher('oci', settings.cli)
            if not launch and Path(settings.cli).suffix.lower() in {'.cmd', '.bat', '.ps1'}:
                raise ValueError('OCI CLI has no supported shell-free launcher for this OS.')
            self.command_prefix = launch or [settings.cli]
        super().__init__(settings, job.redactor, secret_prompt=lambda text: job.prompt("secret", text))

    def execute(self, step, results):
        if self.job.cancelled:
            raise EOFError("Task cancelled before the next command")
        return super().execute(step, results)

    def _process(self, args, timeout):
        if self.job.cancelled:
            raise EOFError("Task cancelled before the next command")
        # Mask sensitive option values as well as registered hidden inputs.
        safe_args, hide_next = [], False
        for arg in args:
            flag, separator, value = arg.partition("=")
            sensitive = flag.startswith("--") and bool(SENSITIVE_KEY.search(flag))
            safe_args.append("[REDACTED]" if hide_next else
                             flag + "=[REDACTED]" if sensitive and separator else self.redactor.text(arg))
            hide_next = sensitive and not separator
        prefix = getattr(self, 'command_prefix', [self.settings.cli])
        command = self.redactor.text(format_command([*prefix, *safe_args]))
        self.job.emit("Command: " + command)
        result = self._run_command(args, timeout)
        payload = self.redactor.clean({
            "command": command, "arguments": [*prefix, *safe_args],
            "exit_code": result.code, "category": result.category,
            "response": result.data, "stdout": result.stdout, "stderr": result.stderr,
            "output_note": result.output_note,
        })
        readable = response_preview(result) or "Command completed with no standard output."
        if result.stderr:
            readable += "\n\nStandard error:\n" + result.stderr
        self.job.store.event(self.job.id, {
            "kind": "command_result", "time": now(), "payload": payload,
            "text": self.redactor.text(readable),
        })
        return result

    def _run_command(self, args, timeout):
        try:
            result = subprocess.run([*self.command_prefix, *args], cwd=self.settings.root,
                                    env=self._environment(), stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                                    shell=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            return CommandResult(124, None, '', 'Command timed out. Cloud outcome is uncertain; reconcile before retrying.', 'uncertain')
        except OSError as exc:
            return CommandResult(127, None, '', self.redactor.text(str(exc)), 'local')
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError):
            data = None
        safe_data = self.redactor.clean(data)
        stdout = json.dumps(safe_data) if data is not None else self.redactor.text(result.stdout)
        return CommandResult(result.returncode, safe_data, stdout, self.redactor.text(result.stderr),
                             classify(result.returncode, result.stdout, result.stderr))


def default_planner(settings, executor, redactor):
    from fde_oci_agent.planner import Planner
    from .usage import UsageModel
    planner = Planner(settings, executor, redactor)
    planner.agent.instructions += OCI_PLANNING_HINTS
    planner.agent.instructions += f'\nExecution host OS: {host_os()}. Return cloud CLI argument arrays; do not generate shell or PowerShell wrapper commands.\n'
    planner.agent.model_settings = replace(planner.agent.model_settings, preserve_raw_usage=True)
    planner.agent.model = UsageModel(model=settings.model_name, openai_client=planner.client, job=executor.job)
    return planner


class Manager:
    def __init__(self, settings: Settings, state_dir: Path, planner_factory=default_planner,
                 executor_factory=BrowserExecutor, cloud_planner_factory=None, cloud_executor_factory=None,
                 compartment_lookup=lookup_compartment):
        self.settings, self.state_dir = settings, state_dir
        self.store = Store(state_dir / "web-history.sqlite3")
        self.planner_factory, self.executor_factory = planner_factory, executor_factory
        self.cloud_planner_factory, self.cloud_executor_factory = cloud_planner_factory, cloud_executor_factory
        self.compartment_lookup = compartment_lookup
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.queue = queue.Queue()
        self.stopping = False
        for record in self.store.listing():
            if record["status"] not in FINAL:
                record.update(status="interrupted", updated_at=now(), summary="Service restarted. Inspect cloud state before retrying; this task was not replayed.")
                self.store.save(record)
        self.worker = threading.Thread(target=self._worker, name="cloud-agent-worker", daemon=True)
        self.worker.start()

    def submit(self, request: str, *, profile: str, region: str, compartment_id: str = '', plan_only=False,
               cloud='oci', scope='') -> Job:
        with self.lock:
            if self.stopping:
                raise ValueError("Service is stopping")
            active = sum(job.metadata["status"] not in FINAL for job in self.jobs.values())
            if active >= 20:
                raise ValueError("The task queue is full")
            from .clouds import validate_context
            scope = scope or compartment_id
            validate_context(cloud, profile, region, scope)
            # Execution credentials never replace the OCI-hosted model's connection.
            selected = replace(self.settings, profile=profile, region=region, compartment_id=scope) if cloud == 'oci' else self.settings
            selected.connection()
            job = Job(self.store, request, selected, plan_only, cloud=cloud, profile=profile, region=region, scope=scope)
            self.jobs[job.id] = job
            self.queue.put(job)
            return job

    def _worker(self):
        while True:
            job = self.queue.get()
            if job is None:
                return
            if job.cancelled:
                continue
            try:
                asyncio.run(self._run(job))
            except BaseException as exc:
                # Keep a service alive after one task's failure, without stack/secret output.
                job.update("cancelled" if job.cancelled else "partially_completed" if job.metadata.get('may_have_changes') else "failed", job.redactor.text(str(exc))[:2000])
                job.emit(job.metadata["summary"])
            finally:
                with job.condition:
                    job.answer = None
                    job.pending = None
                    job.redactor.secrets.clear()

    async def _run(self, job: Job):
        root = self.state_dir / "jobs" / job.id
        settings = replace(job.settings, root=root)
        if job.metadata['cloud'] != 'oci':
            from .multicloud import CloudExecutor, CloudPlanner, run_cloud_workflow
            executor = (self.cloud_executor_factory or CloudExecutor)(settings, job)
            planner = None
            job.update('running', 'Planning with the OCI-hosted model for the selected cloud')
            try:
                planner = (self.cloud_planner_factory or CloudPlanner)(settings, executor, job.redactor)
                await run_cloud_workflow(settings, job, executor, planner)
            finally:
                executor.secret_values.clear()
                if planner is not None:
                    await planner.close()
            return
        executor = self.executor_factory(settings, job)
        planner = None
        job.update("running", "Planning with the OCI-hosted model")
        try:
            try:
                compartment = await asyncio.to_thread(self.compartment_lookup, settings)
                if compartment['id'] == job.metadata['compartment_id']:
                    with job.condition:
                        job.metadata['compartment_name'] = job.redactor.text(compartment['name'])
                        job.store.save(job.metadata)
                    job.emit(f"Compartment: {job.metadata['compartment_name']}\nOCID: {compartment['id']}")
            except Exception:
                job.emit('Compartment name unavailable; using OCID: ' + job.metadata['compartment_id'])
            planner = self.planner_factory(settings, executor, job.redactor)
            workflow = Workflow(settings, planner, executor, Audit(settings.state_dir, job.redactor), job.redactor,
                                ask=lambda text: job.prompt("approval" if text.startswith("Type ACCEPT") else "question", text),
                                emit=job.emit)
            outcome = await workflow.run(job.request, plan_only=job.plan_only)
            job.update(outcome.status, outcome.message)
        finally:
            executor.secret_values.clear()
            if planner is not None:
                await planner.close()

    def get(self, job_id: str):
        with self.lock:
            live = self.jobs.get(job_id)
            return live.public() if live else self.store.get(job_id)

    def respond(self, job_id: str, prompt_id: str, answer: str):
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise ValueError("This task has no active input request")
        job.respond(prompt_id, answer)

    def cancel(self, job_id: str):
        with self.lock:
            job = self.jobs.get(job_id)
        if job:
            job.cancel()

    def close(self):
        self.stopping = True
        with self.lock:
            for job in self.jobs.values():
                if job.metadata["status"] not in FINAL:
                    job.cancel()
        self.queue.put(None)
        self.worker.join(timeout=2)
