"""Integration: section with unsupported claims triggers exactly one regeneration.

- A section WITH cites_at_least_one validator triggers retry when unsupported.
- A section WITHOUT cites_at_least_one validator does NOT retry.
- Exactly 2 generation-tier LLM calls total for the retried section.
- The retry prompt contains the 'unsupported or contradicted claims' suffix.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.draft.engine import DraftEngine, _section_requires_citations
from app.draft.templates.schema import DraftTemplate, SectionSpec, ValidatorSpec

UUID1 = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

SECTION_WITH_CITE = "background"
SECTION_NO_CITE = "summary"


def _make_template(require_cite_on: str) -> DraftTemplate:
    sections = [
        SectionSpec(
            name=require_cite_on,
            description="Background section",
            retrieval_key="background",
            target_length_min=50,
            target_length_max=200,
            validators=["cites_at_least_one"],
        ),
        SectionSpec(
            name=SECTION_NO_CITE,
            description="Summary section",
            retrieval_key="summary",
            target_length_min=50,
            target_length_max=200,
            validators=[],
        ),
    ]
    return DraftTemplate(
        id="test-template",
        display_name="Test Template",
        system_prompt="You are a legal assistant.",
        retrieval_queries={
            "background": "background query",
            "summary": "summary query",
        },
        sections=sections,
        validators=[],
    )


def test_section_requires_citations_with_validator():
    template = _make_template(SECTION_WITH_CITE)
    assert _section_requires_citations(template, SECTION_WITH_CITE) is True


def test_section_does_not_require_citations():
    template = _make_template(SECTION_WITH_CITE)
    assert _section_requires_citations(template, SECTION_NO_CITE) is False


def test_template_level_validator_detected():
    """cites_at_least_one as a template-level ValidatorSpec with section arg."""
    sections = [
        SectionSpec(
            name="body",
            description="Body",
            retrieval_key="body",
            target_length_min=50,
            target_length_max=200,
            validators=[],
        )
    ]
    template = DraftTemplate(
        id="t",
        display_name="T",
        system_prompt="",
        retrieval_queries={"body": "q"},
        sections=sections,
        validators=[
            ValidatorSpec(id="cites_at_least_one", args={"section": "body"})
        ],
    )
    assert _section_requires_citations(template, "body") is True
    assert _section_requires_citations(template, "other") is False


@pytest.mark.asyncio
async def test_retry_triggered_for_cites_required_section():
    """Engine calls _generate_section twice for a section that fails validation
    when it has cites_at_least_one validator.

    Flow:
      Pass 2: generate background (initial), generate summary (initial)
      Pass 3: validate background → unsupported → retry background → validate retry → supported
              validate summary → supported (no retry, no cites_at_least_one)
    """
    template = _make_template(SECTION_WITH_CITE)

    chunk_mock = MagicMock()
    chunk_mock.id = UUID1
    chunk_mock.text = "Real evidence text here."

    retriever = MagicMock()
    retriever.multi_retrieve = AsyncMock(return_value={
        "background": [chunk_mock],
        "summary": [chunk_mock],
    })

    generation_calls: list[str] = []   # "initial" or "retry" for generation tier
    validation_call_count = [0]        # mutable counter for validation tier

    initial_text = f"Claim with bad evidence [chunk:{UUID1}]."
    retry_text = f"Better claim with evidence [chunk:{UUID1}]."

    async def mock_generate(messages, *, task, schema=None, sampling=None,
                            trace_id=None, cache=True, **kw):
        resp = MagicMock()
        resp.tokens_in = 10
        resp.tokens_out = 20
        resp.cost_usd = 0.001
        resp.model_used = "mock"
        resp.latency_ms = 5
        resp.cached_hit = False

        if task == "generation":
            user_content = ""
            for m in messages:
                content = m.content if hasattr(m, "content") else m.get("content", "")
                user_content += content
            if "unsupported or contradicted" in user_content:
                generation_calls.append("retry")
                resp.text = retry_text
            else:
                generation_calls.append("initial")
                resp.text = initial_text
            resp.structured = None

        elif task == "validation":
            n = validation_call_count[0]
            validation_call_count[0] += 1
            # 1st call: background initial validation → unsupported
            # 2nd call: background retry validation → supported
            # 3rd call: summary validation → supported
            if n == 0:
                status = "unsupported"
            else:
                status = "supported"
            payload = {"results": [{"pair_idx": 0, "status": status, "reason": "mock"}]}
            resp.text = json.dumps(payload)
            resp.structured = payload

        elif task == "extraction":
            resp.structured = {"value": "test", "supporting_chunk_ids": [UUID1], "confidence": 0.9}
            resp.text = None
        else:
            resp.text = ""
            resp.structured = None

        return resp

    router = MagicMock()
    router.generate = AsyncMock(side_effect=mock_generate)

    registry = MagicMock()
    registry.get_latest = AsyncMock(return_value=template)

    repo_mock = MagicMock()
    repo_mock.mark_generating = AsyncMock()
    repo_mock.fail = AsyncMock()
    repo_mock.finalize = AsyncMock()

    session_mock = MagicMock()
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)
    session_mock.commit = AsyncMock()

    session_factory = MagicMock(return_value=session_mock)

    with patch("app.draft.engine.DraftRepo", return_value=repo_mock):
        engine = DraftEngine(retriever, router, registry, session_factory)
        await engine.generate(
            draft_id="draft-1",
            template_id="test-template",
            document_ids=["doc-1"],
            trace_id=None,
        )

    # background should have been generated twice (initial + retry),
    # summary only once
    assert generation_calls.count("initial") == 2  # one for background, one for summary
    assert generation_calls.count("retry") == 1    # exactly one retry for background
    assert "retry" in generation_calls

    # finalize should have been called once with our repo mock
    repo_mock.finalize.assert_called_once()


UUID2 = "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_retry_discarded_when_groundedness_decreases():
    """Engine discards the retry when its groundedness is strictly lower than the original.

    Scenario: initial has 2 claims — 1 supported + 1 unsupported → gnd = 0.5, triggers retry.
    Retry has 2 claims — both unsupported → gnd = 0.0 < 0.5 → original is kept.
    """
    template = _make_template(SECTION_WITH_CITE)

    chunk_a = MagicMock()
    chunk_a.id = UUID1
    chunk_a.text = "Evidence for claim A."
    chunk_b = MagicMock()
    chunk_b.id = UUID2
    chunk_b.text = "Evidence for claim B."

    retriever = MagicMock()
    retriever.multi_retrieve = AsyncMock(return_value={
        "background": [chunk_a, chunk_b],
        "summary": [chunk_a],
    })

    # Two cited claims so validation can return different per-pair statuses.
    initial_text = (
        f"First claim is good [chunk:{UUID1}]. "
        f"Second claim is bad [chunk:{UUID2}]."
    )
    retry_text = (
        f"Retry first claim is wrong [chunk:{UUID1}]. "
        f"Retry second claim is also wrong [chunk:{UUID2}]."
    )

    validation_call_count = [0]

    async def mock_generate(messages, *, task, schema=None, sampling=None,
                            trace_id=None, cache=True, **kw):
        resp = MagicMock()
        resp.tokens_in = 10
        resp.tokens_out = 20
        resp.cost_usd = 0.001
        resp.model_used = "mock"
        resp.latency_ms = 5
        resp.cached_hit = False

        if task == "generation":
            user_content = "".join(
                m.content if hasattr(m, "content") else m.get("content", "")
                for m in messages
            )
            resp.text = retry_text if "unsupported or contradicted" in user_content else initial_text
            resp.structured = None

        elif task == "validation":
            n = validation_call_count[0]
            validation_call_count[0] += 1
            if n == 0:
                # background initial: 2 pairs — first supported, second unsupported → gnd=0.5
                payload = {"results": [
                    {"pair_idx": 0, "status": "supported", "reason": "ok"},
                    {"pair_idx": 1, "status": "unsupported", "reason": "bad"},
                ]}
            elif n == 1:
                # background retry: 2 pairs — both unsupported → gnd=0.0 < 0.5
                payload = {"results": [
                    {"pair_idx": 0, "status": "unsupported", "reason": "worse"},
                    {"pair_idx": 1, "status": "unsupported", "reason": "worse"},
                ]}
            else:
                # summary
                payload = {"results": [{"pair_idx": 0, "status": "supported", "reason": "ok"}]}
            resp.text = json.dumps(payload)
            resp.structured = payload

        elif task == "extraction":
            resp.structured = {"value": "test", "supporting_chunk_ids": [UUID1], "confidence": 0.9}
            resp.text = None
        else:
            resp.text = ""
            resp.structured = None

        return resp

    router = MagicMock()
    router.generate = AsyncMock(side_effect=mock_generate)
    registry = MagicMock()
    registry.get_latest = AsyncMock(return_value=template)

    finalize_kwargs: dict = {}

    repo_mock = MagicMock()
    repo_mock.mark_generating = AsyncMock()
    repo_mock.fail = AsyncMock()

    async def capture_finalize(*args, **kwargs):
        finalize_kwargs.update(kwargs)
    repo_mock.finalize = AsyncMock(side_effect=capture_finalize)

    session_mock = MagicMock()
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)
    session_mock.commit = AsyncMock()
    session_factory = MagicMock(return_value=session_mock)

    with patch("app.draft.engine.DraftRepo", return_value=repo_mock):
        engine = DraftEngine(retriever, router, registry, session_factory)
        await engine.generate(
            draft_id="draft-3",
            template_id="test-template",
            document_ids=["doc-1"],
            trace_id=None,
        )

    # finalize should have been called with the ORIGINAL section text (retry gnd 0.0 < 0.5)
    sections_arg = finalize_kwargs.get("sections")
    assert sections_arg is not None, "finalize was not called with sections= kwarg"
    background_sec = next(
        (s for s in sections_arg if s.section_name == SECTION_WITH_CITE), None
    )
    assert background_sec is not None
    assert background_sec.text == initial_text, (
        f"Expected original text to be kept, but got: {background_sec.text!r}"
    )
