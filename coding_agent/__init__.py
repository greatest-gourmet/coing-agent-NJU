"""Core package for the coding agent."""

from .config import AppConfig, ConfigError
from .llm_client import LLMClientError, ModelResponse, OpenAICompatibleClient, ToolCall
from .tools import ToolContext, ToolRegistry, ToolResult, create_default_registry
from .memory import WorkingMemory
from .trace import TraceRecorder, redact
from .lessons import LessonStore

__all__ = [
    "AppConfig",
    "AgentLimits",
    "AgentResult",
    "AgentRunner",
    "ConfigError",
    "LLMClientError",
    "ModelResponse",
    "OpenAICompatibleClient",
    "ToolCall",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "create_default_registry",
    "WorkingMemory",
    "TraceRecorder",
    "redact",
    "LessonStore",
]
from .agent import AgentLimits, AgentResult, AgentRunner
