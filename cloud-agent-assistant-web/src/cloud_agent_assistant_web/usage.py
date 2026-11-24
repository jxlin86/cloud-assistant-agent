"""Record provider-reported token usage for every planner model call."""
from agents import OpenAIChatCompletionsModel


class UsageModel(OpenAIChatCompletionsModel):
    def __init__(self, *args, job, **kwargs):
        super().__init__(*args, **kwargs)
        self.job = job

    async def get_response(self, *args, **kwargs):
        try:
            response = await super().get_response(*args, **kwargs)
        except BaseException:
            self.job.record_usage(None)
            raise
        self.job.record_usage(response.raw_usage)
        return response
