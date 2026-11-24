import asyncio
import json
import threading
import time
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from fde_oci_agent.config import Settings
from fde_oci_agent.executor import CommandResult, Executor
from fde_oci_agent.schema import Expectation, Plan, Step
from cloud_agent_assistant_web.app import create_app
from cloud_agent_assistant_web.jobs import Manager, BrowserExecutor, Job, Store, FINAL
from cloud_agent_assistant_web.jobs import default_planner


@pytest.fixture
def settings(tmp_path):
    profile = tmp_path / "oci-config"
    profile.write_text("[OCIPRJ]\nregion=us-ashburn-1\ntenancy=ocid1.tenancy.test\n")
    return Settings(root=tmp_path, config_file=profile, profile="OCIPRJ", region="us-ashburn-1",
                    compartment_id="ocid1.compartment.test", model_name="openai.gpt-oss-120b",
                    model_region="us-chicago-1", model_project="", model_auth="profile", cli="oci.exe")


def result(data):
    return CommandResult(0, data, json.dumps(data), "", "success")


def wait_for(client, job_id, predicate):
    for _ in range(300):
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        value = response.json()
        if predicate(value["job"]):
            return value
        time.sleep(.01)
    pytest.fail("Task did not reach expected status: " + str(value))


class FakeDocs:
    async def fetch(self, command):
        return {"url": "https://docs.oracle.com/reference"}


class FakePlanner:
    mode = "read"
    def __init__(self, settings, executor, redactor):
        self.docs = FakeDocs()
        self.executor = executor
        self.calls = 0
    async def plan(self, request, history):
        self.calls += 1
        if self.mode == "clarify" and self.calls == 1:
            return Plan(summary="Need a project name", phase="discovery", questions=["Which project name?"])
        if self.mode == "write" and self.calls == 1:
            return Plan(summary="Create project", phase="action", changes=["Create a project"], steps=[
                Step(id="create", purpose="Create project", command=["devops", "project", "create"],
                     arguments=["--name", "FDE-Service"], expect=[Expectation(path="data.id", operator="exists")])])
        if self.mode == "secret" and self.calls == 1:
            return Plan(summary="Create resource with hidden input", phase="action", steps=[
                Step(id="create", purpose="Create simulated resource", command=["devops", "project", "create"],
                     arguments=["--admin-password", "{{secret:ADMIN_PASSWORD}}"], expect=[Expectation(path="data.id", operator="exists")])])
        if self.calls == 1 or (self.mode in {"write", "secret", "clarify"} and self.calls == 2):
            return Plan(summary="Verify project", phase="verification", steps=[
                Step(id="check", purpose="Read project", command=["devops", "project", "get"],
                     arguments=["--project-id", "project-test"], expect=[Expectation(path="data.id", operator="equals", value="project-test")])])
        return Plan(summary="Project verified", phase="complete")
    async def close(self):
        pass


class FakeExecutor(BrowserExecutor):
    calls = []
    def _run_command(self, args, timeout):
        if self.job.cancelled:
            raise EOFError("Cancelled")
        self.calls.append(args)
        # Simulate a response containing a sensitive field; production does this
        # redaction in _process, while the test checks all web publication paths.
        data = self.redactor.clean({"data": {"id": "project-test", "display-name": "FDE-Service", "password": "not-a-real-password"}})
        return result(data)


@pytest.fixture
def client(settings, tmp_path):
    FakeExecutor.calls = []
    FakePlanner.mode = "read"
    def factory(base, state):
        return Manager(base, state, planner_factory=FakePlanner, executor_factory=FakeExecutor,
                       compartment_lookup=lambda settings: dict(id=settings.compartment_id, name='fde-sandbox'))
    app = create_app(settings, tmp_path / "state", manager_factory=factory, hosts=("testserver",),
                     compartment_lookup=lambda settings, ocid: dict(id=ocid, name='fde-sandbox'))
    with TestClient(app) as browser:
        browser.csrf = browser.get("/api/bootstrap").json()["csrf"]
        yield browser


def submit(client, request="List projects", **updates):
    body = dict(request=request, profile="OCIPRJ", region="us-ashburn-1", compartment_id="ocid1.compartment.test", **updates)
    response = client.post("/api/jobs", json=body, headers={"X-CSRF-Token": client.csrf})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def reply(client, job_id, prompt, answer):
    return client.post(f"/api/jobs/{job_id}/respond", json={"prompt_id": prompt["id"], "answer": answer},
                       headers={"X-CSRF-Token": client.csrf})


