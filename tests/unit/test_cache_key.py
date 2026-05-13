from pydantic import BaseModel

from app.llm.cache import build_key
from app.llm.types import Message, SamplingParams


class _Schema(BaseModel):
    foo: str
    bar: int


def _msg(content: str = "hello world") -> list[Message]:
    return [
        Message(role="system", content="be precise"),
        Message(role="user", content=content),
    ]


def test_identical_inputs_produce_identical_key():
    k1 = build_key(
        model_id="m",
        messages=_msg(),
        schema=_Schema,
        sampling=SamplingParams(),
    )
    k2 = build_key(
        model_id="m",
        messages=_msg(),
        schema=_Schema,
        sampling=SamplingParams(),
    )
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex


def test_single_character_change_changes_key():
    base = build_key(
        model_id="m",
        messages=_msg("hello world"),
        schema=None,
        sampling=SamplingParams(),
    )
    changed = build_key(
        model_id="m",
        messages=_msg("hello worlD"),
        schema=None,
        sampling=SamplingParams(),
    )
    assert base != changed


def test_schema_change_changes_key():
    class _Other(BaseModel):
        baz: str

    a = build_key(model_id="m", messages=_msg(), schema=_Schema, sampling=SamplingParams())
    b = build_key(model_id="m", messages=_msg(), schema=_Other, sampling=SamplingParams())
    assert a != b


def test_sampling_change_changes_key():
    a = build_key(model_id="m", messages=_msg(), schema=None, sampling=SamplingParams(temperature=0.2))
    b = build_key(model_id="m", messages=_msg(), schema=None, sampling=SamplingParams(temperature=0.8))
    assert a != b


def test_model_id_change_changes_key():
    a = build_key(model_id="m1", messages=_msg(), schema=None, sampling=SamplingParams())
    b = build_key(model_id="m2", messages=_msg(), schema=None, sampling=SamplingParams())
    assert a != b


def test_appended_rules_change_changes_key():
    """NN-7: appending rules to system prompt (without bumping any version) must change the cache key."""
    base = build_key(
        model_id="m",
        messages=[Message(role="system", content="you are a lawyer"), Message(role="user", content="x")],
        schema=None,
        sampling=SamplingParams(),
    )
    with_rules = build_key(
        model_id="m",
        messages=[
            Message(role="system", content="you are a lawyer\n\nRule 1: cite statutes."),
            Message(role="user", content="x"),
        ],
        schema=None,
        sampling=SamplingParams(),
    )
    assert base != with_rules
