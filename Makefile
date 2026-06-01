# Makefile for Cardinal CloudFormation project.

.PHONY: help install build test test-unit test-templates test-jenkins check lint clean all

VENV_DIR := .venv
PYTHON   := $(VENV_DIR)/bin/python
PIP      := $(VENV_DIR)/bin/pip
PYTEST   := $(VENV_DIR)/bin/pytest

SHELL := /bin/bash

help:	## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:	## Install dependencies in virtual environment
	@if [ ! -d "$(VENV_DIR)" ]; then python3 -m venv $(VENV_DIR); fi
	$(PIP) install -r requirements.txt

build:	## Generate CloudFormation templates and run cfn-lint
	./build.sh

test:	## Run all tests
	$(PYTEST) tests/ -v

test-unit:	## Run unit tests only
	$(PYTEST) tests/unit/ -v

test-templates:	## Run per-template tests only
	$(PYTEST) tests/templates/ -v

test-jenkins:	## Run Jenkins/upgrade-script tests only
	$(PYTEST) tests/unit/test_upgrade_lakerunner.py tests/unit/test_upgrade_lakerunner_lint.py tests/unit/test_jenkinsfile_upgrade.py -v

check: test	## Pre-push gate (alias for test)

lint:	## Run cfn-lint on every generated template
	source $(VENV_DIR)/bin/activate && cfn-lint \
	  generated-templates/lrdev-vpc.yaml \
	  generated-templates/lrdev-baseinfra.yaml \
	  generated-templates/cardinal-infrastructure.yaml \
	  generated-templates/cardinal-cleanup.yaml \
	  generated-templates/cardinal-satellite-infra-base.yaml \
	  generated-templates/cardinal-satellite-services.yaml \
	  generated-templates/cardinal-lakerunner-infra-rds.yaml \
	  generated-templates/cardinal-lakerunner.yaml \
	  generated-templates/cardinal-lakerunner/*.yaml

clean:	## Clean generated files and test caches
	rm -rf generated-templates .pytest_cache tests/__pycache__ src/__pycache__ \
	       src/cardinal_cfn/__pycache__ src/cardinal_cfn/children/__pycache__

all: build test	## Run build + tests
