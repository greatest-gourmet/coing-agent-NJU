"""Environment-backed application configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    api_key: str
    model: str
    base_url: str | None
    request_timeout: float
    workdir: Path
    max_retries: int = 3

    @classmethod
    def from_env(
        cls,
        *,
        workdir: str | Path = ".",
        environ: Mapping[str, str] | None = None,
    ) -> "AppConfig":
        env = os.environ if environ is None else environ
        api_key = env.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ConfigError("缺少环境变量 OPENAI_API_KEY。")

        model = env.get("OPENAI_MODEL", "deepseek-chat").strip()
        if not model:
            raise ConfigError("环境变量 OPENAI_MODEL 不能为空。")

        base_url = env.get("OPENAI_BASE_URL", "").strip() or None
        timeout_text = env.get("OPENAI_TIMEOUT_SECONDS", "60").strip()
        try:
            request_timeout = float(timeout_text)
        except ValueError as exc:
            raise ConfigError("OPENAI_TIMEOUT_SECONDS 必须是数字。") from exc
        if request_timeout <= 0:
            raise ConfigError("OPENAI_TIMEOUT_SECONDS 必须大于 0。")

        retries_text = env.get("OPENAI_MAX_RETRIES", "3").strip()
        try:
            max_retries = int(retries_text)
        except ValueError as exc:
            raise ConfigError("OPENAI_MAX_RETRIES 必须是整数。") from exc
        if max_retries < 1:
            raise ConfigError("OPENAI_MAX_RETRIES 必须大于 0。")

        resolved_workdir = Path(workdir).expanduser().resolve()
        if not resolved_workdir.is_dir():
            raise ConfigError(f"工作目录不存在或不是目录：{resolved_workdir}")

        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            request_timeout=request_timeout,
            workdir=resolved_workdir,
            max_retries=max_retries,
        )

    def safe_summary(self) -> dict[str, str | float | None]:
        """Return diagnostics that are safe to print or write to a trace."""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "request_timeout": self.request_timeout,
            "workdir": str(self.workdir),
        }

