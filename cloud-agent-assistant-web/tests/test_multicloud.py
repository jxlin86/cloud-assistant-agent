import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from fde_oci_agent.schema import Plan, Step, Expectation, WaitFor
from cloud_agent_assistant_web.app import create_app
from cloud_agent_assistant_web.clouds import providers, validate_context, read_only, cli_launcher
from cloud_agent_assistant_web.jobs import Job, Store, Manager, FINAL
from cloud_agent_assistant_web.multicloud import CloudExecutor, run_cloud_workflow, CloudDocumentation
from test_service import settings, result, wait_for, reply


CONTEXT = {
    'aws': dict(profile='development', region='us-east-1', scope='123456789012'),
    'azure': dict(profile='default', region='eastus', scope='11111111-2222-3333-4444-555555555555'),
    'gcp': dict(profile='development', region='us-central1', scope='cloud-test-project'),
}
READS = {'aws': ['ec2', 'describe-instances'], 'azure': ['vm', 'list'], 'gcp': ['compute', 'instances', 'list']}
WRITES = {'aws': ['ec2', 'create-vpc'], 'azure': ['group', 'create'], 'gcp': ['compute', 'networks', 'create']}


class FakeDocs:
    async def fetch(self, command):
        return {'reference': 'Simulated official reference'}


class FakeCloudExecutor(CloudExecutor):
    calls = []

    def _run_command(self, args, timeout):
        self.calls.append((self.cloud, list(args)))
        if args[:2] == ['sts', 'get-caller-identity']:
            return result({'Account': self.context['scope']})
        if args[:2] == ['account', 'show']:
            return result({'id': self.context['scope']})
        if args[:2] == ['projects', 'describe']:
            return result({'projectId': self.context['scope']})
        if args[0] == 'fail':
            from fde_oci_agent.executor import CommandResult
            return CommandResult(124, None, '', 'Simulated timeout', 'uncertain')
        return result({'items': [{'id': 'resource-test', 'state': 'READY'}]})


class FakeCloudPlanner:
    mode = 'read'

    def __init__(self, settings, executor, redactor):
        # Model authentication must keep the OCI profile rather than the cloud profile.
        assert settings.profile == 'OCIPRJ'
        self.executor, self.docs, self.calls = executor, FakeDocs(), 0

    async def plan(self, request, history):
        self.calls += 1
        self.executor.job.record_usage({'prompt_tokens': 100, 'completion_tokens': 20})
        if self.calls == 1 and self.mode in {'write', 'secret'}:
            args = ['--password', '{{secret:ADMIN_PASSWORD}}'] if self.mode == 'secret' else []
            return Plan(summary='Create test resource', phase='action', changes=['Create a cloud resource'],
                        steps=[Step(id='create', purpose='Create resource', command=WRITES[self.executor.cloud], arguments=args)])
        if self.calls == 1 or self.mode in {'write', 'secret'} and self.calls == 2:
            return Plan(summary='Verify resource', phase='verification', steps=[Step(
                id='verify', purpose='Read resource', command=READS[self.executor.cloud],
                expect=[Expectation(path='items.0.id', operator='equals', value='resource-test')])])
        return Plan(summary='Resource verified', phase='complete')

    async def close(self):
        pass


@pytest.fixture
def cloud_client(settings, tmp_path, monkeypatch):
    monkeypatch.setattr('cloud_agent_assistant_web.multicloud.cli_launcher', lambda cloud: ['FAKE-CLI'])
    FakeCloudExecutor.calls = []
    FakeCloudPlanner.mode = 'read'
    def factory(base, state):
        return Manager(base, state, cloud_planner_factory=FakeCloudPlanner, cloud_executor_factory=FakeCloudExecutor)
    with TestClient(create_app(settings, tmp_path / 'state', manager_factory=factory, hosts=('testserver',))) as client:
        client.csrf = client.get('/api/bootstrap').json()['csrf']
        yield client


def submit_cloud(client, cloud, **updates):
    body = dict(request='List cloud resources', cloud=cloud, **CONTEXT[cloud])
    body.update(updates)
    response = client.post('/api/jobs', json=body, headers={'X-CSRF-Token': client.csrf})
    assert response.status_code == 201, response.text
    return response.json()['id']


