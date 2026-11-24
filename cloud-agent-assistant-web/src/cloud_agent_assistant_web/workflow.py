"""OCI browser workflow fixes without changing the pinned terminal agent."""
from dataclasses import replace
import subprocess

from fde_oci_agent.executor import is_read
from fde_oci_agent.schema import Plan, Step
from fde_oci_agent.workflow import Workflow as CoreWorkflow
from .runtime import format_command


OCI_PLANNING_HINTS = """
OCI CLI reminders: list VNIC attachments with compute vnic-attachment list,
never compute instance list-vnic-attachments. CLI JSON is rooted at data;
bind a listed VNIC ID with {{steps.attachments.data.0.vnic-id}}.
A public IP alone does not prove Internet or SSH connectivity. Inspect the
effective subnet/VNIC routes, Internet gateway and applicable security rules.
Distinguish cloud configuration evidence from an actual laptop connectivity test.
Do not claim a laptop test succeeded without recorded test evidence.
"""

ASSESSMENT = """The resource execution stage budget is exhausted. This is one
final assessment only; no proposed resource commands or questions will execute.
Return complete only if recorded, verified results fully answer the original
request. Otherwise summarize the completed work and what remains unverified.
Do not infer Internet connectivity merely from an assigned public IP."""


class _BudgetPlanner:
    def __init__(self, planner, stages):
        self.target, self.stages, self.calls = planner, stages, 0
        self.assessing = False
        self.message = ""
        self.mutation_attempted = False

    def __getattr__(self, name):
        return getattr(self.target, name)

    async def plan(self, request, history):
        self.calls += 1
        if self.calls <= self.stages:
            return await self.target.plan(request, history)
        self.assessing = True
        plan = await self.target.plan(request, [*history, {"instruction": ASSESSMENT}])
        if plan.phase == "complete" and not plan.questions and not plan.steps:
            # CoreWorkflow still requires its own passed-verification flag.
            return plan
        self.message = ("Stage limit reached; no additional resource commands were executed. "
                        "Completion remains unverified. Assessment: " + plan.summary)
        # An empty discovery plan terminates the core workflow before approval
        # or execution. In particular, never forward final assessment questions.
        return Plan(phase="discovery", summary=self.message)

    def final_message(self, message):
        if self.assessing and self.message and message == "No executable steps were proposed.":
            return self.message
        return message

    def final_status(self, status):
        if self.assessing and self.mutation_attempted and status == "blocked":
            return "partially_completed"
        return status


class _BudgetAudit:
    def __init__(self, audit, planner):
        self.target, self.planner = audit, planner

    def record(self, run_id, kind, payload):
        if kind == "executing" and not is_read(Step.model_validate(payload["step"])):
            self.planner.mutation_attempted = True
        kind = self.planner.final_status(kind)
        if isinstance(payload, dict) and "message" in payload:
            payload = {**payload, "message": self.planner.final_message(payload["message"])}
        self.target.record(run_id, kind, payload)


class Workflow(CoreWorkflow):
    def _show(self, plan, digest):
        # Keep all engine approval details, but render commands for this host OS.
        replacements = {}
        for step in plan.steps:
            for item in (step, step.wait_for):
                if item is not None:
                    argv = ['oci', *item.command, *item.arguments]
                    replacements[self.redactor.text(subprocess.list2cmdline(argv))] = self.redactor.text(format_command(argv))
        original = self.emit

        def show(message):
            for before, after in sorted(replacements.items(), key=lambda pair: -len(pair[0])):
                message = message.replace(before, after)
            original(message)

        self.emit = show
        try:
            super()._show(plan, digest)
        finally:
            self.emit = original

    def __init__(self, settings, planner, executor, audit, redactor, ask=input, emit=print):
        budget_planner = _BudgetPlanner(planner, settings.max_stages)

        def show(message):
            if budget_planner.assessing and budget_planner.mutation_attempted:
                message = message.replace("\nBLOCKED [", "\nPARTIALLY_COMPLETED [", 1)
            if message.startswith(f"\nPlanning stage {settings.max_stages + 1} with "):
                emit("\nStage limit reached; assessing completion without additional resource commands...")
            elif budget_planner.assessing and budget_planner.message:
                emit(message.replace("No executable steps were proposed.",
                                     redactor.text(budget_planner.message)))
            else:
                emit(message)

        # The extra slot can only assess completion; _BudgetPlanner strips every
        # executable plan. Existing verification and approval gates stay intact.
        super().__init__(replace(settings, max_stages=settings.max_stages + 1), budget_planner,
                         executor, _BudgetAudit(audit, budget_planner), redactor, ask, show)

    async def _validate_plan(self, plan, results=None):
        for step in plan.steps:
            for command in (step.command, step.wait_for.command if step.wait_for else None):
                if command == ["compute", "instance", "list-vnic-attachments"]:
                    command[:] = ["compute", "vnic-attachment", "list"]
                    self.emit("Corrected OCI command path to compute vnic-attachment list.")
        await super()._validate_plan(plan, results)

    async def run(self, request, plan_only=False):
        outcome = await super().run(request, plan_only)
        return replace(outcome, status=self.planner.final_status(outcome.status),
                       message=self.redactor.text(self.planner.final_message(outcome.message)))
