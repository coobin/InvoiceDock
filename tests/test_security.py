from app.security import throttle_limit, throttle_reset


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