def test_browser_assets_config_and_health_exclude_credentials(client):
    response = client.get("/")
    assert response.status_code == 200 and "Cloud Agent Assistant" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    config = client.get("/api/bootstrap").json()
    assert config["core_version"] == "0.1.4" and config["model_region"] == "us-chicago-1"
    assert set(config["profiles"]["OCIPRJ"]) == {"region", "tenancy"}
    assert client.get("/health").json()["service"] == "cloud-agent-assistant-web"


def test_read_only_task_runs_without_approval_and_has_human_readable_results(client):
    job_id = submit(client)
    value = wait_for(client, job_id, lambda job: job["status"] in FINAL)
    assert value["job"]["status"] == "succeeded" and value["job"]["pending"] is None
    assert value['job']['compartment_name'] == 'fde-sandbox'
    assert client.app.state.manager.store.get(job_id)['compartment_name'] == 'fde-sandbox'
    text = "\n".join(event.get("text", "") for event in value["events"])
    assert "FDE-Service" in text and '"data"' not in text
    assert len(FakeExecutor.calls) == 1
    commands = [event for event in value['events'] if event['kind'] == 'command_result']
    assert len(commands) == 1
    command = commands[0]
    assert 'devops project get' in command['payload']['command']
    assert command['payload']['response']['data']['display-name'] == 'FDE-Service'
    assert command['payload']['exit_code'] == 0
    assert 'FDE-Service' in command['text']
    assert 'not-a-real-password' not in json.dumps(command)
    stored = client.app.state.manager.store.events(job_id, 0)
    assert command in stored


@pytest.mark.parametrize("accept", [True, False])
def test_changes_require_browser_acceptance_before_any_command(client, accept):
    FakePlanner.mode = "write"
    job_id = submit(client, "Create project")
    value = wait_for(client, job_id, lambda job: job["status"] == "awaiting_approval")
    assert not FakeExecutor.calls
    prompt = value["job"]["pending"]
    assert prompt["kind"] == "approval"
    assert reply(client, job_id, prompt, "ACCEPT" if accept else "DECLINE").status_code == 200
    done = wait_for(client, job_id, lambda job: job["status"] in FINAL)
    assert done["job"]["status"] == ("succeeded" if accept else "cancelled")
    assert len(FakeExecutor.calls) == (2 if accept else 0)
    assert reply(client, job_id, prompt, "ACCEPT").status_code == 409


def test_hidden_secret_never_appears_in_web_history_or_persistent_audit(client):
    FakePlanner.mode = "secret"
    job_id = submit(client, "Create resource using {{secret:ADMIN_PASSWORD}}")
    approval = wait_for(client, job_id, lambda job: job["status"] == "awaiting_approval")["job"]["pending"]
    reply(client, job_id, approval, "ACCEPT")
    pending = wait_for(client, job_id, lambda job: job["status"] == "awaiting_secret")["job"]["pending"]
    assert pending["kind"] == "secret" and not FakeExecutor.calls
    secret = "test-browser-hidden-Value-123!"
    assert reply(client, job_id, pending, secret).status_code == 200
    done = wait_for(client, job_id, lambda job: job["status"] in FINAL)
    assert done["job"]["status"] == "succeeded"
    assert secret not in json.dumps(done) and secret not in client.get("/api/jobs").text
    manager = client.app.state.manager
    # Raw SQLite pages must not retain the hidden value either.
    for path in manager.state_dir.rglob("*.sqlite3"):
        assert secret.encode() not in path.read_bytes()
    assert secret in FakeExecutor.calls[0]  # It reached only the simulated CLI argv.


def test_clarification_and_plan_only_paths(client):
    FakePlanner.mode = "clarify"
    job_id = submit(client, "Help with my project", plan_only=True)
    question = wait_for(client, job_id, lambda job: job["status"] == "awaiting_answer")["job"]["pending"]
    assert question["kind"] == "question"
    reply(client, job_id, question, "FDE-Service")
    done = wait_for(client, job_id, lambda job: job["status"] in FINAL)
    assert done["job"]["status"] == "planned" and not FakeExecutor.calls


def test_cancellation_releases_waiting_approval_without_execution(client):
    FakePlanner.mode = "write"
    job_id = submit(client, "Create project")
    wait_for(client, job_id, lambda job: job["status"] == "awaiting_approval")
    response = client.post(f"/api/jobs/{job_id}/cancel", json={}, headers={"X-CSRF-Token": client.csrf})
    assert response.status_code == 200
    assert wait_for(client, job_id, lambda job: job["status"] in FINAL)["job"]["status"] == "cancelled"
    assert not FakeExecutor.calls


