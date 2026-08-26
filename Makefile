# Shortcuts. Everything here is just a docker compose command underneath.
.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup build data scan dry-run check-telegram alert up down logs bot bot-logs test shell clean \
	oi-build oi-dry-run oi-alert oi-up oi-logs oi-backtest oi-warm-cache

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

# ---- F&O open-interest blast scanner ---------------------------------------

oi-build: ## Build the OI scanner image (same image as `alerts`)
	docker compose build oi-scanner

oi-dry-run: ## Run the OI scan and print the alert instead of sending it
	docker compose run --rm oi-scanner --once --dry-run $(if $(DATE),--date $(DATE),)

oi-alert: ## Run the OI scan once and actually send the Telegram alert
	docker compose run --rm oi-scanner --once $(if $(DATE),--date $(DATE),)

oi-up: ## Start the scheduled OI scanner in the background
	docker compose up -d oi-scanner

oi-logs: ## Follow the OI scanner's logs
	docker compose logs -f oi-scanner

oi-warm-cache: ## Pre-download F&O bhavcopy history (make oi-warm-cache START=2024-07-01)
	docker compose run --rm oi-scanner --warm-cache --start $(or $(START),2025-01-01) $(if $(END),--end $(END),)

oi-backtest: ## Backtest the OI signal (make oi-backtest START=2024-07-01 END=2026-08-25)
	docker compose run --rm oi-scanner --backtest --split \
		--start $(or $(START),2025-01-01) $(if $(END),--end $(END),) $(if $(CSV),--csv $(CSV),)

test: ## Run all tests. Sends a real Telegram message too if secrets/.env.dev is set up
	python3 -m pytest tests -q -rs

shell: ## Open a shell inside the alerts image
	docker compose run --rm --no-deps --entrypoint bash alerts

clean: ## Remove containers and the downloaded candle cache
	docker compose --profile bot --profile manual down -v
	rm -rf data/results/*
