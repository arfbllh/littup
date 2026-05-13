from app.llm.cache import build_cache_key
from app.llm.types import Message, SamplingParams


def _msgs(system: str = "you are a helpful assistant.", user: str = "hello") -> list[Message]:
    return [Message(role="system", content=system), Message(role="user", content=user)]


def test_identical_inputs_produce_identical_key():
    s = SamplingParams(max_tokens=128, temperature=0.0)
    k1 = build_cache_key("p:m", _msgs(), None, s)
    k2 = build_cache_key("p:m", _msgs(), None, s)
    assert k1 == k2


def test_changing_one_character_changes_key():
    s = SamplingParams()
    k1 = build_cache_key("p:m", _msgs(system="A"), None, s)
    k2 = build_cache_key("p:m", _msgs(system="B"), None, s)
    assert k1 != k2


def test_schema_dict_order_does_not_change_key():
    s = SamplingParams()
    schema_a = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}
    schema_b = {"properties": {"b": {"type": "integer"}, "a": {"type": "string"}}, "type": "object"}
    k1 = build_cache_key("p:m", _msgs(), schema_a, s)
    k2 = build_cache_key("p:m", _msgs(), schema_b, s)
    assert k1 == k2


def test_model_id_in_key():
    s = SamplingParams()
    k1 = build_cache_key("p:m1", _msgs(), None, s)
    k2 = build_cache_key("p:m2", _msgs(), None, s)
    assert k1 != k2


def test_sampling_params_in_key():
    k1 = build_cache_key("p:m", _msgs(), None, SamplingParams(temperature=0.0))
    k2 = build_cache_key("p:m", _msgs(), None, SamplingParams(temperature=0.5))
    assert k1 != k2
