"""Unit tests for runtime API channel profiles and provider resolution."""

from __future__ import annotations

import pytest

from lunelle.errors import ConflictError, NotFoundError
from lunelle.profiles import ProfileService, ProviderResolver
from lunelle.providers.mock import MockImageProvider
from lunelle.providers.openai_compat import OpenAICompatProvider


@pytest.fixture
def profiles(db):
    return ProfileService(db)


def make_profile(profiles, name="chan-a", **overrides):
    fields = dict(
        name=name,
        base_url="https://api.example.com/v1",
        api_key="sk-unit-test-key-000",
        model="gpt-image-2",
    )
    fields.update(overrides)
    return profiles.create(**fields)


class TestProfileCrud:
    def test_create_redacts_key(self, profiles):
        doc = make_profile(profiles)
        assert doc["api_key"] == {"fingerprint": doc["api_key"]["fingerprint"], "last4": "-000"}
        assert "sk-unit" not in str(doc)
        assert doc["is_active"] is False
        assert doc["supports_mask"] is False

    def test_duplicate_name_conflicts(self, profiles):
        make_profile(profiles)
        with pytest.raises(ConflictError):
            make_profile(profiles)

    def test_https_required(self, profiles):
        with pytest.raises(ValueError):
            make_profile(profiles, name="bad", base_url="http://plain.example.com/v1")

    def test_update_and_missing(self, profiles):
        doc = make_profile(profiles)
        updated = profiles.update(doc["profile_id"], {"model": "gpt-image-1.5", "supports_mask": True})
        assert updated["model"] == "gpt-image-1.5"
        assert updated["supports_mask"] is True
        with pytest.raises(NotFoundError):
            profiles.update("pr_nope", {"model": "x"})

    def test_activate_is_exclusive(self, profiles):
        a = make_profile(profiles, name="a")
        b = make_profile(profiles, name="b")
        profiles.activate(a["profile_id"])
        profiles.activate(b["profile_id"])
        states = {p["name"]: p["is_active"] for p in profiles.list_profiles()}
        assert states == {"a": False, "b": True}

    def test_delete_active_refused(self, profiles):
        a = make_profile(profiles)
        profiles.activate(a["profile_id"])
        with pytest.raises(ConflictError):
            profiles.delete(a["profile_id"])
        profiles.deactivate_all()
        profiles.delete(a["profile_id"])
        assert profiles.list_profiles() == []


class TestProviderResolver:
    def test_fallback_without_active_profile(self, config, db, profiles):
        fallback = MockImageProvider(allowed=True)
        resolver = ProviderResolver(config, db, fallback)
        provider, active = resolver.resolve()
        assert provider is fallback
        assert active is None

    def test_active_profile_builds_provider_and_caches(self, config, db, profiles):
        doc = make_profile(profiles)
        profiles.activate(doc["profile_id"])
        resolver = ProviderResolver(config, db, MockImageProvider(allowed=True))
        provider1, active = resolver.resolve()
        assert isinstance(provider1, OpenAICompatProvider)
        assert active["name"] == "chan-a"
        assert active["api_key"]["last4"] == "-000"
        provider2, _ = resolver.resolve()
        assert provider2 is provider1  # unchanged connection fields -> cached instance
        profiles.update(doc["profile_id"], {"reference_mode": "openai-edits"})
        provider3, _ = resolver.resolve()
        assert provider3 is not provider1  # connection field change invalidates the cache
