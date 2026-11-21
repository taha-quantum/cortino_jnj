# Strands call shapes, confirmed against the installed strands-agents package
# (strands/agent/agent.py, models/anthropic.py, types/media.py, telemetry/metrics.py):
#   model  = strands.models.anthropic.AnthropicModel(model_id=..., max_tokens=...)  # max_tokens is required
#   agent  = strands.Agent(model=model, system_prompt=str, tools=[@tool fns...], callback_handler=None)
#   result = agent(prompt)   # prompt: str, or list of ContentBlock dicts:
#                            #   [{"text": "..."}, {"image": {"format": "png", "source": {"bytes": b"..."}}}]
#   str(result)                                    -> final assistant text
#   result.metrics.accumulated_usage               -> {"inputTokens", "outputTokens", "totalTokens", ...}
#   Native structured output exists (agent(prompt, structured_output_model=Model) ->
#   result.structured_output), but validation here is shared with FakeLLM, so StrandsLLM asks
#   for JSON in text and the common code validates and retries.
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError

MAX_TOKENS = 16000
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(Exception):
    pass


class LLM(Protocol):
    def structured(
        self, role: str, system: str, prompt: str, schema: type[BaseModel], tools: list | None = None
    ) -> BaseModel: ...

    def text(
        self, role: str, system: str, prompt: str, tools: list | None = None, images: list[Path] | None = None
    ) -> str: ...


@dataclass
class FakeReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class _Base:
    """Shared validation, retry, budget and usage logic; subclasses implement _complete."""

    def __init__(
        self,
        budgets: dict | None = None,
        on_usage: Callable[[str, int, int], None] | None = None,
    ):
        self.budgets = budgets or {}
        self.on_usage = on_usage
        # Called before each retry of a temporary failure: (role, attempt, error, wait seconds).
        self.on_retry: Callable[[str, int, str, float], None] | None = None
        self._sleep = time.sleep

    def _complete(
        self, role: str, system: str, prompt: str, tools: list | None, images: list[Path] | None
    ) -> FakeReply:
        raise NotImplementedError

    def _attempt(self, state: dict, role, system, prompt, tools, images) -> str:
        time_budget = self.budgets.get("agent_time_budget_s")
        if time_budget is not None and time.monotonic() - state["start"] > time_budget:
            raise LLMError(f"budget exceeded: more than {time_budget}s elapsed")
        reply = self._complete(role, system, prompt, tools, images)
        if self.on_usage:
            self.on_usage(role, reply.input_tokens, reply.output_tokens)
        state["tokens"] += reply.input_tokens + reply.output_tokens
        token_budget = self.budgets.get("agent_token_budget")
        if token_budget is not None and state["tokens"] > token_budget:
            raise LLMError(f"budget exceeded: {state['tokens']} tokens used, budget {token_budget}")
        if time_budget is not None and time.monotonic() - state["start"] > time_budget:
            raise LLMError(f"budget exceeded: more than {time_budget}s elapsed")
        return reply.text

    def text(self, role, system, prompt, tools=None, images=None) -> str:
        state = {"start": time.monotonic(), "tokens": 0}
        return self._attempt(state, role, system, prompt, tools, images)

    def structured(self, role, system, prompt, schema, tools=None) -> BaseModel:
        state = {"start": time.monotonic(), "tokens": 0}
        system = f"{system}\n\nReply with only a JSON object matching this JSON schema:\n{json.dumps(schema.model_json_schema())}"
        current = prompt
        error: Exception | None = None
        for _ in range(2):
            raw = self._attempt(state, role, system, current, tools, None)
            try:
                return schema.model_validate(_load_json(raw))
            except (ValidationError, ValueError) as e:
                error = e
                current = f"{prompt}\n\nYour previous reply was invalid:\n{e}\nReply again with valid JSON only."
        raise LLMError(f"{role}: reply failed validation twice: {error}")


def _load_json(raw: str):
    raw = raw.strip()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    for m in _FENCE.finditer(raw):
        try:
            return json.loads(m.group(1))
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    for m in re.finditer(r"[{\[]", raw):
        try:
            return decoder.raw_decode(raw, m.start())[0]
        except ValueError:
            continue
    raise ValueError("no JSON found in reply")


