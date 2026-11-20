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


class StrandsLLM(_Base):
    """Real Claude calls via Strands. The token budget is checked after each agent() call
    returns, so a tool-using agent can overspend within a single call."""

    def __init__(self, models: dict, budgets: dict, on_usage=None):
        super().__init__(budgets, on_usage)
        self.models = models

    def _complete(self, role, system, prompt, tools, images) -> FakeReply:
        from strands import Agent
        from strands.models.anthropic import AnthropicModel

        if role not in self.models:
            raise LLMError(f"no model configured for role {role!r}")
        try:
            model = AnthropicModel(model_id=self.models[role], max_tokens=MAX_TOKENS)
            agent = Agent(model=model, system_prompt=system, tools=tools or [], callback_handler=None)
        except Exception as e:
            raise LLMError(f"{role}: could not set up model {self.models[role]!r}: {type(e).__name__}") from e
        content = prompt
        if images:
            content = [{"text": prompt}] + [
                {"image": {"format": p.suffix.lstrip(".").lower().replace("jpg", "jpeg"),
                           "source": {"bytes": p.read_bytes()}}}
                for p in images
            ]
        try:
            result = agent(content)
        except Exception as e:
            raise LLMError(f"{role}: model call failed: {e}") from e
        usage = result.metrics.accumulated_usage
        return FakeReply(str(result), usage["inputTokens"], usage["outputTokens"])
