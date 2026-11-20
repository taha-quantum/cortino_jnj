import os

import pytest
from pydantic import BaseModel

from cortino.llm import FakeLLM, FakeReply, LLMError, StrandsLLM


class Plan(BaseModel):
    title: str
    steps: int


def test_valid_reply_parsed():
    llm = FakeLLM({"planner": ['{"title": "t", "steps": 2}']})
    out = llm.structured("planner", "sys", "go", Plan)
    assert out == Plan(title="t", steps=2)
    assert llm.calls[0]["role"] == "planner"
    assert llm.calls[0]["system"].startswith("sys")
    assert "JSON schema" in llm.calls[0]["system"]
    assert llm.calls[0]["prompt"] == "go"


def test_dict_and_fenced_replies_parsed():
    llm = FakeLLM({"planner": [{"title": "a", "steps": 1}, '```json\n{"title": "b", "steps": 3}\n```']})
    assert llm.structured("planner", "s", "p", Plan).title == "a"
    assert llm.structured("planner", "s", "p", Plan).steps == 3


@pytest.mark.parametrize(
    "reply",
    [
        'Here is the plan: {"title": "t", "steps": 2} Let me know!',
        'Sure.\n```json\n{"title": "t", "steps": 2}\n```\nAnything else?',
        'Intro {not json} then\n```\n{"title": "t", "steps": 2}\n```',
    ],
)
def test_json_extracted_from_prose(reply):
    llm = FakeLLM({"planner": [reply]})
    assert llm.structured("planner", "s", "p", Plan) == Plan(title="t", steps=2)
    assert len(llm.calls) == 1


def test_strands_setup_failure_names_role(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = StrandsLLM({"planner": "claude-x"}, {})
    import strands.models.anthropic as am

    def boom(**kw):
        raise RuntimeError("sk-secret")

    monkeypatch.setattr(am, "AnthropicModel", boom)
    with pytest.raises(LLMError, match="planner") as ei:
        llm.text("planner", "s", "p")
    assert "sk-secret" not in str(ei.value)


def test_malformed_then_valid_retries_once():
    llm = FakeLLM({"planner": ['{"title": "t"}', '{"title": "t", "steps": 2}']})
    assert llm.structured("planner", "s", "p", Plan).steps == 2
    assert len(llm.calls) == 2
    assert "steps" in llm.calls[1]["prompt"]  # validation error text appended
    assert llm.calls[1]["prompt"].startswith("p")


def test_unparseable_json_retries():
    llm = FakeLLM({"planner": ["not json", '{"title": "t", "steps": 2}']})
    assert llm.structured("planner", "s", "p", Plan).title == "t"


def test_malformed_twice_raises_llmerror():
    llm = FakeLLM({"planner": ["{}", "{}", '{"title": "t", "steps": 2}']})
    with pytest.raises(LLMError):
        llm.structured("planner", "s", "p", Plan)
    assert len(llm.calls) == 2


def test_budget_exceeded_raises():
    llm = FakeLLM(
        {"planner": [FakeReply("{}", 60, 10), FakeReply('{"title": "t", "steps": 1}', 60, 10)]},
        budgets={"agent_token_budget": 100, "agent_time_budget_s": 600},
    )
    with pytest.raises(LLMError, match="budget exceeded"):
        llm.structured("planner", "s", "p", Plan)


def test_time_budget_exceeded_raises():
    llm = FakeLLM(
        {"builder": ["hello"]},
        budgets={"agent_token_budget": 100, "agent_time_budget_s": -1},
    )
    with pytest.raises(LLMError, match="budget exceeded"):
        llm.text("builder", "s", "p")


def test_text_records_tools_images_and_usage(tmp_path):
    seen = []
    llm = FakeLLM({"checker": [FakeReply("ok", 5, 7)]}, on_usage=lambda *a: seen.append(a))
    img = tmp_path / "a.png"
    assert llm.text("checker", "s", "p", tools=["t"], images=[img]) == "ok"
    assert llm.calls[0]["tools"] == ["t"] and llm.calls[0]["images"] == [img]
    assert seen == [("checker", 5, 7)]


def test_exhausted_script_raises_clearly():
    llm = FakeLLM({"planner": []})
    with pytest.raises(LLMError, match="planner"):
        llm.text("planner", "s", "p")
    with pytest.raises(LLMError, match="cheap"):
        llm.text("cheap", "s", "p")


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_strands_live_structured():
    from cortino.config import load

    llm = StrandsLLM(load("models"), load("thresholds"))
    out = llm.structured("cheap", "Answer tersely.", "Plan: title 'x', 3 steps.", Plan)
    assert out.steps == 3