class FakeLLM(_Base):
    """Scripted replies per role: str, dict (JSON-dumped) or FakeReply. Records every call."""

    def __init__(self, script: dict[str, list], budgets: dict | None = None, on_usage=None):
        super().__init__(budgets, on_usage)
        self.script = {role: list(replies) for role, replies in script.items()}
        self.calls: list[dict] = []

    def _complete(self, role, system, prompt, tools, images) -> FakeReply:
        self.calls.append(
            {"role": role, "system": system, "prompt": prompt, "tools": tools, "images": images}
        )
        replies = self.script.get(role)
        if not replies:
            raise LLMError(f"FakeLLM has no scripted reply left for role {role!r}")
        reply = replies.pop(0)
        if isinstance(reply, FakeReply):
            return reply
        return FakeReply(reply if isinstance(reply, str) else json.dumps(reply))


_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504, 529}
# Exception class names (anywhere in the class hierarchy) that mean "try again": network and DNS
# failures, timeouts, throttling and overloaded or unavailable servers, across the Anthropic,
# Google and Strands SDKs and requests/httpx/google-auth underneath them.
_TRANSIENT_NAMES = (
    "connection", "timeout", "timedout", "nameresolution", "transporterror", "throttl",
    "ratelimit", "serviceunavailable", "internalserver", "overloaded", "resourceexhausted",
)
# The same in error text, for SDKs that re-raise only the message.
_TRANSIENT_TEXT = (
    "max retries exceeded", "nameresolutionerror", "getaddrinfo", "temporary failure in name resolution",
    "timed out", "connection reset", "connection aborted", "connection refused", "resource_exhausted",
    "rate limit", "overloaded", "service unavailable", "503 unavailable", "502 bad gateway",
)


def is_transient(error: BaseException) -> bool:
    """Whether a failed model call is worth retrying, judged across the whole cause chain. Bad
    requests, authentication and unknown models are not; a retry cannot change their answer."""
    seen, cur = set(), error
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        names = [c.__name__.lower() for c in type(cur).__mro__]
        if any(t in n for n in names for t in _TRANSIENT_NAMES):
            return True
        for attr in ("status_code", "code", "status"):
            code = getattr(cur, attr, None)
            if isinstance(code, int) and code in _TRANSIENT_STATUS:
                return True
        if any(t in str(cur).lower() for t in _TRANSIENT_TEXT):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


PROVIDERS = ("anthropic", "vertex", "gemini")


def llm_provider() -> str:
    """`CORTINO_LLM_PROVIDER`, one of:
    - "anthropic" (default): Claude via the Anthropic API, needs ANTHROPIC_API_KEY.
    - "vertex": Claude on Google Cloud Vertex AI. Application Default Credentials, CLOUD_ML_REGION
      and optionally ANTHROPIC_VERTEX_PROJECT_ID (else the ADC project).
    - "gemini": Gemini on Vertex AI. Application Default Credentials, GOOGLE_CLOUD_PROJECT (or
      ANTHROPIC_VERTEX_PROJECT_ID) and GOOGLE_CLOUD_LOCATION (or CLOUD_ML_REGION, else "global").
      Model IDs come from config/models_gemini.yaml."""
    provider = os.environ.get("CORTINO_LLM_PROVIDER", "").strip().lower() or "anthropic"
    if provider not in PROVIDERS:
        raise LLMError(f"CORTINO_LLM_PROVIDER must be one of {', '.join(PROVIDERS)}, got {provider!r}")
    return provider


def models_config_name() -> str:
    """The config file (without .yaml) holding the role -> model map for the active provider."""
    return "models_gemini" if llm_provider() == "gemini" else "models"


def _gemini_project() -> str:
    return os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") or ""


def _gemini_location() -> str:
    return os.environ.get("GOOGLE_CLOUD_LOCATION") or os.environ.get("CLOUD_ML_REGION") or "global"


