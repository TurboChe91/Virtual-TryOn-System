"""The verifier that gates the provider call.

`compare_to_snapshot` returning [] is the precondition for spending money. Every
field it checks was, at some point, able to drift from the snapshot unnoticed — so
each one has a test that it is actually caught.
"""

from __future__ import annotations

import hashlib

import pytest

from lunelle.inputs import (
    DEFERRABLE_ROLES,
    PROMPT_TRANSFORMS,
    ROLES,
    channel_fingerprint,
    compare_to_snapshot,
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def snapshot(**overrides) -> dict:
    doc = {
        "prompt": "copy each tile onto the finger its caption names",
        "size": [2224, 1664],
        "channel": {"model": "gpt-image-2", "channel_fingerprint": "abc123"},
        "input_assets": [
            {"role": "view_plan", "digest": "a" * 64},
            {"role": "base_hand", "digest": "b" * 64},
        ],
        "deferred_inputs": [],
    }
    doc.update(overrides)
    return doc


def manifest(**overrides) -> dict:
    doc = {
        "prompt_sha256": sha("copy each tile onto the finger its caption names"),
        "size_requested": [2224, 1664],
        "model": "gpt-image-2",
        "provider": "openai-compat",
        "channel_fingerprint": "abc123",
        "prompt_transform": "none",
        "input_images": [
            {"index": 1, "role": "view_plan", "sha256": "a" * 64},
            {"index": 2, "role": "base_hand", "sha256": "b" * 64},
        ],
    }
    doc.update(overrides)
    return doc


class TestMatchingRequestPasses:
    def test_identical_plan_and_request_has_no_differences(self):
        assert compare_to_snapshot(snapshot(), manifest()) == []


class TestEachDriftIsCaught:
    def test_prompt_drift(self):
        problems = compare_to_snapshot(
            snapshot(), manifest(prompt_sha256=sha("something else")))
        assert any("prompt differs" in p for p in problems)

    def test_size_drift(self):
        problems = compare_to_snapshot(snapshot(), manifest(size_requested=[2048, 2048]))
        assert any("size differs" in p for p in problems)

    def test_model_drift(self):
        problems = compare_to_snapshot(snapshot(), manifest(model="doubao-seedream-4-5"))
        assert any("model differs" in p for p in problems)

    def test_image_content_drift(self):
        """The one that matters most: right role, wrong bytes."""
        images = [
            {"index": 1, "role": "view_plan", "sha256": "f" * 64},
            {"index": 2, "role": "base_hand", "sha256": "b" * 64},
        ]
        problems = compare_to_snapshot(snapshot(), manifest(input_images=images))
        assert any("input 1 (view_plan) content differs" in p for p in problems)

    def test_role_swap_is_caught_even_with_the_right_digests(self):
        """Sending the base hand as Image 1 is the 2026-08-06 failure in miniature."""
        images = [
            {"index": 1, "role": "base_hand", "sha256": "b" * 64},
            {"index": 2, "role": "view_plan", "sha256": "a" * 64},
        ]
        problems = compare_to_snapshot(snapshot(), manifest(input_images=images))
        assert any("input 1 role differs" in p for p in problems)

    def test_a_raw_plan_sent_where_a_view_plan_was_promised(self):
        """Exactly the incident: the prompt says view-plan, the bytes are the plan."""
        images = [
            {"index": 1, "role": "view_plan", "sha256": sha("the raw 2x5 plan")},
            {"index": 2, "role": "base_hand", "sha256": "b" * 64},
        ]
        problems = compare_to_snapshot(snapshot(), manifest(input_images=images))
        assert problems, "sending different bytes under the promised role must be caught"

    def test_missing_image(self):
        images = [{"index": 1, "role": "view_plan", "sha256": "a" * 64}]
        problems = compare_to_snapshot(snapshot(), manifest(input_images=images))
        assert any("fewer inputs" in p for p in problems)

    def test_an_undeclared_extra_image(self):
        images = manifest()["input_images"] + [
            {"index": 3, "role": "correction_detail", "sha256": "c" * 64},
        ]
        problems = compare_to_snapshot(snapshot(), manifest(input_images=images))
        assert any("not in the snapshot" in p for p in problems)


class TestDeferredInputs:
    def test_a_declared_deferred_input_is_allowed(self):
        """A wearing shot's grid cannot be frozen; it can only be declared."""
        plan = snapshot(input_assets=[], deferred_inputs=[{"role": "grid"}])
        sent = manifest(input_images=[
            {"index": 1, "role": "grid", "sha256": "d" * 64},
        ])
        assert compare_to_snapshot(plan, sent) == []

    def test_a_deferred_input_that_did_not_materialise_is_allowed(self):
        """No grid yet means a text-only render, not a mismatch."""
        plan = snapshot(input_assets=[], deferred_inputs=[{"role": "grid"}])
        assert compare_to_snapshot(plan, manifest(input_images=[])) == []

    def test_an_undeclared_role_cannot_ride_in_as_deferred(self):
        plan = snapshot(input_assets=[], deferred_inputs=[{"role": "grid"}])
        sent = manifest(input_images=[
            {"index": 1, "role": "style_reference", "sha256": "d" * 64},
        ])
        assert compare_to_snapshot(plan, sent), "only the declared role may be deferred"

    def test_only_grid_is_deferrable(self):
        """The set is closed on purpose: everything else must be frozen."""
        assert DEFERRABLE_ROLES == {"grid"}
        assert DEFERRABLE_ROLES <= set(ROLES)


class TestPromptTransforms:
    def test_stripping_is_verified_by_recomputation(self):
        """A declared transform must produce exactly the frozen prompt's transform."""
        from lunelle.prompts import strip_reference_block

        frozen = "IMAGE 1 is the reference.\n\nRender the nails."
        plan = snapshot(prompt=frozen)
        sent = manifest(prompt_sha256=sha(strip_reference_block(frozen)),
                        prompt_transform="strip_reference_block")
        assert compare_to_snapshot(plan, sent) == []

    def test_naming_a_transform_does_not_excuse_an_arbitrary_prompt(self):
        """The point of recomputing rather than trusting the declaration."""
        plan = snapshot(prompt="IMAGE 1 is the reference.\n\nRender the nails.")
        sent = manifest(prompt_sha256=sha("a completely different instruction"),
                        prompt_transform="strip_reference_block")
        assert compare_to_snapshot(plan, sent), "a named transform is not a blank cheque"

    def test_an_unknown_transform_is_refused(self):
        problems = compare_to_snapshot(snapshot(), manifest(prompt_transform="rewrite"))
        assert any("not a permitted transform" in p for p in problems)

    def test_the_registry_is_small_and_closed(self):
        assert set(PROMPT_TRANSFORMS) == {"none", "strip_reference_block"}


class TestChannelFingerprint:
    def test_same_channel_same_fingerprint(self):
        profile = {"profile_id": "pr_1", "base_url": "https://a.example/v1",
                   "key_fingerprint": "aabbccdd"}
        assert channel_fingerprint(profile) == channel_fingerprint(dict(profile))

    @pytest.mark.parametrize("field,value", [
        ("profile_id", "pr_2"),
        ("base_url", "https://b.example/v1"),
        ("key_fingerprint", "eeff0011"),
    ])
    def test_any_channel_field_changes_it(self, field, value):
        profile = {"profile_id": "pr_1", "base_url": "https://a.example/v1",
                   "key_fingerprint": "aabbccdd"}
        assert channel_fingerprint({**profile, field: value}) != channel_fingerprint(profile)

    def test_no_profile_is_the_env_fallback(self):
        assert channel_fingerprint(None) == "env-fallback"

    def test_the_fingerprint_carries_no_secret(self):
        profile = {"profile_id": "pr_1", "base_url": "https://a.example/v1",
                   "key_fingerprint": "aabbccdd", "api_key": "sk-real-secret-value"}
        rendered = channel_fingerprint(profile)
        assert "sk-" not in rendered
        assert "aabbccdd" not in rendered
