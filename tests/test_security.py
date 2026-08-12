from types import SimpleNamespace

from app import security
from app.security import is_reserved_username, throttle_limit, throttle_reset


def test_throttle_allows_up_to_limit_then_blocks():
    key = "test:login-ip"
    throttle_reset(key)
    for _ in range(3):
        assert throttle_limit(key, 3, 60) is False
    assert throttle_limit(key, 3, 60) is True


def test_throttle_reset_clears_bucket():
    key = "test:register-ip"
    throttle_reset(key)
    for _ in range(3):
        throttle_limit(key, 3, 60)
    assert throttle_limit(key, 3, 60) is True
    throttle_reset(key)
    assert throttle_limit(key, 3, 60) is False


def test_throttle_keys_are_independent():
    throttle_reset("test:a")
    throttle_reset("test:b")
    throttle_limit("test:a", 1, 60)
    assert throttle_limit("test:a", 1, 60) is True
    assert throttle_limit("test:b", 1, 60) is False


def test_reserved_username_blocks_email_local_part_and_aliases(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(admin_username="kay", additional_reserved_usernames=set()),
    )
    assert is_reserved_username("admin@example.com") is True
    assert is_reserved_username("Admin+test@example.com") is True
    assert is_reserved_username("a.d_m-i-n@example.com") is True
    assert is_reserved_username("kay@example.com") is True
    assert is_reserved_username("normal-user@example.com") is False


def test_reserved_username_blocks_sensitive_display_names(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(admin_username="kay", additional_reserved_usernames=set()),
    )
    assert is_reserved_username("系统 管理员") is True
    assert is_reserved_username("官方") is True
    assert is_reserved_username("普通用户") is False


def test_environment_can_add_reserved_usernames(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(
            admin_username="kay",
            additional_reserved_usernames={"finance", "财务"},
        ),
    )
    assert is_reserved_username("finance@example.com") is True
    assert is_reserved_username("财务") is True
