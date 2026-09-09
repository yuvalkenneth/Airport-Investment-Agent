import os

from pydantic import BaseModel, Field

from airport_agent.constants import (
    AGENT_RECURSION_LIMIT,
    DEFAULT_MODEL,
    MODEL_MAX_RETRIES,
    MODEL_TIMEOUT_MS,
)


class Settings(BaseModel):
    api_key: str = Field(min_length=1)
    model: str = DEFAULT_MODEL
    model_timeout_ms: int = Field(default=MODEL_TIMEOUT_MS, gt=0)
    model_max_retries: int = Field(default=MODEL_MAX_RETRIES, ge=0)
    agent_recursion_limit: int = Field(default=AGENT_RECURSION_LIMIT, gt=0)

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            model=os.getenv("OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL,
        )
