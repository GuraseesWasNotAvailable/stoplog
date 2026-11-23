from types import SimpleNamespace

import pytest

from stoplog.config import Settings
from stoplog.llm import AnthropicLLM, LLMError, OpenAILLM, make_llm
from stoplog.models import Narrative


class Recorder:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def openai_client(response):
    rec = Recorder(response)
    return SimpleNamespace(responses=rec), rec


def anthropic_client(response):
    rec = Recorder(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=rec)), rec


def test_openai_request_shape_and_parsed_output():
    parsed = Narrative(insights=[])
    client, rec = openai_client(SimpleNamespace(incomplete_details=None, output_parsed=parsed, output=[]))
    out = OpenAILLM("k", image_detail="original", client=client).structured(
        model="gpt-6.2-sol", system="sys", text="hi", schema=Narrative, image_jpeg=b"\xff\xd8jpg")
    assert out is parsed
    assert rec.kwargs["text_format"] is Narrative and rec.kwargs["instructions"] == "sys"
    assert rec.kwargs["store"] is False
    parts = rec.kwargs["input"][0]["content"]
    assert parts[0] == {"type": "input_text", "text": "hi"}
    assert parts[1]["type"] == "input_image" and parts[1]["detail"] == "original"
    assert parts[1]["image_url"].startswith("data:image/jpeg;base64,")


def test_openai_refusal_and_incomplete():
    refusal = SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal", refusal="no")])
    client, _ = openai_client(SimpleNamespace(incomplete_details=None, output_parsed=None, output=[refusal]))
    with pytest.raises(LLMError) as e:
        OpenAILLM("k", client=client).structured(model="m", system="s", text="t", schema=Narrative)
    assert not e.value.retryable
    client, _ = openai_client(SimpleNamespace(incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                                              output_parsed=None, output=[]))
    with pytest.raises(LLMError) as e:
        OpenAILLM("k", client=client).structured(model="m", system="s", text="t", schema=Narrative)
    assert e.value.retryable


def test_anthropic_request_shape():
    parsed = Narrative(insights=[])
    client, rec = anthropic_client(SimpleNamespace(stop_reason="end_turn", parsed_output=parsed))
    out = AnthropicLLM("k", client=client).structured(
        model="claude-opus-5-5", system="sys", text="hi", schema=Narrative, image_jpeg=b"\xff\xd8", effort="high")
    assert out is parsed
    k = rec.kwargs
    assert k["output_format"] is Narrative and k["output_config"] == {"effort": "high"}
    assert k["betas"] == ["server-side-fallback-2026-07-01"] and k["fallbacks"] == "default"
    content = k["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert content[1] == {"type": "text", "text": "hi"}
    assert "thinking" not in k and "temperature" not in k


def test_haiku_gets_no_fallback_beta():
    client, rec = anthropic_client(SimpleNamespace(stop_reason="end_turn", parsed_output=Narrative(insights=[])))
    AnthropicLLM("k", client=client).structured(model="claude-haiku-5-5", system="s", text="t",
                                                schema=Narrative, effort="low")
    assert "betas" not in rec.kwargs and "fallbacks" not in rec.kwargs
    assert rec.kwargs["output_config"] == {"effort": "low"}


def test_anthropic_refusal_is_not_retried():
    client, _ = anthropic_client(SimpleNamespace(stop_reason="refusal", parsed_output=None,
                                                 stop_details=SimpleNamespace(category="cyber")))
    with pytest.raises(LLMError) as e:
        AnthropicLLM("k", client=client).structured(model="m", system="s", text="t", schema=Narrative)
    assert not e.value.retryable and "cyber" in str(e.value)


def test_provider_selection():
    assert Settings(openai_api_key="a").resolved_provider() == "openai"
    assert Settings(anthropic_api_key="b").resolved_provider() == "anthropic"
    assert Settings(openai_api_key="a", anthropic_api_key="b").resolved_provider() == "openai"
    assert Settings(provider="anthropic", openai_api_key="a", anthropic_api_key="b").extraction_model() == "claude-haiku-5-5"
    assert Settings(openai_api_key="a").extraction_model() == "gpt-6.2-sol"
    assert Settings(openai_api_key="a", model="gpt-6.1-terra").narrative_model() == "gpt-6.1-terra"
    with pytest.raises(RuntimeError):
        Settings().resolved_provider()
    with pytest.raises(RuntimeError):
        Settings(provider="anthropic", openai_api_key="a").resolved_provider()


def test_make_llm_builds_real_clients_without_network():
    assert make_llm(Settings(openai_api_key="sk-test")).provider == "openai"
    assert make_llm(Settings(anthropic_api_key="sk-ant-test")).provider == "anthropic"
