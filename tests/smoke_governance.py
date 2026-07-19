"""Smoke test for rate limiter & circuit breaker."""
from app.core.codes import ErrorCode, GatewayError
from app.core.ratelimit import AgentRateLimiter
from app.core.circuit_breaker import CircuitBreaker, on_failure, on_success


def test_rate_limiter():
    rl = AgentRateLimiter(rps=3)
    for i in range(3):
        rl.acquire("agent.test")  # should not raise
    try:
        rl.acquire("agent.test")
        print("FAIL: rate limit did not trigger")
    except GatewayError as e:
        assert e.code == ErrorCode.ROUTE_LIMIT_EXCEEDED
        print(f"OK rate-limit triggered after 3 requests: {e.message}")


def test_circuit_breaker_open_close():
    cb = CircuitBreaker(window_sec=10, fail_threshold=3, open_sec=2)

    # 3 连续失败 -> OPEN
    for _ in range(3):
        cb.record_failure("agent.cb1")
    try:
        cb.pre_check("agent.cb1")
        print("FAIL: breaker should be open")
    except GatewayError as e:
        assert e.code == ErrorCode.AGENT_5XX_ERROR
        print(f"OK breaker open after 3 failures: {e.message}")

    # 成功一次后 -> CLOSED
    cb.record_success("agent.cb1")
    cb.pre_check("agent.cb1")
    print("OK breaker closed after success")


def test_circuit_breaker_global_singleton():
    on_failure("agent.singleton")
    on_failure("agent.singleton")
    on_success("agent.singleton")
    from app.core.circuit_breaker import get_circuit_breaker
    print("singleton state:", get_circuit_breaker().snapshot())


if __name__ == "__main__":
    test_rate_limiter()
    test_circuit_breaker_open_close()
    test_circuit_breaker_global_singleton()
    print("\nall smoke tests PASSED")
