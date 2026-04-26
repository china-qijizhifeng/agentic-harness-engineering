# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# LLM failover middleware.
#
#  RFC-0003: LLM 自动降级中间件
#
#  当主 LLM provider 调用失败时，自动切换到备用 provider 重试。
#  通过 middleware 机制实现零侵入，不修改 LLMCaller 核心代码。
#
#  # 核心流程
#
#  1. wrap_model_call 拦截 LLM 调用
#  2. 主 provider 失败时，匹配 status_code / exception 类型
#  3. 创建新的 LLMConfig + Client，传入 modified params
#  4. 按 fallback_providers 顺序依次尝试

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field

import anthropic
import openai

from nexau.archs.llm.llm_config import LLMConfig

from nexau.archs.main_sub.execution.hooks import Middleware, ModelCallFn, ModelCallParams
from nexau.archs.main_sub.execution.model_response import ModelResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FailoverTrigger:
    """Conditions that trigger failover to the next provider.

    RFC-0003: 降级触发条件配置
    """

    # 1. 匹配 HTTP status codes（如 500, 502, 503, 529）
    status_codes: list[int] = field(default_factory=lambda: [500, 502, 503])
    # 2. 匹配异常类名（如 "RateLimitError", "InternalServerError"）
    exception_types: list[str] = field(default_factory=lambda: list[str]())


@dataclass
class FallbackProvider:
    """A fallback LLM provider configuration.

    RFC-0003: 备用 provider 配置
    """

    name: str
    # llm_config 字段直接映射到 LLMConfig 构造参数
    llm_config: dict[str, str | int | float | bool] = field(default_factory=lambda: dict[str, str | int | float | bool]())


@dataclass
class CircuitBreakerConfig:
    """Optional circuit breaker to avoid hammering a failing primary.

    RFC-0003: 熔断器配置

    当主 provider 连续失败 failure_threshold 次后，在 recovery_timeout_seconds
    内直接跳过主 provider，直接走 fallback。
    """

    failure_threshold: int = 3
    recovery_timeout_seconds: float = 60.0


# ---------------------------------------------------------------------------
# Circuit breaker (optional)
# ---------------------------------------------------------------------------


