"""Tests for opt-in notification preferences and fail-closed validation."""

import pytest

from custom_components.daylight_calendar_import.notification_preferences import (
    NOTIFICATION_CLASSES, NotificationPreferences, NotificationPreferencesError,
    normalize_notification_preferences, notification_preferences_snapshot,
)


def test_default_policy_never_delivers():
    policy = normalize_notification_preferences({})
    assert policy == NotificationPreferences()
    assert NOTIFICATION_CLASSES == frozenset({
        "review_ready", "calendar_created", "calendar_create_failed",
        "calendar_write_uncertain", "conflict_detected", "parse_failed",
    })
    assert all(not policy.permits(kind) for kind in NOTIFICATION_CLASSES)
    assert notification_preferences_snapshot(policy) == {
        "enabled": False, "classes": [], "target": None,
    }


def test_explicit_opt_in_selects_only_user_enabled_classes():
    policy = normalize_notification_preferences({
        "enabled": True, "target": "notify.phone",
        "classes": ["review_ready", "calendar_created", "review_ready"],
    })
    assert policy.permits("review_ready")
    assert policy.permits("calendar_created")
    assert not policy.permits("parse_failed")
    assert notification_preferences_snapshot(policy)["classes"] == [
        "calendar_created", "review_ready",
    ]


def test_disabled_keeps_preferences_without_delivery():
    policy = normalize_notification_preferences({
        "enabled": False, "target": "notify.phone", "classes": ["parse_failed"],
    })
    assert not policy.permits("parse_failed")


@pytest.mark.parametrize("invalid", [
    None, [], {"enabled": 1}, {"enabled": True},
    {"enabled": True, "target": "notify.phone", "classes": ["unknown"]},
    {"enabled": True, "target": "notify.phone", "classes": "review_ready"},
    {"target": "script.secrets"}, {"target": "notify. bad"},
    {"target": "notify." + "a" * 129}, {"target": 3}, {"unknown": True},
    {"target": "notify."}, {"target": "notify.phone/backup"},
    {"target": "notify.phone.extra"}, {"target": "notify.phone!"},
    {"target": "notify.Phone"}, {"target": "notify.phone\n"},
    {"target": "notify._phone"}, {"target": "notify.phone_"},
    {"classes": [42]},
])
def test_invalid_preferences_fail_closed(invalid):
    with pytest.raises(NotificationPreferencesError):
        normalize_notification_preferences(invalid)


def test_opt_in_without_selected_classes_delivers_nothing():
    policy = normalize_notification_preferences({
        "enabled": True, "target": "notify.phone",
    })
    assert not any(policy.permits(kind) for kind in NOTIFICATION_CLASSES)


def test_valid_notify_entity_slug_is_allowed():
    policy = normalize_notification_preferences({
        "enabled": True, "target": "notify.mobile_app_phone_2",
        "classes": ["review_ready"],
    })
    assert policy.target == "notify.mobile_app_phone_2"
    assert policy.permits("review_ready")


@pytest.mark.parametrize("collection", [
    ["review_ready", "parse_failed"],
    ("review_ready", "parse_failed"),
    frozenset({"review_ready", "parse_failed"}),
])
def test_supported_category_collections(collection):
    policy = normalize_notification_preferences({
        "enabled": True, "target": "notify.phone", "classes": collection,
    })
    assert policy.classes == frozenset({"review_ready", "parse_failed"})
    assert policy.permits("review_ready")
    assert policy.permits("parse_failed")
    assert not policy.permits("conflict_detected")


def test_exact_notify_entity_length_boundary():
    # The full HA entity ID, including the seven-character notify. prefix,
    # may be at most 128 characters in the Daylight settings contract.
    maximum = "notify." + "a" * (128 - len("notify."))
    assert len(maximum) == 128
    assert normalize_notification_preferences({"target": maximum}).target == maximum
    with pytest.raises(NotificationPreferencesError, match="Notification target must be a notify entity"):
        normalize_notification_preferences({"target": maximum + "a"})


@pytest.mark.parametrize(("bad", "error"), [
    (None, "Notification preferences must be an object"),
    ({"extra": 1}, "Unknown notification setting"),
    ({"enabled": "yes"}, "Notification enabled must be a boolean"),
    ({"classes": ["unknown"]}, "Unknown notification class"),
    ({"classes": "review_ready"}, "Unknown notification class"),
    ({"target": "notify._phone"}, "Notification target must be a notify entity"),
    ({"target": "notify.phone_"}, "Notification target must be a notify entity"),
    ({"enabled": True}, "Select a notification target before enabling"),
])
def test_validation_messages_are_stable(bad, error):
    with pytest.raises(NotificationPreferencesError) as caught:
        normalize_notification_preferences(bad)
    assert str(caught.value) == error


@pytest.mark.parametrize("valid", [
    "notify.phone١",
    "notify.९_٣",  # HA's Unicode decimal digit rule, excluding underscore edges.
    "notify.n1_2",
])
def test_ha_valid_unicode_decimal_digits_in_notify_ids(valid):
    policy = normalize_notification_preferences({
        "enabled": True, "target": valid, "classes": ["review_ready"],
    })
    assert policy.target == valid
    assert policy.permits("review_ready")


@pytest.mark.parametrize("suffix", ["\n", "\r", "\t", "\x00", "\u2028"])
def test_notify_target_rejects_actual_nonprintable_characters(suffix):
    """An HA regex with a '$' anchor can accept a real final newline."""
    target = "notify.phone" + suffix
    assert not target.isprintable()
    with pytest.raises(NotificationPreferencesError) as caught:
        normalize_notification_preferences({
            "enabled": True, "target": target, "classes": ["review_ready"],
        })
    assert str(caught.value) == "Notification target must be a notify entity"
