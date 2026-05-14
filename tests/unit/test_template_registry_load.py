"""Tests for TemplateRegistry.load_from_disk()."""
from __future__ import annotations

import os
import tempfile

import pytest

from app.draft.templates.registry import TemplateRegistry, TemplateValidationError


def test_all_production_yamls_parse():
    registry = TemplateRegistry()
    config_dir = os.path.join(os.path.dirname(__file__), "..", "..", "config", "templates")
    registry.load_from_disk(config_dir)

    assert "case_fact_summary" in registry._templates
    assert "title_review_summary" in registry._templates
    assert "document_checklist" in registry._templates


def test_invalid_yaml_raises_template_validation_error():
    registry = TemplateRegistry()

    with tempfile.TemporaryDirectory() as tmpdir:
        bad_file = os.path.join(tmpdir, "bad.yaml")
        with open(bad_file, "w") as f:
            f.write("id: [invalid yaml: {unclosed bracket\n")

        with pytest.raises(TemplateValidationError) as exc_info:
            registry.load_from_disk(tmpdir)

        assert "bad.yaml" in str(exc_info.value)


def test_missing_required_field_raises_template_validation_error():
    registry = TemplateRegistry()

    with tempfile.TemporaryDirectory() as tmpdir:
        bad_file = os.path.join(tmpdir, "missing.yaml")
        with open(bad_file, "w") as f:
            # Missing required 'system_prompt' and 'sections'
            f.write("id: test\ndisplay_name: Test\n")

        with pytest.raises(TemplateValidationError) as exc_info:
            registry.load_from_disk(tmpdir)

        assert "missing.yaml" in str(exc_info.value)
