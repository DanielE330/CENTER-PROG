.PHONY: up down logs demo-data seed test smoke ml-backtest
ROOT := $(abspath $(CURDIR)/..)
up:
	docker compose up --build -d
down:
	docker compose down
logs:
	docker compose logs -f
demo-data:
	mkdir -p $(ROOT)/data && python3 scripts/gen_statement.py > $(ROOT)/data/demo_statement.csv
test:
	docker build -f docker/python.Dockerfile --target test -t ci1-pytest $(ROOT) && docker run --rm ci1-pytest
	docker run --rm -v $(ROOT):/src -w /src/backend -e CARGO_TARGET_DIR=/tmp/target rust:1-bookworm cargo test
smoke:
	./scripts/smoke.sh
seed:
	$(ROOT)/infra/scripts/seed_demo.sh

# ML-лаборатория: единый бэктест на датасете (нужен MOCK_DATASET_DIR), см. docs/ml-architecture.md
ml-backtest:
	cd $(ROOT) && python3 -m ml.backtest.run --companies 30 --cutoffs 8