@pytest.mark.parametrize('cloud', CONTEXT)
def test_cloud_routes_reads_scopes_outputs_and_usage(cloud_client, cloud):
    job_id = submit_cloud(cloud_client, cloud)
    value = wait_for(cloud_client, job_id, lambda job: job['status'] in FINAL)
    assert value['job']['status'] == 'succeeded', value
    assert value['job']['cloud'] == cloud
    assert value['job']['scope'] == CONTEXT[cloud]['scope']
    assert value['job']['compartment_id'] == ''
    assert value['job']['llm_usage']['input_tokens'] == 200
    events = [event for event in value['events'] if event['kind'] == 'command_result']
    assert events and all(event['payload']['exit_code'] == 0 for event in events)
    assert any(event['payload']['response'].get('items') for event in events)
    assert all(provider == cloud for provider, args in FakeCloudExecutor.calls)
    for provider, args in FakeCloudExecutor.calls:
        flag = {'aws': '--profile', 'azure': '--subscription', 'gcp': '--project'}[cloud]
        expected = CONTEXT[cloud]['profile' if cloud == 'aws' else 'scope']
        assert args[args.index(flag) + 1] == expected
        assert '--compartment-id' not in args


@pytest.mark.parametrize('cloud', CONTEXT)
@pytest.mark.parametrize('accept', [True, False])
def test_cloud_writes_wait_for_acceptance(cloud_client, cloud, accept):
    FakeCloudPlanner.mode = 'write'
    job_id = submit_cloud(cloud_client, cloud, request='Create cloud resource')
    pending = wait_for(cloud_client, job_id, lambda job: job['status'] == 'awaiting_approval')['job']['pending']
    assert not any(args[:len(WRITES[cloud])] == WRITES[cloud] for provider, args in FakeCloudExecutor.calls)
    reply(cloud_client, job_id, pending, 'ACCEPT' if accept else 'DECLINE')
    done = wait_for(cloud_client, job_id, lambda job: job['status'] in FINAL)
    assert done['job']['status'] == ('succeeded' if accept else 'cancelled')
    writes = [args for provider, args in FakeCloudExecutor.calls if args[:len(WRITES[cloud])] == WRITES[cloud]]
    assert len(writes) == int(accept)


@pytest.mark.parametrize('cloud', CONTEXT)
def test_cloud_plan_only_does_not_run_cli(cloud_client, cloud):
    job_id = submit_cloud(cloud_client, cloud, plan_only=True)
    assert wait_for(cloud_client, job_id, lambda job: job['status'] in FINAL)['job']['status'] == 'planned'
    assert not FakeCloudExecutor.calls


def test_cloud_hidden_input_is_not_persisted(cloud_client):
    FakeCloudPlanner.mode = 'secret'
    job_id = submit_cloud(cloud_client, 'aws', request='Create using {{secret:ADMIN_PASSWORD}}')
    pending = wait_for(cloud_client, job_id, lambda job: job['status'] == 'awaiting_approval')['job']['pending']
    reply(cloud_client, job_id, pending, 'ACCEPT')
    pending = wait_for(cloud_client, job_id, lambda job: job['status'] == 'awaiting_secret')['job']['pending']
    reply(cloud_client, job_id, pending, 'hidden-multicloud-password')
    done = wait_for(cloud_client, job_id, lambda job: job['status'] in FINAL)
    assert done['job']['status'] == 'succeeded'
    assert 'hidden-multicloud-password' not in json.dumps(done)
    assert b'hidden-multicloud-password' not in cloud_client.app.state.manager.store.path.read_bytes()


@pytest.mark.parametrize('cloud', CONTEXT)
def test_api_rejects_oci_scope_for_other_clouds(cloud_client, cloud):
    body = dict(request='List resources', cloud=cloud, **CONTEXT[cloud])
    body['scope'] = 'ocid1.compartment.test'
    assert cloud_client.post('/api/jobs', json=body, headers={'X-CSRF-Token': cloud_client.csrf}).status_code == 400


