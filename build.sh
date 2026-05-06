#!/bin/sh
# Generate all Cardinal CloudFormation artifacts.

set -e

if [ -d ".venv" ]; then
  . .venv/bin/activate
else
  python3 -m venv .venv
  . .venv/bin/activate
  pip install -r requirements.txt
fi

if [ -d "generated-templates" ]; then
  rm -rf generated-templates
fi
mkdir -p generated-templates/cardinal-lakerunner

export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"

echo "Generating cardinal-vpc.yaml..."
python3 -m cardinal_cfn.cardinal_vpc > generated-templates/cardinal-vpc.yaml

echo "Generating cardinal-deployer-role.yaml..."
python3 -m cardinal_cfn.cardinal_deployer > generated-templates/cardinal-deployer-role.yaml

# ---------------------------------------------------------------------------
# Existing lakerunner stack (root + 12 children) -- unchanged in this PR.
# Phase 2 (separate PR) refactors children to take roles/SGs as parameters
# and removes the database/storage/config children (work moves to Lambda).
# ---------------------------------------------------------------------------
echo "Generating cardinal-lakerunner.yaml (root)..."
python3 -m cardinal_cfn.root > generated-templates/cardinal-lakerunner.yaml

for child in cluster database storage alb config cert migration \
             services_query services_process services_control otel maestro; do
  out_name=$(echo "$child" | tr '_' '-')
  echo "Generating cardinal-lakerunner/${out_name}.yaml..."
  python3 -m "cardinal_cfn.children.${child}" > "generated-templates/cardinal-lakerunner/${out_name}.yaml"
done

# ---------------------------------------------------------------------------
# New artifacts (this PR): data-setup Lambda + its CFN wrapper, plus the
# generated required-roles cookbook the customer's IT consumes.
# ---------------------------------------------------------------------------
echo "Generating cardinal-data-setup.yaml (Lambda wrapper)..."
python3 -m cardinal_cfn.data_setup_lambda.template > generated-templates/cardinal-data-setup.yaml

echo "Packaging cardinal-data-setup-lambda.zip..."
LAMBDA_BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$LAMBDA_BUILD_DIR"' EXIT
cp src/cardinal_cfn/data_setup_lambda/handler.py "$LAMBDA_BUILD_DIR/handler.py"
( cd "$LAMBDA_BUILD_DIR" && zip -q "$OLDPWD/generated-templates/cardinal-data-setup-lambda.zip" handler.py )

echo "Generating docs/operations/required-roles.md..."
python3 -m cardinal_cfn.required_roles_doc > docs/operations/required-roles.md

echo
echo "Linting CFN templates..."
cfn-lint generated-templates/cardinal-vpc.yaml \
         generated-templates/cardinal-deployer-role.yaml \
         generated-templates/cardinal-lakerunner.yaml \
         generated-templates/cardinal-lakerunner/*.yaml \
         generated-templates/cardinal-data-setup.yaml || \
  echo "cfn-lint completed with warnings"

echo
echo "Generated artifacts:"
ls -la generated-templates/
echo "  cardinal-lakerunner/:"
ls generated-templates/cardinal-lakerunner/
