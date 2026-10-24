.PHONY: up down logs demo-data test
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
	docker run --rm -v $(ROOT):/src -w /src/backend/api -e CARGO_TARGET_DIR=/tmp/target rust:1-bookworm cargo test
