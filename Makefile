.PHONY: help build up down verify test clean

help:
	@echo "make build   - build the image"
	@echo "make up      - start api + worker (host port via API_PORT, default 8080)"
	@echo "make verify  - run the one-shot acceptance service"
	@echo "make test    - run pytest (set LOCAL_SOFFICE for real-converter tests)"
	@echo "make clean   - stop stack and remove the data volume"

build:
	docker compose build

up:
	docker compose up --build

down:
	docker compose down

verify:
	docker compose build
	docker compose --profile verify run --rm verify

test:
	.venv/bin/python -m pytest

clean:
	docker compose down -v
