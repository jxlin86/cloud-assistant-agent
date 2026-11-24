"""Execution limits must allow reporting, never unbounded resource work."""
import asyncio
from dataclasses import replace

import pytest
from fde_oci_agent.audit import Audit
from fde_oci_agent.executor import Executor
from fde_oci_agent.schema import Plan, Step, Expectation
from cloud_agent_assistant_web.redaction import Redactor
from cloud_agent_assistant_web.workflow import Workflow
from test_service import settings, result, FakeDocs


def read_plan(command=None, expect=True):
    step = Step(id="check", purpose="Inspect resource", command=command or ["devops", "project", "get"],
                arguments=["--project-id", "project-test"],
                expect=[Expectation(path="data.id", operator="equals", value="project-test")] if expect else [])
    return Plan(summary="Inspect resource", phase="verification" if expect else "discovery", steps=[step])


class Planner:
    def __init__(self, plans):
        self.plans, self.histories, self.docs = iter(plans), [], FakeDocs()

    async def plan(self, request, history):
        self.histories.append(list(history))
        value = next(self.plans)
        if isinstance(value, Exception):
            raise value
        return value


class RecordingExecutor(Executor):
    def __init__(self, settings, redactor):
        super().__init__(settings, redactor)
        self.commands = []

    def _process(self, args, timeout):
        self.commands.append(args)
        if "vnic-attachment" in args:
            return result({"data": [{"id": "project-test", "vnic-id": "vnic-test"}]})
        return result({"data": {"id": "project-test"}})


def run(settings, plans, ask=lambda _: "ACCEPT"):
    settings = replace(settings, max_stages=1)
    redactor = Redactor()
    planner = Planner(plans)
    executor = RecordingExecutor(settings, redactor)
    audit = Audit(settings.state_dir, redactor)
    messages = []
    workflow = Workflow(settings, planner, executor, audit, redactor, ask=ask, emit=messages.append)
    outcome = asyncio.run(workflow.run("Inspect resource"))
    return outcome, planner, executor, audit, messages


def test_completion_assessment_after_last_verification(settings):
    outcome, planner, executor, audit, messages = run(settings, [
        read_plan(), Plan(phase="complete", summary="Verified resource")])
    assert outcome.status == "succeeded"
    assert len(executor.commands) == 1
    assert len(planner.histories) == 2
    assert "final assessment" in planner.histories[-1][-1]["instruction"]
    assert any("result" in item for item in planner.histories[-1])
    assert audit.recent(1)[0]["event"] == "succeeded"
    assert not any("BLOCKED" in line for line in messages)


@pytest.mark.parametrize("final", [
    read_plan(),
    Plan(phase="action", summary="Need another operation", steps=[
        Step(id="create", purpose="Create resource", command=["devops", "project", "create"])]),
    Plan(phase="discovery", summary="Need user input", questions=["Which resource?"]),
])
def test_assessment_never_executes_or_prompts(settings, final):
    def unexpected_prompt(_):
        pytest.fail("Final assessment must not request input")
    outcome, planner, executor, audit, messages = run(settings, [read_plan(), final], unexpected_prompt)
    assert outcome.status == "blocked"
    assert len(executor.commands) == 1
    assert "Completion remains unverified" in outcome.message
    assert audit.recent(1)[0]["data"]["message"] == outcome.message
    assert any(outcome.message in line for line in messages)


def test_final_complete_requires_actual_verification(settings):
    outcome, _, executor, _, _ = run(settings, [
        read_plan(expect=False), Plan(phase="complete", summary="Done")])
    assert outcome.status == "blocked"
    assert "without a passed verification" in outcome.message
    assert len(executor.commands) == 1


def test_assessment_failure_cannot_report_success(settings):
    outcome, _, executor, _, _ = run(settings, [read_plan(), RuntimeError("Model unavailable")])
    assert outcome.status == "failed"
    assert len(executor.commands) == 1


def test_incomplete_mutation_preserves_partial_status(settings):
    mutation = Plan(phase="action", summary="Create resource", steps=[Step(
        id="create", purpose="Create project", command=["devops", "project", "create"],
        arguments=["--name", "FDE-Service"], expect=[Expectation(path="data.id", operator="exists")])])
    outcome, _, executor, audit, messages = run(settings, [mutation, read_plan()])
    assert outcome.status == "partially_completed"
    assert len(executor.commands) == 1
    assert audit.recent(1)[0]["event"] == "partially_completed"
    assert any("PARTIALLY_COMPLETED" in line for line in messages)


def test_vnic_alias_is_validated_as_canonical_read(settings):
    plan = read_plan(["compute", "instance", "list-vnic-attachments"])
    plan.steps[0].arguments = ["--instance-id", "instance-test"]
    plan.steps[0].expect[0].path = "data.0.id"
    # Test the full local validator, including docs and recognized-read checks.
    outcome, _, executor, audit, _ = run(settings, [plan, Plan(phase="complete", summary="Verified")])
    assert outcome.status == "succeeded"
    assert executor.commands[0][:3] == ["compute", "vnic-attachment", "list"]
    approvals = [item for item in audit.recent() if item["event"] == "approval"]
    assert approvals[0]["data"]["mode"] == "automatic_read_only"


def test_other_invalid_read_path_is_not_normalized(settings):
    outcome, _, executor, _, _ = run(settings, [
        read_plan(["compute", "instance", "delete"]), Plan(phase="complete", summary="Done")])
    assert outcome.status == "blocked"
    assert executor.commands == []
