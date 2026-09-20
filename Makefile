# Aureon developer tasks.
#
# IMPORTANT for the emulator: gRPC honours its own proxy variables, so a session with
# HTTP(S)_PROXY set will try to reach 127.0.0.1 through the proxy and fail with
# "Expected SETTINGS frame as the first frame". `no_grpc_proxy` is the targeted
# bypass -- it does not disable the proxy for anything else.
EMULATOR_PORT ?= 8080
EMULATOR_HOST ?= 127.0.0.1:$(EMULATOR_PORT)
EMULATOR_ENV  = FIRESTORE_EMULATOR_HOST=$(EMULATOR_HOST) \
                GOOGLE_CLOUD_PROJECT=aureon-test \
                AUREON_COLLECTION_PREFIX=aureon_test \
                no_grpc_proxy=127.0.0.1,localhost

.PHONY: help emulator emulator-stop test test-fast test-emulator contracts baseline lint check

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

emulator: ## start the Firestore emulator (no Docker needed)
	@tools/run_emulator.sh $(EMULATOR_PORT)

emulator-stop: ## stop it
	@kill "$$(cat /tmp/aureon-emulator/emulator.pid 2>/dev/null)" 2>/dev/null || true
	@echo "emulator stopped"

test-fast: ## everything that needs no emulator
	pytest -q --ignore=tests/failure_injection

test-emulator: ## the failure-injection suite, against a running emulator
	$(EMULATOR_ENV) pytest -q tests/failure_injection

test: ## the whole suite
	$(EMULATOR_ENV) pytest -q

contracts: ## regenerate docs/CONTRACTS.md
	python scripts/gen_contracts.py

baseline: ## regenerate docs/PHASE2_BASELINE.md
	python scripts/gen_baseline.py

lint:
	ruff check aureon tests scripts main_observer.py main_executor.py

check: lint ## the cross-phase checklist
	python scripts/gen_contracts.py --check
	python scripts/gen_baseline.py --check
	$(MAKE) test
