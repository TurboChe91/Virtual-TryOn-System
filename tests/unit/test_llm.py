"""Unit tests for LLM helpers: JSON extraction, identity/style parsing, channel pick."""

from __future__ import annotations

from pathlib import Path

import pytest

from lunelle.llm import (
    LLMUnavailable,
    build_llm_chat,
    extract_json,
    identify_nail_identities,
    style_fields_from_image,
)
from lunelle.profiles import ProfileService


class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_with_prose(self):
        text = 'Sure! Here you go:\n```json\n{"name": "Rose"}\n```\nHope that helps.'
        assert extract_json(text) == {"name": "Rose"}

    def test_embedded_object(self):
        assert extract_json('prefix {"x": [1, 2]} suffix') == {"x": [1, 2]}

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            extract_json("no json here at all")


def fake_chat(reply: str):
    def chat(system: str, user: str, images: list[Path]) -> str:
        assert images, "vision calls must carry at least one image"
        return reply
    return chat


IDENTITY_REPLY = "\n".join(
    [f"- nail-{i:02d}: milky white base, one pearl. Shape hint: medium almond." for i in range(1, 11)]
) + "\nSET-WIDE: palette locked to milky white and pearl silver."


class TestParsers:
    def test_identity_roundtrip(self, tmp_path):
        image = tmp_path / "plan.png"
        image.write_bytes(b"x" * 200)
        text = identify_nail_identities(fake_chat(IDENTITY_REPLY), image)
        assert text.count("nail-") == 10
        assert text.endswith("silver.")

    def test_identity_missing_nails_rejected(self, tmp_path):
        image = tmp_path / "plan.png"
        image.write_bytes(b"x" * 200)
        with pytest.raises(ValueError):
            identify_nail_identities(fake_chat("just nail-01 and nothing else"), image)

    def test_style_fields_filters_junk(self, tmp_path):
        image = tmp_path / "design.png"
        image.write_bytes(b"x" * 200)
        reply = (
            '{"name": "Pearl French", "description": "elegant set", '
            '"base_colors": ["milky white", ""], "elements": ["pearl", 42], '
            '"shape": "almond", "length": "not-a-length", "avoid": []}'
        )
        fields = style_fields_from_image(fake_chat(reply), image)
        assert fields["name"] == "Pearl French"
        assert fields["base_colors"] == ["milky white"]
        assert fields["elements"] == ["pearl", "42"]
        assert fields["shape"] == "almond"
        # invalid enum values survive here; the endpoint drops them against vocab
        assert fields["length"] == "not-a-length"


class TestChannelSelection:
    def test_unavailable_without_profile_or_env(self, config, db):
        with pytest.raises(LLMUnavailable):
            build_llm_chat(config, db)

    def test_active_llm_profile_wins(self, config, db):
        profiles = ProfileService(db)
        doc = profiles.create(
            name="vision", base_url="https://llm.example.com/v1",
            api_key="sk-test-key-12345", model="gpt-5.6-sol", kind="llm",
        )
        profiles.activate(doc["profile_id"])
        chat = build_llm_chat(config, db)
        assert callable(chat)

    def test_image_profile_does_not_satisfy_llm(self, config, db):
        profiles = ProfileService(db)
        doc = profiles.create(
            name="imgchan", base_url="https://img.example.com/v1",
            api_key="sk-test-key-12345", model="gpt-image-2", kind="image",
        )
        profiles.activate(doc["profile_id"])
        with pytest.raises(LLMUnavailable):
            build_llm_chat(config, db)
