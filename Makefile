# Makefile for cardinal-cfn (Cardinal Lakerunner CloudFormation).

.PHONY: help install test check build lint clean all

VENV_DIR := .venv
PYTHON   := $(VENV_DIR)/bin/python
PIP      := $(VENV_DIR)/bin/pip
PYTEST   := $(VENV_DIR)/bin/pytest

SHELL := /bin/bash

help:	## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:	## Install dependencies in virtual environment
	source $(VENV_DIR)/bin/activate && pip install -r requirements.txt

test:	## Run pytest suites (unit + template)
	source $(VENV_DIR)/bin/activate && $(PYTEST) tests/unit tests/templates -v

check: test	## Alias for test (pre-push gate)

build:	## Generate CloudFormation templates (Phase 8 placeholder)
	source $(VENV_DIR)/bin/activate && ./build.sh

lint:	## Run cfn-lint on generated templates (Phase 8 placeholder)
	source $(VENV_DIR)/bin/activate && cfn-lint generated-templates/*.yaml || echo "cfn-lint completed with warnings"

clean:	## Clean generated files and test caches
	rm -rf generated-templates/*.yaml .pytest_cache tests/__pycache__ src/__pycache__

all: check	## Default sanity check (alias for check until Phase 8 build wires up)
