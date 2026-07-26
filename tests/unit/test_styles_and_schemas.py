from __future__ import annotations

import pytest
from pydantic import ValidationError

from lunelle.schemas import StyleCreateRequest, StyleSpec
from lunelle.styles import StyleInputError, build_style_spec, derive_sku, parse_with_llm, slugify


def req(**kw) -> StyleCreateRequest:
    return StyleCreateRequest(**kw)


class TestDeterministicParsing:
    def test_natural_language_full(self):
        outcome = build_style_spec(
            req(description="milky white almond nails with pearl, gold line and french tip, "
                            "glossy translucent finish, long length"),
            taken_skus=set(),
        )
        spec = outcome.spec
        assert spec.shape == "almond"
        assert spec.length == "long"
        assert "milky white" in spec.base_colors
        assert "pearl" in spec.elements and "french tip" in spec.elements
        assert "glossy" in spec.texture
        assert outcome.parser == "deterministic"

    def test_defaults_applied(self):
        outcome = build_style_spec(req(name="Plain Set"), taken_skus=set())
        spec = outcome.spec
        assert spec.shape == "almond"
        assert spec.length == "medium"
        assert spec.base_colors == ["nude pink"]
        assert spec.texture == ["glossy"]
        assert any("default" in w for w in outcome.warnings)

    def test_chinese_keywords(self):
        outcome = build_style_spec(
            req(name="Fang", description="方形 短款 磨砂"), taken_skus=set()
        )
        assert outcome.spec.shape == "square"
        assert outcome.spec.length == "short"
        assert "matte" in outcome.spec.texture

    def test_structured_fields_beat_description(self):
        outcome = build_style_spec(
            req(description="red coffin nails", shape="almond", base_colors=["black"]),
            taken_skus=set(),
        )
        assert outcome.spec.shape == "almond"
        assert outcome.spec.base_colors == ["black"]

    def test_empty_input_rejected(self):
        with pytest.raises(StyleInputError):
            build_style_spec(req(), taken_skus=set())

    def test_unknown_shape_rejected(self):
        with pytest.raises(StyleInputError, match="unknown shape"):
            build_style_spec(req(name="x", shape="triangle"), taken_skus=set())

    def test_unknown_length_rejected(self):
        with pytest.raises(StyleInputError, match="unknown length"):
            build_style_spec(req(name="x", length="gigantic"), taken_skus=set())

    def test_unknown_skin_tone_rejected(self):
        with pytest.raises(StyleInputError, match="unknown skin_tone"):
            build_style_spec(req(name="x", skin_tone="rainbow"), taken_skus=set())

    def test_overlong_description_rejected_by_schema(self):
        with pytest.raises(ValidationError):
            req(description="x" * 2001)


class TestSku:
    def test_derive_from_name(self):
        assert derive_sku(req(name="Pearl French!"), set()) == "nail-pearl-french"

    def test_derive_unique_suffix(self):
        taken = {"nail-pearl-french"}
        assert derive_sku(req(name="Pearl French"), taken) == "nail-pearl-french-2"

    def test_explicit_sku_conflict(self):
        from lunelle.errors import ConflictError

        with pytest.raises(ConflictError, match="already exists"):
            derive_sku(req(sku="nail-a1", name="x"), {"nail-a1"})

    def test_invalid_sku_rejected(self):
        with pytest.raises(StyleInputError):
            derive_sku(req(sku="BAD SKU!!", name="x"), set())

    def test_slugify_transliterates_unicode(self):
        assert slugify("Pérle Fränçh 001") == "perle-franch-001"
        assert slugify("纯中文名") == ""


class TestStyleSpecValidation:
    def test_too_many_elements(self):
        with pytest.raises(ValidationError):
            StyleSpec(sku="nail-x", name="x", elements=[f"e{i}" for i in range(11)])

    def test_item_too_long(self):
        with pytest.raises(ValidationError):
            StyleSpec(sku="nail-x", name="x", base_colors=["y" * 61])

    def test_dedupe_and_clean(self):
        spec = StyleSpec(sku="nail-x", name="x", elements=["Pearl ", "pearl", "  bow "])
        assert spec.elements == ["Pearl", "bow"]

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            StyleSpec(sku="nail-x", name="x", hacker_field="boom")


class TestLLMParsing:
    def test_valid_llm_json(self):
        def chat_fn(system, user):
            return ('{"name":"Dark Rose","base_colors":["black"],"elements":["rose"],'
                    '"texture":["matte"],"shape":"coffin","length":"long",'
                    '"visual_style":"gothic","avoid":["glitter"],"skin_tone":"tan"}')

        spec, warnings = parse_with_llm(req(description="dark rose"), "nail-dark", chat_fn=chat_fn)
        assert spec.shape == "coffin"
        assert spec.skin_tone == "tan"
        assert spec.avoid == ["glitter"]

    def test_llm_code_fence_stripped(self):
        def chat_fn(system, user):
            return '```json\n{"name":"A","base_colors":["red"],"shape":"oval","length":"short"}\n```'

        spec, _ = parse_with_llm(req(description="x"), "nail-a", chat_fn=chat_fn)
        assert spec.shape == "oval"

    def test_llm_garbage_falls_back(self):
        outcome = build_style_spec(
            req(description="red square nails", use_llm=True),
            taken_skus=set(),
            chat_fn=lambda s, u: "I cannot help with that.",
        )
        assert outcome.parser == "deterministic"
        assert any("llm parse failed" in w for w in outcome.warnings)
        assert outcome.spec.shape == "square"

    def test_llm_invalid_enum_falls_back(self):
        outcome = build_style_spec(
            req(description="red nails", use_llm=True),
            taken_skus=set(),
            chat_fn=lambda s, u: '{"name":"A","base_colors":["red"],"shape":"hexagon","length":"short"}',
        )
        assert outcome.parser == "deterministic"