def test_queue_is_serial_and_tasks_keep_separate_state(client):
    FakePlanner.mode = "write"
    first = submit(client, "First project")
    wait_for(client, first, lambda job: job["status"] == "awaiting_approval")
    second = submit(client, "Second project")
    assert client.get(f"/api/jobs/{second}").json()["job"]["status"] == "queued"
    client.post(f"/api/jobs/{first}/cancel", json={}, headers={"X-CSRF-Token": client.csrf})
    wait_for(client, second, lambda job: job["status"] == "awaiting_approval")
    assert not FakeExecutor.calls


def test_service_restart_marks_unfinished_tasks_interrupted_and_never_replays(settings, tmp_path):
    state_dir = tmp_path / "restart"
    store = Store(state_dir / "web-history.sqlite3")
    value = Job(store, "Create resource", settings, False)
    value.update("running")
    manager = Manager(settings, state_dir, planner_factory=lambda *args: pytest.fail("Unexpected replay"))
    try:
        assert manager.get(value.id)["status"] == "interrupted"
        assert "not replayed" in manager.get(value.id)["summary"]
    finally:
        manager.close()


@pytest.mark.parametrize("headers", [{}, {"X-CSRF-Token": "wrong"}, {"Origin": "https://evil.example"},
                                     {"Sec-Fetch-Site": "cross-site"}, {"Host": "evil.example"}])
def test_cross_site_and_unauthenticated_writes_cannot_queue_commands(client, headers):
    response = client.post("/api/jobs", json={}, headers=headers)
    assert response.status_code in {400, 403}
    assert not FakeExecutor.calls


def test_invalid_profile_request_and_extra_connection_fields_rejected(client):
    base = dict(request="List projects", profile="missing", region="us-ashburn-1", compartment_id="ocid1.compartment.test")
    assert client.post("/api/jobs", json=base, headers={"X-CSRF-Token": client.csrf}).status_code == 400
    base.update(profile="OCIPRJ", endpoint="https://evil.example")
    assert client.post("/api/jobs", json=base, headers={"X-CSRF-Token": client.csrf}).status_code == 400
    assert not FakeExecutor.calls


@pytest.mark.parametrize('response', [
    CommandResult(1, None, '', 'Permission denied', 'permission'),
    CommandResult(124, None, '', 'Command timed out', 'uncertain'),
    CommandResult(0, [], '[]', 'A runtime warning', 'success'),
    CommandResult(0, False, 'false', '', 'success'),
    CommandResult(0, None, '', '', 'success'),
    result({'data': [{'id': str(index)} for index in range(2000)]}),
])
def test_command_views_preserve_output_errors_and_redact_arguments(settings, tmp_path, monkeypatch, response):
    job = Job(Store(tmp_path / 'events.sqlite3'), 'Test command views', settings, False)
    executor = BrowserExecutor(settings, job)
    monkeypatch.setattr(BrowserExecutor, '_run_command', lambda self, args, timeout: response)
    job.redactor.add('hidden-value-123')
    args = ['devops', 'project', 'get', '--admin-password', 'literal-password-123',
            '--api-key=inline-value-123', '--name', 'hidden-value-123']
    assert executor._process(args, 10) is response
    # Reopen the store to check the exact persisted event, including large data.
    events = Store(job.store.path).events(job.id, 0)
    encoded = json.dumps(events)
    for secret in ('literal-password-123', 'inline-value-123', 'hidden-value-123'):
        assert secret not in encoded
    event = events[-1]
    assert event['kind'] == 'command_result'
    assert event['payload']['response'] == response.data
    assert event['payload']['stdout'] == response.stdout
    assert event['payload']['stderr'] == response.stderr
    assert event['payload']['exit_code'] == response.code
    assert event['text']
    if response.stderr:
        assert response.stderr in event['text']