def live_llm_configured() -> bool:
    """Whether real model calls can be attempted (used to gate live tests)."""
    provider = os.environ.get("CORTINO_LLM_PROVIDER", "").strip().lower()
    if provider == "vertex":
        return bool(os.environ.get("CLOUD_ML_REGION"))
    if provider == "gemini":
        return bool(_gemini_project())
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


class StrandsLLM(_Base):
    """Real model calls via Strands: Claude through the Anthropic API or Vertex AI, or Gemini
    through Vertex AI (see llm_provider). The token budget is checked after each agent() call
    returns, so a tool-using agent can overspend within a single call."""

    def __init__(self, models: dict, budgets: dict, on_usage=None):
        super().__init__(budgets, on_usage)
        self.models = models
        self._client = None

    def _make_model(self, provider: str, model_id: str):
        if provider == "gemini":
            from strands.models.gemini import (
                GeminiModel,  # needs the gemini extra (google-genai)
            )

            project = _gemini_project()
            if not project:
                raise LLMError("gemini: set GOOGLE_CLOUD_PROJECT (or ANTHROPIC_VERTEX_PROJECT_ID)")
            return GeminiModel(
                client_args={"vertexai": True, "project": project, "location": _gemini_location()},
                model_id=model_id,
                params={"max_output_tokens": MAX_TOKENS},
            )
        from strands.models.anthropic import AnthropicModel

        kwargs = {}
        if provider == "vertex":
            if self._client is None:
                from anthropic import (
                    AsyncAnthropicVertex,  # needs the vertex extra (google-auth)
                )

                self._client = AsyncAnthropicVertex()
            kwargs["client"] = self._client
        return AnthropicModel(model_id=model_id, max_tokens=MAX_TOKENS, **kwargs)

    def _complete(self, role, system, prompt, tools, images) -> FakeReply:
        from strands import Agent

        if role not in self.models:
            raise LLMError(f"no model configured for role {role!r}")
        provider = llm_provider()
        model_id = self.models[role]
        if provider == "anthropic" and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise LLMError(
                f"{role}: ANTHROPIC_API_KEY is not set. Set it, or set CORTINO_LLM_PROVIDER to "
                "'vertex' or 'gemini' (in the shell or in .env)"
            )
        try:
            model = self._make_model(provider, model_id)
        except LLMError:
            raise
        except ImportError as e:
            raise LLMError(f"provider {provider!r} needs its extra: uv sync --extra {provider}") from e
        except Exception as e:
            if provider == "anthropic":  # may echo the API key; keep only the type
                raise LLMError(f"{role}: could not set up model {model_id!r}: {type(e).__name__}") from e
            # Google SDK messages (e.g. no CLOUD_ML_REGION) carry no secrets and say what is missing
            raise LLMError(f"{role}: could not set up {provider} model {model_id!r}: {e}") from e
        content = prompt
        if images:
            content = [{"text": prompt}] + [
                {"image": {"format": p.suffix.lstrip(".").lower().replace("jpg", "jpeg"),
                           "source": {"bytes": p.read_bytes()}}}
                for p in images
            ]
        retries = int(self.budgets.get("llm_retries", 3))
        backoff = float(self.budgets.get("llm_retry_backoff_s", 2))
        for attempt in range(retries + 1):
            try:  # a fresh agent each try: a failed call can leave half a turn in the agent's history
                agent = Agent(model=model, system_prompt=system, tools=tools or [], callback_handler=None)
            except Exception as e:
                raise LLMError(f"{role}: could not set up model {model_id!r}: {type(e).__name__}") from e
            try:
                result = agent(content)
                break
            except Exception as e:
                if attempt < retries and is_transient(e):
                    wait = backoff * 2**attempt
                    if self.on_retry:
                        self.on_retry(role, attempt + 1, f"{type(e).__name__}: {e}"[:300], wait)
                    self._sleep(wait)
                    continue
                tried = f" (after {attempt + 1} attempts)" if attempt else ""
                raise LLMError(f"{role}: model call failed{tried}: {e}") from e
        usage = result.metrics.accumulated_usage
        return FakeReply(str(result), usage["inputTokens"], usage["outputTokens"])
