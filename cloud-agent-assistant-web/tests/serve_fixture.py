"""Browser-test server: every model plan and CLI response is simulated."""
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_service import FakePlanner, FakeExecutor
from test_multicloud import FakeCloudPlanner, FakeCloudExecutor
import cloud_agent_assistant_web.multicloud as multi
from cloud_agent_assistant_web.app import create_app
from cloud_agent_assistant_web.jobs import Manager
from fde_oci_agent.config import Settings

root = Path(__file__).resolve().parent.parent / ".state" / "browser-fixture"
root.mkdir(parents=True, exist_ok=True)
config = root / "oci-config"
config.write_text("[OCIPRJ]\nregion=us-ashburn-1\ntenancy=ocid1.tenancy.test\n")
settings = Settings(root=root, config_file=config, profile="OCIPRJ", region="us-ashburn-1",
                    compartment_id="ocid1.compartment.test", model_name="openai.gpt-oss-120b",
                    model_region="us-chicago-1", model_project="", model_auth="profile", cli="NOT-A-REAL-CLI")

class BrowserPlanner(FakePlanner):
    async def plan(self, request, history):
        # Synthetic counters, confined to this simulated browser-test server.
        self.executor.job.record_usage({'prompt_tokens': 120, 'completion_tokens': 25})
        self.mode = "secret" if "secret" in request.lower() else "write" if "create" in request.lower() else "read"
        return await super().plan(request, history)

class BrowserCloudPlanner(FakeCloudPlanner):
    async def plan(self, request, history):
        self.mode = 'write' if 'create' in request.lower() else 'read'
        return await super().plan(request, history)

# Only the test server bypasses execution-CLI detection; every process result is fake.
multi.cli_launcher = lambda cloud: ['FAKE-' + cloud]

def manager(base, state):
    return Manager(base, state, planner_factory=BrowserPlanner, executor_factory=FakeExecutor,
                   cloud_planner_factory=BrowserCloudPlanner, cloud_executor_factory=FakeCloudExecutor,
                   compartment_lookup=lambda settings: dict(id=settings.compartment_id, name='fde-sandbox'))

if __name__ == "__main__":
    uvicorn.run(create_app(settings, root / "data", manager_factory=manager,
                          compartment_lookup=lambda settings, ocid: dict(id=ocid, name='fde-sandbox')),
                host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8767, access_log=False, log_level="warning")