def test_usage_counts_tool_turns_and_invalid_plan_retries(settings, tmp_path, monkeypatch):
    import httpx
    from openai import AsyncOpenAI
    import fde_oci_agent.planner as core

    job = Job(Store(tmp_path / 'usage.sqlite3'), 'Test usage', settings, False)
    executor = BrowserExecutor(settings, job)
    usages = [(120, 25), (200, 45), (15, 2), (30, 4)]
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        index = len(calls) - 1
        incoming, outgoing = usages[index]
        message = {'role': 'assistant', 'content': json.dumps({'summary': 'Done', 'phase': 'complete'})}
        if index == 0:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'schema-test', 'type': 'function', 'function': {
                    'name': 'get_oci_response_schema', 'arguments': json.dumps({'command': ['iam', 'tenancy', 'get']})}}]}
        if index == 2:
            message['content'] = 'Invalid plan; retry me'
        return httpx.Response(200, json={
            'id': f'completion-{index}', 'object': 'chat.completion', 'created': 1, 'model': settings.model_name,
            'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if index == 0 else 'stop'}],
            'usage': {'prompt_tokens': incoming, 'completion_tokens': outgoing, 'total_tokens': incoming + outgoing},
        })

    async def exercise():
        client = AsyncOpenAI(api_key='fake', base_url='http://model.test/v1', max_retries=0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        monkeypatch.setattr(core, 'build_client', lambda settings: client)
        planner = default_planner(settings, executor, job.redactor)
        try:
            await planner.plan('List projects', [])
            assert job.public()['llm_usage']['requests'] == 2
            await planner.plan('List projects', [])
        finally:
            await planner.close()
    asyncio.run(exercise())
    expected = dict(input_tokens=365, output_tokens=76, total_tokens=441, requests=4, missing_usage_requests=0)
    assert job.public()['llm_usage'] == expected
    assert Store(job.store.path).get(job.id)['llm_usage'] == expected


def test_missing_usage_and_real_zero_counts_are_distinct(settings, tmp_path):
    job = Job(Store(tmp_path / 'usage.sqlite3'), 'Test usage', settings, False)
    for raw in (None, {}, {'prompt_tokens': 4}, {'prompt_tokens': -1, 'completion_tokens': 0}):
        job.record_usage(raw)
    assert job.public()['llm_usage']['missing_usage_requests'] == 4
    job.record_usage({'prompt_tokens': 0, 'completion_tokens': 0})
    job.record_usage({'input_tokens': 6, 'output_tokens': 2})
    assert job.public()['llm_usage'] == dict(input_tokens=6, output_tokens=2, total_tokens=8,
                                           requests=6, missing_usage_requests=4)


def test_failed_model_call_records_unavailable_usage(settings, tmp_path, monkeypatch):
    from agents import OpenAIChatCompletionsModel
    from cloud_agent_assistant_web.usage import UsageModel
    job = Job(Store(tmp_path / 'usage.sqlite3'), 'Test failure', settings, False)
    async def fail(*args, **kwargs):
        raise RuntimeError('Simulated transport failure')
    monkeypatch.setattr(OpenAIChatCompletionsModel, 'get_response', fail)
    model = UsageModel(model='fake', openai_client=None, job=job)
    with pytest.raises(RuntimeError, match='Simulated'):
        asyncio.run(model.get_response())
    assert job.public()['llm_usage']['requests'] == 1
    assert job.public()['llm_usage']['missing_usage_requests'] == 1


def test_compartment_name_api_returns_name_and_ocid(client):
    response = client.get('/api/oci/compartment', params=dict(profile='OCIPRJ', region='us-ashburn-1', ocid='ocid1.compartment.test'))
    assert response.status_code == 200
    assert response.json() == dict(id='ocid1.compartment.test', name='fde-sandbox')
    assert client.get('/api/oci/compartment?profile=missing').status_code == 400


def test_compartment_name_failure_does_not_expose_credentials(settings, tmp_path):
    def denied(*args):
        raise RuntimeError('secret diagnostic must stay private')
    with TestClient(create_app(settings, tmp_path / 'state', compartment_lookup=denied, hosts=('testserver',))) as browser:
        response = browser.get('/api/oci/compartment')
        assert response.status_code == 400
        assert 'secret diagnostic' not in response.text


@pytest.mark.parametrize('ocid', ['ocid1.compartment.lookuptest', 'ocid1.tenancy.lookuptest'])
def test_compartment_sdk_lookup_and_cache(settings, monkeypatch, ocid):
    import oci
    from types import SimpleNamespace
    from cloud_agent_assistant_web.compartments import lookup_compartment
    calls = []
    monkeypatch.setattr(oci.config, 'from_file', lambda *args: {})
    def fetch(value):
        calls.append(value)
        return SimpleNamespace(data=SimpleNamespace(id=value, name='Resolved compartment'))
    client = SimpleNamespace(get_compartment=fetch, get_tenancy=fetch,
                             base_client=SimpleNamespace(session=SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr(oci.identity, 'IdentityClient', lambda *args, **kwargs: client)
    expected = dict(id=ocid, name='Resolved compartment')
    assert lookup_compartment(settings, ocid) == expected
    assert lookup_compartment(settings, ocid) == expected
    assert calls == [ocid]
