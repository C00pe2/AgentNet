"""AgentNet 命令行:registry / create-key / register / list / search / ask / serve。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx
import typer
import yaml
from agentnet_core import AgentCard
from agentnet_router import Router, RouterSettings
from agentnet_sdk import AgentServer
from dotenv import load_dotenv
from rich.console import Console

app = typer.Typer(help="AgentNet CLI", no_args_is_help=True)
console = Console()
err = Console(stderr=True)


@app.callback()
def _main() -> None:
    load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get("AGENTNET_" + key, default)


def _registry_url() -> str:
    return _env("REGISTRY_URL", "http://localhost:9000").rstrip("/")


def _check(resp: httpx.Response) -> httpx.Response:
    if resp.status_code >= 400:
        try:
            message = resp.json().get("message", resp.text[:300])
        except Exception:  # noqa: BLE001
            message = resp.text[:300]
        err.print(f"[red]错误 {resp.status_code}:[/red] {message}")
        raise typer.Exit(1)
    return resp


# ---------------------------------------------------------------------------
# registry 服务
# ---------------------------------------------------------------------------


@app.command()
def registry(host: str | None = typer.Option(None), port: int | None = typer.Option(None)) -> None:
    """启动 Registry 服务(等价 python -m agentnet_registry)。"""
    import uvicorn
    from agentnet_registry.app import create_app
    from agentnet_registry.config import Settings

    settings = Settings()
    uvicorn.run(create_app(settings), host=host or settings.host, port=port or settings.port)


# ---------------------------------------------------------------------------
# 控制面
# ---------------------------------------------------------------------------


@app.command("create-key")
def create_key(
    role: str = typer.Option(..., help="provider | consumer"),
    name: str = typer.Option(..., help="key 的属主名(同一名字即同一身份)"),
) -> None:
    """用 admin key 签发 provider/consumer API key。"""

    async def _run() -> None:
        async with httpx.AsyncClient(base_url=_registry_url()) as client:
            resp = _check(
                await client.post(
                    "/v1/keys",
                    json={"role": role, "name": name},
                    headers={"Authorization": f"Bearer {_env('ADMIN_KEY')}"},
                )
            )
            data = resp.json()["data"]
            console.print("[green]已签发(明文仅此一次显示,请妥善保存):[/green]")
            console.print(data["key"])

    asyncio.run(_run())


def load_card(path: Path) -> AgentCard:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise typer.BadParameter(f"YAML 解析失败:{exc}") from exc
    if not isinstance(data, dict):
        raise typer.BadParameter("card 文件必须是 YAML mapping")
    try:
        return AgentCard(**data)
    except Exception as exc:  # noqa: BLE001
        raise typer.BadParameter(f"card 校验失败:{exc}") from exc


@app.command()
def register(card_path: Path = typer.Argument(..., exists=True, help="Agent Card YAML 文件")) -> None:
    """注册(或更新)一个 agent 到网络。需要 AGENTNET_PROVIDER_KEY。"""
    card = load_card(card_path)

    async def _run() -> None:
        async with httpx.AsyncClient(base_url=_registry_url()) as client:
            resp = _check(
                await client.post(
                    "/v1/agents",
                    json=json.loads(card.model_dump_json()),
                    headers={"Authorization": f"Bearer {_env('PROVIDER_KEY')}"},
                )
            )
            console.print(f"[green]已注册:[/green] {resp.json()['data']['agent_id']}")

    asyncio.run(_run())


@app.command("list")
def list_agents() -> None:
    """列出网络中的 agent(含信誉)。"""

    async def _run() -> None:
        async with httpx.AsyncClient(base_url=_registry_url()) as client:
            resp = _check(
                await client.get(
                    "/v1/agents", headers={"Authorization": f"Bearer {_env('CONSUMER_KEY')}"}
                )
            )
            for a in resp.json()["data"]:
                rep = a.get("reputation") or {}
                rep_text = (
                    f"调用 {rep.get('calls', 0)} 次,成功率 {rep.get('success_rate', 0):.0%}"
                    if rep
                    else "无调用记录"
                )
                console.print(
                    f"[bold]{a['agent_id']}[/bold] ({a['name']}) [{a.get('status', '?')}]\n"
                    f"  {a.get('description', '')}\n"
                    f"  信誉:{rep_text}"
                )

    asyncio.run(_run())


@app.command()
def search(query: str = typer.Argument(...), top_k: int = typer.Option(5)) -> None:
    """试召回:看网络认为哪些 agent 能解决这个问题。"""

    async def _run() -> None:
        async with httpx.AsyncClient(base_url=_registry_url()) as client:
            resp = _check(
                await client.get(
                    "/v1/agents/search",
                    params={"q": query, "top_k": top_k},
                    headers={"Authorization": f"Bearer {_env('CONSUMER_KEY')}"},
                )
            )
            for item in resp.json()["data"]["candidates"]:
                card = item["card"]
                console.print(f"{item['score']:.3f}  [bold]{card['agent_id']}[/bold] ({card['name']})")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 消费
# ---------------------------------------------------------------------------


@app.command()
def ask(
    query: str = typer.Argument(...),
    rate: float | None = typer.Option(None, help="给本次回答打分 0~5(写入该 agent 信誉)"),
) -> None:
    """提问:自动路由到网络中最合适的 agent,失败时本地 LLM 兜底。"""
    settings = RouterSettings.from_env()
    if not settings.consumer_key:
        err.print("[red]缺少 AGENTNET_CONSUMER_KEY[/red]")
        raise typer.Exit(1)
    if not settings.llm.api_key:
        err.print("[red]缺少 AGENTNET_LLM_API_KEY[/red]")
        raise typer.Exit(1)

    async def _run() -> None:
        router = Router(settings)
        streamed = False

        def on_delta(text: str) -> None:
            nonlocal streamed
            streamed = True
            console.print(text, end="", highlight=False)

        async def on_input_required(question: str) -> str:
            console.print(f"\n[yellow]agent 追问:[/yellow] {question}")
            return await asyncio.to_thread(typer.prompt, "你的回答")

        result = await router.ask(query, on_delta=on_delta, on_input_required=on_input_required)

        if not streamed:
            console.print(result.answer, highlight=False)
        else:
            console.print()
        for artifact in result.artifacts:
            console.print(f"[dim]工件:{artifact.name}[/dim]")
        if result.routed and not result.fallback:
            err.print(f"[dim]—— 由网络中的 {result.agent_id} 回答({result.reason})[/dim]")
        elif result.routed:
            err.print(f"[dim]—— {result.agent_id} 调用失败,已本地兜底[/dim]")
        else:
            err.print(f"[dim]—— 本地回答({result.reason})[/dim]")

        if rate is not None and result.task_id:
            await router.feedback(result.task_id, rate)
            err.print(f"[dim]已评分 {rate}[/dim]")
        await router.aclose()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 提供:跑一个 agent
# ---------------------------------------------------------------------------


def load_server(path: Path) -> AgentServer:
    """加载 agent 文件,要求其中定义名为 server 的 AgentServer 实例。"""
    spec = importlib.util.spec_from_file_location("agentnet_user_agent", path)
    if spec is None or spec.loader is None:
        raise typer.BadParameter(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.resolve().parent))
    spec.loader.exec_module(module)
    server = getattr(module, "server", None)
    if not isinstance(server, AgentServer):
        raise typer.BadParameter("agent 文件必须定义名为 server 的 AgentServer 实例")
    return server


@app.command()
def serve(
    path: Path = typer.Argument(..., exists=True, help="定义了 AgentServer 的 python 文件"),
    host: str = typer.Option("0.0.0.0"),
    port: int = typer.Option(8001),
) -> None:
    """运行一个 agent 服务(提供方)。"""
    server = load_server(path)
    console.print(f"[green]启动 agent:[/green] {server.card.agent_id} @ {host}:{port}")
    server.run(host=host, port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
