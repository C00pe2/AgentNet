"""AgentNet 端到端验收脚本。

起双 registry(主 9000 + reg2 9001,联邦同步)+ 3 个 demo agent,走通:
签发 key → 注册 → 联邦同步 → 召回 → LLM 精排 → SSE 调用 → 澄清 → 信誉 →
canary → 计费 → 巡检 → 本地兜底。
sql-optimizer 注册在 reg2 上,经联邦同步进入主 registry 召回,调用走链式代理。

用法:
    uv run python scripts/e2e.py                  # hash embedding(无需下载模型)
    uv run python scripts/e2e.py --real-embedding # 本地 bge-m3(首次下载 ~2.3GB)

需要 .env 提供 AGENTNET_LLM_BASE_URL / AGENTNET_LLM_API_KEY / AGENTNET_LLM_MODEL。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REGISTRY_URL = "http://localhost:9000"
REGISTRY2_URL = "http://localhost:9001"
ADMIN_KEY = "e2e-admin-key"
AGENTS = {
    "go-reviewer": 8001,
    "translator": 8002,
    "sql-optimizer": 8003,
}
LOCAL_AGENTS = {"go-reviewer", "translator"}  # 注册在主 registry
FEDERATED_AGENTS = {"sql-optimizer"}  # 注册在 reg2,经联邦同步进主 registry

# (query, 期望路由到的 agent_id;None 表示期望本地兜底)
ROUTING_CASES: list[tuple[str, str | None]] = [
    ("帮我看看这段 Go 代码有没有 goroutine 泄漏", "go-reviewer"),
    ("这个 channel 为什么会死锁?", "go-reviewer"),
    ("review 一下我的 Go 并发代码,有没有竞态条件", "go-reviewer"),
    ("用 pprof 怎么做 CPU 性能分析?", "go-reviewer"),
    ("帮我 review 一段 Go 的 HTTP handler", "go-reviewer"),
    ("帮我把这段中文翻译成英文", "translator"),
    ("这个技术文档帮我翻译一下,保留 markdown 格式", "translator"),
    ("Translate this paragraph into Chinese", "translator"),
    ("中译英:人工智能正在改变世界", "translator"),
    ("这条 SQL 查询很慢,帮我优化", "sql-optimizer"),
    ("这个索引该怎么建?", "sql-optimizer"),
    ("帮我分析一下这个执行计划", "sql-optimizer"),
    ("慢查询日志里这条 JOIN 能优化吗", "sql-optimizer"),
    ("Python 装饰器怎么用?", None),
    ("帮我写个快速排序", None),
    ("今天天气怎么样", None),
    ("1+1 等于几", None),
    ("推荐一部好电影", None),
    ("怎么学英语?", None),
    ("帮我重装电脑系统", None),
]


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


async def wait_http(url: str, timeout: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(url, timeout=2.0)
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"等待 {url} 超时")
            await asyncio.sleep(0.5)


async def wait_agent_status(agent_id: str, status: str, consumer_key: str, timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    headers = {"Authorization": f"Bearer {consumer_key}"}
    async with httpx.AsyncClient(base_url=REGISTRY_URL) as client:
        while True:
            resp = await client.get(f"/v1/agents/{agent_id}", headers=headers)
            current = resp.json()["data"]["status"]
            if current == status:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"{agent_id} 未在 {timeout}s 内变为 {status}(当前 {current})")
            await asyncio.sleep(0.5)


async def search_ids(query: str, consumer_key: str) -> list[str]:
    headers = {"Authorization": f"Bearer {consumer_key}"}
    async with httpx.AsyncClient(base_url=REGISTRY_URL) as client:
        resp = await client.get("/v1/agents/search", params={"q": query}, headers=headers)
        return [c["card"]["agent_id"] for c in resp.json()["data"]["candidates"]]


async def wait_canary_score(agent_id: str, consumer_key: str, timeout: float = 30.0) -> float:
    deadline = asyncio.get_running_loop().time() + timeout
    headers = {"Authorization": f"Bearer {consumer_key}"}
    async with httpx.AsyncClient(base_url=REGISTRY_URL) as client:
        while True:
            resp = await client.get(f"/v1/agents/{agent_id}", headers=headers)
            score = (resp.json()["data"].get("reputation") or {}).get("canary_score")
            if score is not None:
                return score
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"{agent_id} 的 canary 跑分未在 {timeout}s 内出现")
            await asyncio.sleep(1)


def start_registry(db: Path, port: int, real_embedding: bool, extra_env: dict | None = None) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENTNET_DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "AGENTNET_PORT": str(port),
        "AGENTNET_EMBEDDING_BACKEND": "sentence-transformers" if real_embedding else "hash",
        "AGENTNET_ADMIN_KEY": ADMIN_KEY,
        **(extra_env or {}),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "agentnet_registry"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def main() -> int:
    load_dotenv(ROOT / ".env")
    real_embedding = "--real-embedding" in sys.argv
    embedding = "sentence-transformers" if real_embedding else "hash"

    registry_db = ROOT / ".e2e_registry.db"
    registry2_db = ROOT / ".e2e_registry2.db"
    for db in (registry_db, registry2_db):
        if db.exists():
            db.unlink()

    import json as _json

    import yaml

    main_governance = {
        "AGENTNET_INSPECT_ENABLED": "true",
        "AGENTNET_INSPECT_INTERVAL_SEC": "1",
        "AGENTNET_INSPECT_FAIL_THRESHOLD": "2",
        "AGENTNET_CANARY_ENABLED": "true",
        "AGENTNET_CANARY_INTERVAL_SEC": "2",
        "AGENTNET_CANARY_TIMEOUT_SEC": "10",
    }
    reg2 = start_registry(
        registry2_db,
        9001,
        real_embedding,
        {"AGENTNET_INSPECT_ENABLED": "false", "AGENTNET_CANARY_ENABLED": "false"},
    )
    registry_proc: subprocess.Popen | None = None
    agent_procs: dict[str, subprocess.Popen] = {}
    failures: list[str] = []

    try:
        log(f"启动 reg2(联邦对端,embedding={embedding})...")
        await wait_http(f"{REGISTRY2_URL}/healthz", timeout=120.0 if real_embedding else 60.0)

        for agent_id in AGENTS:
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "examples" / "agents" / f"{agent_id.replace('-', '_')}.py")],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            agent_procs[agent_id] = proc
        for port in AGENTS.values():
            await wait_http(f"http://localhost:{port}/health")
        log("3 个 demo agent 已启动(8001~8003)")

        # reg2:签 key + 注册联邦侧 agent(sql-optimizer),并签发主 registry 的对等消费 key
        async with httpx.AsyncClient(base_url=REGISTRY2_URL, timeout=30.0) as client2:
            admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
            provider2_key = (
                await client2.post("/v1/keys", json={"role": "provider", "name": "e2e-carol"}, headers=admin)
            ).json()["data"]["key"]
            peering_key = (
                await client2.post("/v1/keys", json={"role": "consumer", "name": "main-registry"}, headers=admin)
            ).json()["data"]["key"]
            for agent_id in FEDERATED_AGENTS:
                card = yaml.safe_load((ROOT / "examples" / "cards" / f"{agent_id}.yaml").read_text("utf-8"))
                resp = await client2.post(
                    "/v1/agents", json=card, headers={"Authorization": f"Bearer {provider2_key}"}
                )
                assert resp.status_code == 200, resp.text
        log(f"reg2 就绪:{','.join(FEDERATED_AGENTS)} 已注册")

        # 主 registry:以消费者身份对等 reg2(联邦同步)
        peers = _json.dumps([{"url": REGISTRY2_URL, "consumer_key": peering_key, "name": "reg2"}])
        registry_proc = start_registry(
            registry_db,
            9000,
            real_embedding,
            {
                **main_governance,
                "AGENTNET_PEERS": peers,
                "AGENTNET_FEDERATION_SYNC_INTERVAL_SEC": "2",
            },
        )
        log("启动主 registry(联邦 peer=reg2)...")
        await wait_http(f"{REGISTRY_URL}/healthz", timeout=120.0 if real_embedding else 60.0)

        async with httpx.AsyncClient(base_url=REGISTRY_URL, timeout=30.0) as client:
            admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
            provider_key = (
                await client.post("/v1/keys", json={"role": "provider", "name": "e2e-alice"}, headers=admin)
            ).json()["data"]["key"]
            consumer_key = (
                await client.post("/v1/keys", json={"role": "consumer", "name": "e2e-bob"}, headers=admin)
            ).json()["data"]["key"]
            log("已签发 provider/consumer key")

            provider = {"Authorization": f"Bearer {provider_key}"}
            for agent_id in LOCAL_AGENTS:
                card = yaml.safe_load((ROOT / "examples" / "cards" / f"{agent_id}.yaml").read_text("utf-8"))
                resp = await client.post("/v1/agents", json=card, headers=provider)
                assert resp.status_code == 200, resp.text
            log("2 个本地 agent 已注册;sql-optimizer 等待联邦同步")

        # 联邦同步:reg2 的 sql-optimizer 进入主 registry 召回
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            ids = await search_ids("SQL 查询优化", consumer_key)
            if "sql-optimizer" in ids:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("联邦同步未在 30s 内同步 sql-optimizer")
            await asyncio.sleep(1)
        async with httpx.AsyncClient(base_url=REGISTRY_URL) as client:
            consumer = {"Authorization": f"Bearer {consumer_key}"}
            data = (await client.get("/v1/agents/sql-optimizer", headers=consumer)).json()["data"]
            assert data["provider"] == "federated:reg2", data["provider"]
        log("联邦同步完成:sql-optimizer 进入主 registry 召回(provider=federated:reg2)")

        # CLI 冒烟:list
        cli_env = {**os.environ, "AGENTNET_CONSUMER_KEY": consumer_key}
        cli = subprocess.run(
            [sys.executable, "-m", "agentnet_cli.main", "list"],
            env=cli_env, capture_output=True, text=True, timeout=30,
        )
        assert "go-reviewer" in cli.stdout, cli.stderr
        log("CLI `agentnet list` 正常")

        # 路由准确率
        from agentnet_router import LLMSettings, Router, RouterSettings

        llm = LLMSettings(
            base_url=os.environ["AGENTNET_LLM_BASE_URL"],
            api_key=os.environ["AGENTNET_LLM_API_KEY"],
            model=os.environ["AGENTNET_LLM_MODEL"],
        )
        router = Router(
            RouterSettings(
                registry_url=REGISTRY_URL,
                consumer_key=consumer_key,
                llm=llm,
                threshold=0.6,
            )
        )
        log(f"开始路由准确率测试({len(ROUTING_CASES)} 条)...")
        correct = 0
        for query, expected in ROUTING_CASES:
            decision = await router.route(query)
            got = decision.agent_id if decision.routed else None
            mark = "OK " if got == expected else "MISS"
            if got == expected:
                correct += 1
            else:
                failures.append(f"{mark} {query!r}: 期望 {expected},实际 {got}({decision.reason})")
            print(f"  [{mark}] {query}  ->  {got or '本地兜底'}", flush=True)
        accuracy = correct / len(ROUTING_CASES)
        log(f"路由准确率:{correct}/{len(ROUTING_CASES)} = {accuracy:.0%}")
        if accuracy < 0.8:
            failures.append(f"准确率 {accuracy:.0%} 低于 80%")

        # 完整 ask:路由成功 + SSE 流式 + artifact 产出
        log("测试完整 ask(SSE 流式 + artifact)...")
        deltas: list[str] = []
        result = await router.ask("帮我看看这段 Go 代码有没有并发问题", on_delta=deltas.append)
        assert result.routed and not result.fallback, result.reason
        assert result.agent_id == "go-reviewer" and deltas, result
        assert result.artifacts and result.artifacts[0].name == "fix.diff", result.artifacts
        diff_text = "".join(p.text for p in result.artifacts[0].parts if p.type == "text")
        assert "close(results)" in diff_text, diff_text
        log(
            f"ask 路由成功,{len(deltas)} 个流式增量,回答 {len(result.answer)} 字,"
            f"artifact fix.diff({len(diff_text)} 字 diff)"
        )

        # 完整 ask:无人能答 → 本地兜底
        result = await router.ask("宇宙的意义是什么?")
        assert result.fallback, "应当本地兜底"
        log(f"ask 本地兜底正常(回答 {len(result.answer)} 字)")

        # 完整 ask:input-required 多轮澄清(sql-optimizer 会追问表结构)
        log("测试 input-required 多轮澄清...")
        asked: list[str] = []

        async def on_input(question: str) -> str:
            asked.append(question)
            return "orders 表,已有 user_id 索引"

        result = await router.ask("这条 SQL 查询很慢,帮我优化", on_input_required=on_input)
        assert result.routed and not result.fallback, result.reason
        assert result.agent_id == "sql-optimizer" and asked, result
        assert "orders 表" in result.answer, result.answer
        async with httpx.AsyncClient(base_url=REGISTRY_URL) as client:
            resp = await client.get(
                f"/v1/agents/sql-optimizer/tasks/{result.task_id}",
                headers={"Authorization": f"Bearer {consumer_key}"},
            )
            roles = [m["role"] for m in resp.json()["data"]["messages"]]
            assert roles[0] == "user" and roles.count("user") == 2 and "agent" in roles, roles
        log(f"澄清链路正常(agent 追问 {len(asked)} 次,问答已落库:{roles})")

        # 无应答回调时:agent 要求澄清 → router 主动取消并本地兜底(不挂起)
        result = await router.ask("这条 JOIN 能优化吗")
        assert result.fallback and "应答回调" in result.reason, result
        log("无回调澄清场景:已主动取消并本地兜底")

        # 出站安全:含密钥的 query 永不外发(registry 零接触,直接本地回答)
        result = await router.ask("我的 API key 是 sk-a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6,帮我 review 代码")
        assert result.fallback and "密钥" in result.reason, result
        log("出站安全策略正常(含密钥 query 未外发)")

        # 反馈 → 信誉
        routed = await router.ask("review 一下这段 Go 代码")
        if routed.task_id:
            await router.feedback(routed.task_id, 5)
            async with httpx.AsyncClient(base_url=REGISTRY_URL) as client:
                resp = await client.get(
                    "/v1/agents/go-reviewer",
                    headers={"Authorization": f"Bearer {consumer_key}"},
                )
                rep = resp.json()["data"]["reputation"]
                log(f"go-reviewer 信誉:调用 {rep['calls']} 次,成功率 {rep['success_rate']:.0%},评分 {rep['rating']}")
                assert rep["calls"] >= 1 and rep["success_rate"] == 1.0

        # canary 跑分:go-reviewer 声明的用例应通过并计入信誉
        score = await wait_canary_score("go-reviewer", consumer_key)
        assert score == 1.0, score
        log(f"canary 跑分正常(go-reviewer 通过率 {score:.0%})")

        # 计费:translator 按次收费(1 积分/次);成功才扣费
        log("测试计费(translator 1 积分/次)...")
        async with httpx.AsyncClient(base_url=REGISTRY_URL) as client:
            admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
            consumer = {"Authorization": f"Bearer {consumer_key}"}
            resp = await client.post(
                "/v1/credits/topup", json={"name": "e2e-bob", "amount": 1.0}, headers=admin
            )
            assert resp.status_code == 200 and resp.json()["data"]["balance"] == 1.0, resp.text

        result = await router.ask("帮我把这段中文翻译成英文")
        assert result.routed and not result.fallback, result.reason
        async with httpx.AsyncClient(base_url=REGISTRY_URL) as client:
            consumer = {"Authorization": f"Bearer {consumer_key}"}
            data = (await client.get("/v1/credits/balance", headers=consumer)).json()["data"]
            assert data["balance"] == 0.0, data
        log("按次扣费正常(余额 1 → 0,流水含 charge 记录)")

        # 余额不足 → 402 → router 本地兜底
        result = await router.ask("再帮我翻译一句")
        assert result.fallback and "任务创建失败" in result.reason, result
        log("余额不足:402 拒绝,已本地兜底")

        # 巡检:translator 宕机 → offline 且退出召回;恢复 → active 且回到召回
        log("测试巡检(translator 宕机 → 恢复)...")
        agent_procs["translator"].terminate()
        agent_procs["translator"].wait(timeout=10)
        await wait_agent_status("translator", "offline", consumer_key)
        ids = await search_ids("帮我把这段中文翻译成英文", consumer_key)
        assert "translator" not in ids, ids
        log("translator 已 offline 并退出召回")

        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "examples" / "agents" / "translator.py")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        agent_procs["translator"] = proc
        await wait_http("http://localhost:8002/health")
        await wait_agent_status("translator", "active", consumer_key)
        ids = await search_ids("帮我把这段中文翻译成英文", consumer_key)
        assert "translator" in ids, ids
        log("translator 恢复 active 并回到召回")
        await router.aclose()

    finally:
        for proc in agent_procs.values():
            proc.terminate()
        if registry_proc is not None:
            registry_proc.terminate()
        reg2.terminate()

    if failures:
        log("失败项:")
        for f in failures:
            print(f"  - {f}")
        return 1
    log("E2E 全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