@pytest.mark.parametrize('cloud', CONTEXT)
@pytest.mark.parametrize('arguments', [
    ['--profile', 'other'], ['--subscription=other'], ['--project', 'other'], ['--pro', 'other'],
    ['--endpoint-url=https://example.com'], ['--flags-file=creds.yaml'], ['--output', 'table'],
    ['--cli-input-json', '{}'], ['--file', 'private.txt'], ['-p', 'other'], ['@private.json'],
    ['--source', '.'], ['--password', 'plaintext'],
])
def test_context_and_local_file_overrides_are_rejected(settings, tmp_path, cloud, arguments):
    job = Job(Store(tmp_path / 'test.sqlite3'), 'Test', settings, False, cloud=cloud, **CONTEXT[cloud])
    executor = CloudExecutor(settings, job)
    with pytest.raises(ValueError):
        executor.validate(Step(id='test', purpose='Test', command=READS[cloud], arguments=arguments))


@pytest.mark.parametrize('cloud,command', [('aws', ['s3', 'cp']), ('azure', ['extension', 'add']),
                                         ('gcp', ['auth', 'login']), ('gcp', ['compute', 'ssh'])])
def test_local_or_interactive_commands_are_rejected(settings, tmp_path, cloud, command):
    job = Job(Store(tmp_path / 'test.sqlite3'), 'Test', settings, False, cloud=cloud, **CONTEXT[cloud])
    with pytest.raises(ValueError):
        CloudExecutor(settings, job).validate(Step(id='test', purpose='Test', command=command))


@pytest.mark.parametrize('cloud', CONTEXT)
def test_scope_identity_mismatch_stops_before_planning(settings, tmp_path, monkeypatch, cloud):
    monkeypatch.setattr('cloud_agent_assistant_web.multicloud.cli_launcher', lambda cloud: ['FAKE-CLI'])
    job = Job(Store(tmp_path / 'test.sqlite3'), 'Test', settings, False, cloud=cloud, **CONTEXT[cloud])
    executor = CloudExecutor(settings, job)
    executor._run_command = lambda args, timeout: result({'Account': 'other', 'id': 'other', 'projectId': 'other'})
    with pytest.raises(ValueError, match='does not match'):
        asyncio.run(executor.preflight())


@pytest.mark.parametrize('cloud', CONTEXT)
def test_missing_cli_is_explicit(settings, tmp_path, monkeypatch, cloud):
    monkeypatch.setattr('cloud_agent_assistant_web.multicloud.cli_launcher', lambda cloud: None)
    job = Job(Store(tmp_path / 'test.sqlite3'), 'Test', settings, False, cloud=cloud, **CONTEXT[cloud])
    with pytest.raises(ValueError, match='not installed'):
        asyncio.run(CloudExecutor(settings, job).preflight())


def test_read_detection_requires_approval_for_sensitive_and_write_commands():
    assert read_only('aws', ['ec2', 'describe-instances'])
    assert read_only('azure', ['vm', 'list'])
    assert read_only('gcp', ['compute', 'instances', 'list'])
    for cloud, command in [('aws', ['ec2', 'run-instances']), ('azure', ['keyvault', 'secret', 'show']),
                           ('gcp', ['secrets', 'list']), ('azure', ['storage', 'account', 'show-connection-string'])]:
        assert not read_only(cloud, command)


@pytest.mark.parametrize('cloud', CONTEXT)
def test_native_process_has_no_shell_and_pinned_context(settings, tmp_path, monkeypatch, cloud):
    import subprocess
    job = Job(Store(tmp_path / 'test.sqlite3'), 'Test', settings, False, cloud=cloud, **CONTEXT[cloud])
    executor = CloudExecutor(settings, job)
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{"items":[]}', '')
    monkeypatch.setattr('cloud_agent_assistant_web.multicloud.subprocess.run', run)
    monkeypatch.setenv('AWS_ENDPOINT_URL', 'https://override.invalid')
    monkeypatch.setenv('CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT', 'other')
    response = executor.execute(Step(id='read', purpose='Read', command=READS[cloud]), {})
    assert response.data == {'items': []}
    argv, kwargs = seen[0]
    assert kwargs['shell'] is False
    assert kwargs['stdin'] == subprocess.DEVNULL
    assert argv[-len(executor.flags()):] == executor.flags()
    if cloud == 'aws':
        assert 'AWS_ENDPOINT_URL' not in kwargs['env']
    if cloud == 'gcp':
        assert 'CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT' not in kwargs['env']


