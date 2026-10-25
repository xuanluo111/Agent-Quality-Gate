PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
MATRIX ?= models.json
BATCH_ARGS ?=

.PHONY: help venv install dev test verify verify-live build-suite dry-run smoke eval batch batch-dry visualize clean

help:
	@echo "make install    创建 .venv 并安装运行依赖"
	@echo "make dev        额外安装 pytest"
	@echo "make test       运行离线单元测试"
	@echo "make verify     一键自检：单测+六项能力+数据集+dry-run（0 成本）"
	@echo "make verify-live 自检 + 真机冒烟 6 条，验证 pass^k（会产生费用）"
	@echo "make build-suite 重建 core/smoke 数据集与审计清单"
	@echo "make dry-run    只校验数据，不调用模型"
	@echo "make smoke      用 18 条冒烟集调用真实模型（会产生费用）"
	@echo "make eval       用 core 全量评测（会产生费用）"
	@echo "make batch-dry  校验多模型清单，不调用模型"
	@echo "make batch      按 $(MATRIX) 串行评测多个模型（会产生费用）"
	@echo "make visualize  根据 reports 生成 HTML 仪表盘和 PNG"

venv:
	python3 -m venv .venv

install: venv
	$(PIP) install -r requirements.txt

dev: install
	$(PIP) install -r requirements-dev.txt

test:
	$(PY) -m pytest -q

verify:
	$(PY) scripts/verify.py

verify-live:
	$(PY) scripts/verify.py --live --repeat 2

build-suite:
	$(PY) scripts/build_suite.py

dry-run:
	$(PY) scripts/run_eval.py --suite smoke --dry-run --limit 0

smoke:
	$(PY) scripts/run_eval.py --suite smoke --limit 18

eval:
	$(PY) scripts/run_eval.py --suite core --limit 0

batch-dry:
	$(PY) scripts/run_batch.py --matrix $(MATRIX) --dry-run

batch:
	$(PY) scripts/run_batch.py --matrix $(MATRIX) $(BATCH_ARGS)

visualize:
	$(PY) scripts/visualize_reports.py

clean:
	rm -rf .pytest_cache reports/runs reports/charts workspace
