"""治理原语单元测试(无需 DB)。

包含旧版 v0 gateway 熔断器全局状态 bug 的回归测试。
"""

import time

from agentnet_registry.governance import CircuitBreaker, RateLimiter


def test_rate_limiter_per_agent_isolation():
    limiter = RateLimiter(rps=2)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False  # a 被限流
    assert limiter.allow("b") is True  # b 不受影响


def test_rate_limiter_window_slides():
    limiter = RateLimiter(rps=1)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    limiter._hits["a"][0] -= 1.1  # 模拟时间流逝
    assert limiter.allow("a") is True


def test_breaker_per_agent_isolation():
    """回归:旧版熔断状态是全局单值,a 熔断会挡掉所有 agent。"""
    breaker = CircuitBreaker(fail_threshold=3, open_sec=60, window_sec=10)
    for _ in range(3):
        breaker.on_failure("a")
    assert breaker.pre_check("a") is False  # a 熔断
    assert breaker.pre_check("b") is True  # b 必须不受影响


def test_breaker_half_open_recovery():
    breaker = CircuitBreaker(fail_threshold=2, open_sec=0.05, window_sec=10)
    breaker.on_failure("a")
    breaker.on_failure("a")
    assert breaker.pre_check("a") is False
    time.sleep(0.15)  # 留足余量,避免 Windows 定时器精度导致的抖动
    assert breaker.pre_check("a") is True  # 半开放行
    breaker.on_success("a")
    assert breaker.pre_check("a") is True


def test_breaker_success_resets_failures():
    breaker = CircuitBreaker(fail_threshold=3, open_sec=60, window_sec=10)
    breaker.on_failure("a")
    breaker.on_failure("a")
    breaker.on_success("a")
    breaker.on_failure("a")
    breaker.on_failure("a")
    assert breaker.pre_check("a") is True  # 未达连续阈值
