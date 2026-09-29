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

run: ## 启动后台服务（控制台 + 任务中心）
	uv run --project apps/server uvicorn divinepen_server.web:app --reload --port 8801

serve: ## 前台启动后台服务（不起 reload，适合只看页面）
	.venv/bin/python -m uvicorn divinepen_server.web:app --host 127.0.0.1 --port 8801

migrate: ## 生成/应用数据库迁移（aerich）
	cd apps/server && uv run aerich upgrade

seed: ## 把内置模型目录写入数据库（不覆盖已改过的参数）
	.venv/bin/python -m divinepen_server.cli seed

gaps: ## 列出还不能放心调用的模型（待核对 / 未接入 / 已停用）
	.venv/bin/python -m divinepen_server.cli gaps

specimen: ## 打开视觉方向样张
	open design/specimen.html
