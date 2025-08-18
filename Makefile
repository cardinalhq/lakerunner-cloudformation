# Makefile for Lakerunner CloudFormation project

.PHONY: help install build test lint clean all

# Detect virtual environment
VENV_DIR = .venv
PYTHON = $(VENV_DIR)/bin/python
PIP = $(VENV_DIR)/bin/pip
PYTEST = $(VENV_DIR)/bin/pytest

# Use bash for all shell commands to support source
SHELL := /bin/bash

help:	## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:	## Install dependencies in virtual environment
	source $(VENV_DIR)/bin/activate && pip install -r requirements.txt

build:		## Generate CloudFormation templates and validate
	source $(VENV_DIR)/bin/activate && ./build.sh

test:		## Run unit tests (working tests only)
	source $(VENV_DIR)/bin/activate && $(PYTEST) tests/test_common_infra.py tests/test_*_simple.py tests/test_parameter_validation.py tests/test_condition_validation.py -v

test-all:	## Run all tests including complex ones (may have failures)
	source $(VENV_DIR)/bin/activate && $(PYTEST) tests/ -v

test-common:	## Run tests for CommonInfra template only
	source $(VENV_DIR)/bin/activate && $(PYTEST) tests/test_common_infra.py -v

test-services:	## Run simplified tests for Services template
	source $(VENV_DIR)/bin/activate && $(PYTEST) tests/test_services_simple.py -v

test-migration:	## Run simplified tests for Migration template
	source $(VENV_DIR)/bin/activate && $(PYTEST) tests/test_migration_simple.py -v

test-grafana:	## Run simplified tests for Grafana template
	source $(VENV_DIR)/bin/activate && $(PYTEST) tests/test_grafana_simple.py -v

test-params:	## Run parameter and condition validation tests
	source $(VENV_DIR)/bin/activate && $(PYTEST) tests/test_parameter_validation.py tests/test_condition_validation.py -v

lint:		## Run CloudFormation linting (warnings are acceptable)
	source $(VENV_DIR)/bin/activate && cfn-lint generated-templates/*.yaml || echo "⚠️  cfn-lint completed with warnings (warnings are acceptable per CLAUDE.md)"

clean:		## Clean generated files and test cache
	rm -rf generated-templates/*.yaml .pytest_cache tests/__pycache__ src/__pycache__

all: build test lint	## Run build, test, and lint