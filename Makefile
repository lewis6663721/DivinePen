.PHONY: help install dev fmt lint type test smoke check run worker migrate seed specimen

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## 安装底座 + 开发依赖
	uv sync --all-extras

fmt: ## 格式化
	uv run ruff format src tests apps
	uv run ruff check --fix src tests apps

lint: ## 静态检查
	uv run ruff check src tests apps
	uv run ruff format --check src tests apps

type: ## 类型检查
	uv run mypy

test: ## 单元测试（绝不发真实计费请求）
	uv run pytest -m "not billed"

smoke: ## 端到端冒烟：注册 → 表单 → dry-run → 真跑本地能力（零成本）
	PYTHONPATH=src .venv/bin/python scripts/smoke.py

check: lint type test ## 提交前全量检查

run: ## 启动后台服务
	uv run --project apps/server uvicorn divinepen_server.main:app --reload --port 8801

migrate: ## 生成/应用数据库迁移（aerich）
	cd apps/server && uv run aerich upgrade

seed: ## 把内置模型目录写入数据库（不覆盖已改过的参数）
	cd apps/server && uv run python -m divinepen_server.db.seed

specimen: ## 打开视觉方向样张
	open design/specimen.html
