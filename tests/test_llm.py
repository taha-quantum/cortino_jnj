
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


class _Usage:
    def __init__(self):
        self.accumulated_usage = {"inputTokens": 3, "outputTokens": 4}


class _Result:
    metrics = _Usage()

    def __str__(self):
        return "ok"


def _scripted_agent(monkeypatch, outcomes):
    """Patch strands.Agent so each agent call takes the next outcome: an exception or a result."""
    import strands

    made = []

    class Agent:
        def __init__(self, **kw):
            made.append(kw)

        def __call__(self, content):
            out = outcomes.pop(0)
            if isinstance(out, BaseException):
                raise out
            return out

    monkeypatch.setattr(strands, "Agent", Agent)
    return made


def _retrying_llm(monkeypatch, retries=3):
    monkeypatch.setenv("CORTINO_LLM_PROVIDER", "gemini")
    llm = StrandsLLM({"checker": "m"}, {"llm_retries": retries, "llm_retry_backoff_s": 2})
    llm._make_model = lambda provider, model_id: object()
    llm.slept = []
    llm._sleep = llm.slept.append
    llm.retries = []
    llm.on_retry = lambda role, attempt, error, wait: llm.retries.append((role, attempt, wait))
    return llm


def _dns_error():
    """Shaped like the real failure: requests ConnectionError caused by a NameResolutionError."""
    import requests

    try:
        try:
            raise OSError("[Errno 11004] getaddrinfo failed")
        except OSError as inner:
            raise requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='oauth2.googleapis.com', port=443): Max retries exceeded"
            ) from inner
    except requests.exceptions.ConnectionError as e:
        return e


class _Status(Exception):
    def __init__(self, code):
        super().__init__(f"status {code}")
        self.status_code = code


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (_dns_error(), True),
        (TimeoutError("read timed out"), True),
        (_Status(429), True),
        (_Status(503), True),
        (RuntimeError("429 RESOURCE_EXHAUSTED: quota"), True),
        (_Status(400), False),
        (_Status(401), False),
        (ValueError("model 'gemini-x' not found"), False),
        (RuntimeError("invalid argument"), False),
    ],
)
def test_is_transient(error, transient):
    from cortino.llm import is_transient

    assert is_transient(error) is transient


def test_network_drop_is_retried_then_succeeds(monkeypatch):
    llm = _retrying_llm(monkeypatch)
    made = _scripted_agent(monkeypatch, [_dns_error(), _Status(503), _Result()])
    assert llm.text("checker", "s", "p") == "ok"
    assert llm.slept == [2, 4]  # exponential backoff
    assert [(r, a) for r, a, _ in llm.retries] == [("checker", 1), ("checker", 2)]
    assert len(made) == 3  # a fresh agent for every try


def test_retries_run_out_with_a_clear_error(monkeypatch):
    llm = _retrying_llm(monkeypatch, retries=2)
    _scripted_agent(monkeypatch, [_dns_error(), _dns_error(), _dns_error()])
    with pytest.raises(LLMError, match=r"checker: model call failed \(after 3 attempts\)"):
        llm.text("checker", "s", "p")
    assert llm.slept == [2, 4]


def test_permanent_errors_are_not_retried(monkeypatch):
    llm = _retrying_llm(monkeypatch)
    _scripted_agent(monkeypatch, [_Status(400), _Result()])
    with pytest.raises(LLMError, match="checker: model call failed: status 400"):
        llm.text("checker", "s", "p")
    assert llm.slept == [] and llm.retries == []
