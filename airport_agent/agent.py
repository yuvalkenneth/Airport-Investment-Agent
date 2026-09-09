from langchain.agents import create_agent
from langchain_openrouter import ChatOpenRouter

from airport_agent.config import Settings
from airport_agent.constants import SYSTEM_PROMPT
from airport_agent.tools import TOOLS


def build_agent(settings: Settings, model=None):
    model = model or ChatOpenRouter(
        model=settings.model,
        api_key=settings.api_key,
        timeout=settings.model_timeout_ms,
        max_retries=settings.model_max_retries,
    )
    return create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)