@pytest.mark.parametrize('cloud', CONTEXT)
def test_real_cloud_planner_uses_selected_context_and_tracks_tool_turns(settings, tmp_path, monkeypatch, cloud):
    import httpx
    from openai import AsyncOpenAI
    from cloud_agent_assistant_web.multicloud import CloudPlanner
    job = Job(Store(tmp_path / 'test.sqlite3'), 'List resources', settings, False, cloud=cloud, **CONTEXT[cloud])
    monkeypatch.setattr('cloud_agent_assistant_web.multicloud.cli_launcher', lambda cloud: ['FAKE-CLI'])
    monkeypatch.setattr(CloudDocumentation, 'fetch', FakeDocs.fetch)
    calls = []
    verification = Plan(summary='Read inventory', phase='verification', steps=[Step(
        id='inventory', purpose='List resources', command=READS[cloud],
        expect=[Expectation(path='items', operator='exists')])])
    def respond(request):
        calls.append(json.loads(request.content))
        index = len(calls)
        message = {'role': 'assistant', 'content': verification.model_dump_json() if index == 2 else
                   Plan(summary='Inventory verified', phase='complete').model_dump_json()}
        if index == 1:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'reference', 'type': 'function', 'function': {'name': 'get_cloud_cli_reference',
                 'arguments': json.dumps({'command': READS[cloud]})}}]}
        return httpx.Response(200, json={'id': str(index), 'object': 'chat.completion', 'created': 1,
            'model': settings.model_name, 'choices': [{'index': 0, 'message': message,
                'finish_reason': 'tool_calls' if index == 1 else 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}})
    async def exercise():
        client = AsyncOpenAI(api_key='fake', base_url='http://model.test/v1', max_retries=0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        monkeypatch.setattr('cloud_agent_assistant_web.multicloud.build_client', lambda settings: client)
        executor = FakeCloudExecutor(settings, job)
        planner = CloudPlanner(settings, executor, job.redactor)
        try:
            await run_cloud_workflow(settings, job, executor, planner)
        finally:
            await planner.close()
    asyncio.run(exercise())
    assert job.public()['status'] == 'succeeded'
    assert job.public()['llm_usage'] == dict(input_tokens=300, output_tokens=60, total_tokens=360,
                                           requests=3, missing_usage_requests=0)
    user_message = next(message for message in calls[0]['messages'] if message['role'] == 'user')
    context = json.loads(user_message['content'])['connection']
    assert context == dict(cloud=cloud, **CONTEXT[cloud])
    assert 'default_compartment' not in context


@pytest.mark.parametrize('cloud', CONTEXT)
def test_uncertain_write_is_not_replayed(settings, tmp_path, monkeypatch, cloud):
    monkeypatch.setattr('cloud_agent_assistant_web.multicloud.cli_launcher', lambda cloud: ['FAKE-CLI'])
    job = Job(Store(tmp_path / 'test.sqlite3'), 'Create resource', settings, False, cloud=cloud, **CONTEXT[cloud])
    job.prompt = lambda kind, text: 'ACCEPT'
    executor = FakeCloudExecutor(settings, job)
    FakeCloudExecutor.calls = []
    class Planner:
        docs = FakeDocs()
        calls = 0
        async def plan(self, request, history):
            self.calls += 1
            return Plan(summary='Create', phase='action', steps=[Step(id='create', purpose='Create', command=['fail', 'create-resource'])])
    planner = Planner()
    asyncio.run(run_cloud_workflow(settings, job, executor, planner))
    assert job.public()['status'] == 'partially_completed'
    assert planner.calls == 1
    assert sum(args[0] == 'fail' for provider, args in FakeCloudExecutor.calls) == 1


def test_polling_is_read_only_and_never_repeats_submission(settings, tmp_path, monkeypatch):
    settings = replace(settings, poll_interval=0)
    monkeypatch.setattr('cloud_agent_assistant_web.multicloud.cli_launcher', lambda cloud: ['FAKE-CLI'])
    job = Job(Store(tmp_path / 'test.sqlite3'), 'Create VPC', settings, False, cloud='aws', **CONTEXT['aws'])
    job.prompt = lambda kind, text: 'ACCEPT'
    executor = FakeCloudExecutor(settings, job)
    calls = []
    def process(args, timeout):
        calls.append(args)
        if args[:2] == ['sts', 'get-caller-identity']:
            return result({'Account': CONTEXT['aws']['scope']})
        if args[:2] == ['ec2', 'create-vpc']:
            return result({'Vpc': {'VpcId': 'vpc-test', 'State': 'pending'}})
        count = sum(args[:2] == ['ec2', 'describe-vpcs'] for args in calls)
        return result({'Vpcs': [{'VpcId': 'vpc-test', 'State': 'pending' if count == 1 else 'available'}]})
    executor._run_command = process
    class Planner:
        docs = FakeDocs()
        calls = 0
        async def plan(self, request, history):
            self.calls += 1
            if self.calls == 1:
                return Plan(summary='Create VPC', phase='action', steps=[Step(
                    id='create', purpose='Create VPC', command=['ec2', 'create-vpc'], arguments=['--cidr-block', '10.0.0.0/16'],
                    wait_for=WaitFor(command=['ec2', 'describe-vpcs'], arguments=['--vpc-ids', '{{steps.create.Vpc.VpcId}}'],
                                     state_path='Vpcs.0.State', success_states=['available'], failure_states=['failed']))])
            if self.calls == 2:
                return Plan(summary='Verify VPC', phase='verification', steps=[Step(
                    id='verify', purpose='Verify VPC', command=['ec2', 'describe-vpcs'], arguments=['--vpc-ids', 'vpc-test'],
                    expect=[Expectation(path='Vpcs.0.State', operator='equals', value='available')])])
            return Plan(summary='VPC verified', phase='complete')
    asyncio.run(run_cloud_workflow(settings, job, executor, Planner()))
    assert job.public()['status'] == 'succeeded'
    assert sum(args[:2] == ['ec2', 'create-vpc'] for args in calls) == 1
    assert sum(args[:2] == ['ec2', 'describe-vpcs'] for args in calls) == 3
    assert any('vpc-test' in args for args in calls if args[:2] == ['ec2', 'describe-vpcs'])


@pytest.mark.parametrize('cloud,arg', [
    ('aws', 'arn:aws:ec2:us-east-1:999999999999:instance/i-test'),
    ('azure', '/subscriptions/99999999-2222-3333-4444-555555555555/resourceGroups/test'),
    ('gcp', 'projects/other-project/zones/us-central1-a/instances/test'),
])
def test_explicit_cross_scope_targets_are_rejected(settings, tmp_path, cloud, arg):
    job = Job(Store(tmp_path / 'test.sqlite3'), 'Test', settings, False, cloud=cloud, **CONTEXT[cloud])
    with pytest.raises(ValueError, match='outside'):
        CloudExecutor(settings, job).validate(Step(id='read', purpose='Read', command=READS[cloud], arguments=['--ids', arg]))


def test_windows_cli_launchers_bypass_batch_shells(tmp_path, monkeypatch):
    import os
    if os.name != 'nt':
        pytest.skip('Windows launcher layout')
    azure = tmp_path / 'CLI2' / 'wbin' / 'az.cmd'
    azure.parent.mkdir(parents=True)
    azure.touch()
    python = azure.parent.parent / 'python.exe'
    python.touch()
    monkeypatch.setattr('cloud_agent_assistant_web.clouds.shutil.which', lambda name: str(azure))
    assert cli_launcher('azure') == [str(python), '-I', '-m', 'azure.cli']
    gcloud = tmp_path / 'google-cloud-sdk' / 'bin' / 'gcloud.cmd'
    gcloud.parent.mkdir(parents=True)
    gcloud.touch()
    script = gcloud.with_suffix('.py')
    script.touch()
    python = gcloud.parent.parent / 'platform' / 'bundledpython' / 'python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr('cloud_agent_assistant_web.clouds.shutil.which', lambda name: str(gcloud))
    assert cli_launcher('gcp') == [str(python), str(script)]


def test_additional_cloud_credential_fields_are_redacted(settings, tmp_path):
    job = Job(Store(tmp_path / 'test.sqlite3'), 'Test', settings, False)
    assert job.redactor.clean({'connectionString': 'private', 'primaryAccountKey': 'private',
                               'AccessKeyId': 'private', 'resource': {'name': 'visible'}}) == {
        'connectionString': '[REDACTED]', 'primaryAccountKey': '[REDACTED]', 'AccessKeyId': '[REDACTED]',
        'resource': {'name': 'visible'}}