class _CircuitBreaker:
    """Simple circuit breaker for the primary provider.

    RFC-0003: 熔断器实现

    状态机: CLOSED → OPEN → HALF_OPEN → CLOSED
    - CLOSED: 正常调用主 provider
    - OPEN: 连续失败超过阈值，跳过主 provider
    - HALF_OPEN: recovery_timeout 后允许一次探测
    """

    def __init__(self, config: CircuitBreakerConfig) -> None:
        self._threshold = config.failure_threshold
        self._recovery_timeout = config.recovery_timeout_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def should_skip_primary(self) -> bool:
        """Check if primary provider should be skipped."""
        if self._opened_at is None:
            return False
        elapsed = time.monotonic() - self._opened_at
        if elapsed >= self._recovery_timeout:
            # HALF_OPEN: 允许一次探测
            return False
        return True

    def record_success(self) -> None:
        """Record a successful call — reset breaker to CLOSED."""
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """Record a failed call — may trip breaker to OPEN."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._opened_at = time.monotonic()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_status_code(exc: Exception) -> int | None:
    """Extract HTTP status code from OpenAI/Anthropic SDK exceptions.

    RFC-0003: 从 SDK 异常中提取 HTTP 状态码

    支持 openai.APIStatusError.status_code 和 anthropic.APIStatusError.status_code。
    """
    # openai >= 1.x 和 anthropic >= 0.18 都有 status_code 属性
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code
    return None


def _build_client_for_config(config: LLMConfig) -> openai.OpenAI | anthropic.Anthropic:
    """Build an SDK client from LLMConfig.

    RFC-0003: 根据 api_type 创建对应的 SDK client
    """
    client_kwargs = config.to_client_kwargs()
    if config.api_type == "anthropic_chat_completion":
        return anthropic.Anthropic(**client_kwargs)
    return openai.OpenAI(**client_kwargs)


# ---------------------------------------------------------------------------
# Main middleware
# ---------------------------------------------------------------------------


class LLMFailoverMiddleware(Middleware):
    """LLM provider failover middleware.

    RFC-0003: LLM 自动降级中间件

    当主 provider 调用失败且匹配触发条件时，自动按顺序尝试备用 provider。
    通过 wrap_model_call 拦截，不修改 LLMCaller 核心代码。

    YAML 配置示例::

        middlewares:
          - import: nexau.archs.main_sub.execution.middleware.llm_failover:LLMFailoverMiddleware
            params:
              trigger:
                status_codes: [500, 502, 503, 529]
                exception_types: ["RateLimitError", "InternalServerError"]
              fallback_providers:
                - name: "backup-gateway"
                  llm_config:
                    base_url: "https://backup.example.com/v1"
                    api_key: "sk-backup-xxx"
                - name: "emergency"
                  llm_config:
                    model: "gpt-4o"
                    base_url: "https://emergency.example.com/v1"
                    api_key: "sk-emergency-xxx"
                    api_type: "openai_chat_completion"
              circuit_breaker:
                failure_threshold: 3
                recovery_timeout_seconds: 60
    """

    def __init__(
        self,
        *,
        fallback_providers: list[dict[str, str | dict[str, str | int | float | bool]]],
        trigger: dict[str, list[int] | list[str]] | None = None,
        circuit_breaker: dict[str, int | float] | None = None,
    ) -> None:
        # 1. 解析触发条件
        trigger_dict = trigger or {}
        raw_codes = trigger_dict.get("status_codes", [500, 502, 503])
        raw_exc_types = trigger_dict.get("exception_types", [])
        self._trigger = FailoverTrigger(
            status_codes=[int(c) for c in raw_codes],
            exception_types=[str(t) for t in raw_exc_types],
        )

        # 2. 解析 fallback providers
        self._fallback_providers: list[FallbackProvider] = []
        for entry in fallback_providers:
            name = str(entry.get("name", f"fallback-{len(self._fallback_providers)}"))
            raw_llm = entry.get("llm_config", {})
            llm_dict = dict(raw_llm) if isinstance(raw_llm, dict) else {}
            self._fallback_providers.append(FallbackProvider(name=name, llm_config=llm_dict))

        # 3. 可选熔断器
        self._circuit_breaker: _CircuitBreaker | None = None
        if circuit_breaker is not None:
            cb_cfg = CircuitBreakerConfig(
                failure_threshold=int(circuit_breaker.get("failure_threshold", 3)),
                recovery_timeout_seconds=float(circuit_breaker.get("recovery_timeout_seconds", 60.0)),
            )
            self._circuit_breaker = _CircuitBreaker(cb_cfg)

    # ------------------------------------------------------------------
    # Core: wrap_model_call
    # ------------------------------------------------------------------

    def wrap_model_call(self, params: ModelCallParams, call_next: ModelCallFn) -> ModelResponse | None:
        """Intercept LLM calls and failover on matching errors.

        RFC-0003: 拦截 LLM 调用，匹配错误后自动降级

        流程:
        1. 如果熔断器打开，跳过主 provider
        2. 尝试主 provider
        3. 失败且匹配触发条件 → 依次尝试 fallback providers
        4. 所有 provider 都失败 → 抛出最后一个异常
        """
        # 1. 判断是否跳过主 provider（熔断器打开时）
        skip_primary = self._circuit_breaker is not None and self._circuit_breaker.should_skip_primary()

        last_exc: Exception | None = None

        if not skip_primary:
            try:
                result = call_next(params)
                if self._circuit_breaker is not None:
                    self._circuit_breaker.record_success()
                return result
            except Exception as exc:
                last_exc = exc
                if self._circuit_breaker is not None:
                    self._circuit_breaker.record_failure()
                if not self._should_failover(exc):
                    raise
                logger.warning(
                    "LLM failover: primary provider failed (%s), trying fallback providers",
                    type(exc).__name__,
                )

        # 2. 依次尝试 fallback providers
        for i, provider in enumerate(self._fallback_providers):
            try:
                fallback_params = self._apply_fallback(params, provider)
                result = call_next(fallback_params)
                logger.info("LLM failover: succeeded with provider '%s'", provider.name)
                return result
            except Exception as exc:
                last_exc = exc
                is_last = i == len(self._fallback_providers) - 1
                if is_last:
                    logger.error(
                        "LLM failover: all providers exhausted, last error: %s",
                        exc,
                    )
                    raise
                logger.warning(
                    "LLM failover: provider '%s' failed (%s), trying next",
                    provider.name,
                    type(exc).__name__,
                )

        # 不应该到这里，但防御性处理
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("LLM failover: Primary provider was skipped and no fallback providers were available.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _should_failover(self, exc: Exception) -> bool:
        """Check if exception matches failover trigger conditions.

        RFC-0003: 判断异常是否匹配降级触发条件

        匹配逻辑（OR）：
        - status_code 在 trigger.status_codes 中
        - 异常类名在 trigger.exception_types 中
        """
        # 检查 status code
        status = _extract_status_code(exc)
        if status is not None and status in self._trigger.status_codes:
            return True

        # 检查异常类名
        exc_type_name = type(exc).__name__
        if exc_type_name in self._trigger.exception_types:
            return True

        return False

    def _apply_fallback(self, params: ModelCallParams, provider: FallbackProvider) -> ModelCallParams:
        """Create new ModelCallParams with fallback provider config.

        RFC-0003: 创建使用备用 provider 的新 params

        不修改原始 params，而是创建副本并替换 llm_config 和 client。
        未在 fallback 中指定的字段继承自主 provider。
        """
        # 1. 基于主 config 创建副本
        original_config = params.llm_config
        if isinstance(original_config, LLMConfig):
            new_config = original_config.copy()
        else:
            new_config = LLMConfig()

        # 2. 覆盖 fallback 指定的字段
        for key, value in provider.llm_config.items():
            new_config.set_param(key, value)

        # 3. 创建新 client
        new_client = _build_client_for_config(new_config)

        # 4. 创建新的 api_params
        new_api_params = new_config.to_openai_params()
        # 保留原始 params 中的非 config 字段（如 tools, stop 等）
        for key in ("tools", "tool_choice", "stop"):
            if key in params.api_params and key not in new_api_params:
                new_api_params[key] = params.api_params[key]
        if params.max_tokens is not None:
            new_api_params["max_tokens"] = params.max_tokens

        # 5. 构建新 params（浅拷贝不可变字段，替换可变字段）
        new_params = copy.copy(params)
        new_params.llm_config = new_config
        new_params.openai_client = new_client
        new_params.api_params = new_api_params

        return new_params
