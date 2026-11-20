
import pytest
from pydantic import BaseModel

from cortino.llm import (
    FakeLLM,
    FakeReply,
    LLMError,
    StrandsLLM,
    live_llm_configured,
    models_config_name,
)


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
    monkeypatch.delenv("CORTINO_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    llm = StrandsLLM({"planner": "claude-x"}, {})
    import strands.models.anthropic as am

    def boom(**kw):
        raise RuntimeError("sk-secret")

    monkeypatch.setattr(am, "AnthropicModel", boom)
    with pytest.raises(LLMError, match="planner") as ei:
        llm.text("planner", "s", "p")
    assert "sk-secret" not in str(ei.value)


def test_vertex_provider_passes_vertex_client(monkeypatch):
    monkeypatch.setenv("CORTINO_LLM_PROVIDER", "vertex")
    import anthropic
    import strands.models.anthropic as am

    made = []

    class FakeVertex:
        def __init__(self, **kw):
            made.append(kw)

    seen = {}

    def capture(**kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(anthropic, "AsyncAnthropicVertex", FakeVertex)
    monkeypatch.setattr(am, "AnthropicModel", capture)
    llm = StrandsLLM({"planner": "claude-x"}, {})
    with pytest.raises(LLMError, match="planner"):
        llm.text("planner", "s", "p")
    assert isinstance(seen["client"], FakeVertex)
    assert seen["model_id"] == "claude-x"
    with pytest.raises(LLMError):
        llm.text("planner", "s", "p")
    assert len(made) == 1  # one client per StrandsLLM


def test_vertex_missing_region_is_clear(monkeypatch):
    monkeypatch.setenv("CORTINO_LLM_PROVIDER", "vertex")
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
    import anthropic

    def no_region(**kw):
        raise ValueError("No region was given.")

    monkeypatch.setattr(anthropic, "AsyncAnthropicVertex", no_region)
    with pytest.raises(LLMError, match="could not set up vertex model .*No region"):
        StrandsLLM({"planner": "claude-x"}, {}).text("planner", "s", "p")


def test_anthropic_without_key_says_what_to_set(monkeypatch):
    monkeypatch.delenv("CORTINO_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(LLMError, match="planner: ANTHROPIC_API_KEY is not set.*CORTINO_LLM_PROVIDER"):
        StrandsLLM({"planner": "claude-x"}, {}).text("planner", "s", "p")


def test_anthropic_provider_passes_no_client(monkeypatch):
    monkeypatch.delenv("CORTINO_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import strands.models.anthropic as am

    seen = {}

    def capture(**kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(am, "AnthropicModel", capture)
    with pytest.raises(LLMError):
        StrandsLLM({"planner": "claude-x"}, {}).text("planner", "s", "p")
    assert "client" not in seen


def test_gemini_provider_uses_vertex_gemini(monkeypatch):
    monkeypatch.setenv("CORTINO_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-x")
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
    import strands.models.gemini as gm

    seen = {}

    def capture(**kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(gm, "GeminiModel", capture)
    with pytest.raises(LLMError, match="planner"):
        StrandsLLM({"planner": "gemini-x"}, {}).text("planner", "s", "p")
    assert seen["model_id"] == "gemini-x"
    assert seen["client_args"] == {"vertexai": True, "project": "proj-x", "location": "global"}
    assert seen["params"]["max_output_tokens"] > 0


def test_gemini_needs_project(monkeypatch):
    monkeypatch.setenv("CORTINO_LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    with pytest.raises(LLMError, match="GOOGLE_CLOUD_PROJECT"):
        StrandsLLM({"planner": "gemini-x"}, {}).text("planner", "s", "p")


def test_models_config_follows_provider(monkeypatch):
    from cortino.config import load

    monkeypatch.setenv("CORTINO_LLM_PROVIDER", "gemini")
    assert models_config_name() == "models_gemini"
    assert set(load("models_gemini")) == set(load("models"))  # same roles for every provider
    monkeypatch.setenv("CORTINO_LLM_PROVIDER", "vertex")
    assert models_config_name() == "models"


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("CORTINO_LLM_PROVIDER", "openai")
    with pytest.raises(LLMError, match="CORTINO_LLM_PROVIDER"):
        StrandsLLM({"planner": "claude-x"}, {}).text("planner", "s", "p")


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
@pytest.mark.skipif(not live_llm_configured(), reason="needs ANTHROPIC_API_KEY or Vertex AI settings")
def test_strands_live_structured():
    from cortino.config import load

    llm = StrandsLLM(load(models_config_name()), load("thresholds"))
    out = llm.structured("cheap", "Answer tersely.", "Plan: title 'x', 3 steps.", Plan)
    assert out.steps == 3
