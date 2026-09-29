"""命令行入口：``python -m divinepen_server.cli seed [--force]``。

不引第三方 CLI 框架 —— argparse 够用，少一个依赖少一处升级风险。
"""

from __future__ import annotations

import argparse
import sys

import anyio

from . import runtime as rt
from .db import Store
from .settings import get_settings
from .store import Repo, SecretBox, seed


async def _seed(force: bool) -> int:
    settings = get_settings()
    repo = Repo(Store(settings.db_path), SecretBox(settings.credential_key))
    outcome = await seed(repo, force=force)
    models = outcome["models"]
    print(
        f"供应商 +{outcome['providers']} · 模型 新增 {models['inserted']} "
        f"更新 {models['updated']} 跳过 {models['skipped']}"
    )
    if outcome.get("admin_created"):
        print("已创建默认管理员 admin / divinepen（上线前请改掉）")
    print(f"数据库：{settings.db_path}")
    return 0


async def _status() -> int:
    settings = get_settings()
    instance = rt.App(settings)
    overview = await instance.repo.overview()
    print(f"数据库        {settings.db_path}")
    print(f"凭据加密      {instance.box.backend}")
    print(f"要求登录      {settings.auth_required}")
    print(f"真实计费调用  {settings.allow_billed_calls}")
    print(f"连通性模型    {settings.connectivity_model}")
    print(f"能力          {', '.join(instance.registry.ids()) or '（空）'}")
    print(f"模型          {overview['models']}")
    await instance.aclose()
    return 0


async def _gaps() -> int:
    settings = get_settings()
    instance = rt.App(settings)
    rows = await instance.repo.models()
    shown = 0
    for row in rows:
        reasons = []
        if not row["enabled"]:
            reasons.append("已停用")
        if not row["verified"]:
            reasons.append("参数待核对")
        if row["transport"] != "http":
            reasons.append(f"传输 {row['transport']} 未接入")
        if reasons:
            shown += 1
            print(f"{row['id']:<52} {' / '.join(reasons)}")
    if not shown:
        print("所有模型均无缺口。")
    await instance.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="divinepen_server.cli", description="DivinePen 后台命令行"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    seed_parser = sub.add_parser("seed", help="把内置模型目录导入数据库")
    seed_parser.add_argument("--force", action="store_true", help="用目录覆盖已改过的参数")
    sub.add_parser("status", help="打印配置与库内概况")
    sub.add_parser("gaps", help="列出还不能放心调用的模型")

    args = parser.parse_args(argv)
    if args.command == "seed":
        return anyio.run(_seed, args.force)
    if args.command == "status":
        return anyio.run(_status)
    return anyio.run(_gaps)


if __name__ == "__main__":
    sys.exit(main())
