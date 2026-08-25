# Shortcuts. Everything here is just a docker compose command underneath.
.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup build data scan dry-run check-telegram alert up down logs bot bot-logs test shell clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## First-time setup: create .env, secrets/.env.dev and data dirs
	@./scripts/setup.sh

build: ## Build the custom alerts image (pulls PKScreener's image first)
	docker compose build alerts

data: ## Download the latest daily candles for every NSE stock
	docker compose run --rm data-refresh

scan: ## Run a PKScreener scanner (make scan OPTIONS=X:12:9)
	@./scripts/run-scan.sh $(OPTIONS)

dry-run: ## Run YOUR indicator and print the alert instead of sending it
	@./scripts/dry-run.sh $(SYMBOLS)

check-telegram: ## Verify the bot token/chat id by sending a test message
	docker compose run --rm --no-deps alerts --check-telegram

alert: ## Run YOUR indicator once and actually send the Telegram alert
	docker compose run --rm --no-deps alerts --once

up: ## Start the scheduled alert runner in the background
	docker compose up -d alerts

down: ## Stop everything
	docker compose --profile bot --profile manual down

logs: ## Follow the alert runner's logs
	docker compose logs -f alerts

bot: ## Start PKScreener's own Telegram bot server
	docker compose --profile bot up -d bot

bot-logs: ## Follow the bot server's logs
	docker compose --profile bot logs -f bot

test: ## Run the test suite for the custom code
	python3 -m pytest tests -q

shell: ## Open a shell inside the alerts image
	docker compose run --rm --no-deps --entrypoint bash alerts

clean: ## Remove containers and the downloaded candle cache
	docker compose --profile bot --profile manual down -v
	rm -rf data/results/*
