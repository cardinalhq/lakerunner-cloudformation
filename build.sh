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

echo "Generating lrdev-vpc.yaml..."
python3 -m cardinal_cfn.lrdev_vpc > generated-templates/lrdev-vpc.yaml

echo "Generating cardinal-infrastructure.yaml..."
python3 -m cardinal_cfn.cardinal_infrastructure > generated-templates/cardinal-infrastructure.yaml

echo "Generating cardinal-cleanup.yaml..."
python3 -m cardinal_cfn.cardinal_cleanup > generated-templates/cardinal-cleanup.yaml

# ---------------------------------------------------------------------------
# Lakerunner stack (root + 9 nested children). The Security child owns all
# SGs and IAM roles; other children take SG IDs and role ARNs from it
# rather than from customer-supplied parameters.
# ---------------------------------------------------------------------------
echo "Generating cardinal-lakerunner.yaml (root)..."
python3 -m cardinal_cfn.root > generated-templates/cardinal-lakerunner.yaml

for child in security alb cert migration \
             services_query services_process services_control otel maestro; do
  out_name=$(echo "$child" | tr '_' '-')
  echo "Generating cardinal-lakerunner/${out_name}.yaml..."
  python3 -m "cardinal_cfn.children.${child}" > "generated-templates/cardinal-lakerunner/${out_name}.yaml"
done

echo
echo "Linting CFN templates..."
cfn-lint generated-templates/lrdev-vpc.yaml \
         generated-templates/cardinal-infrastructure.yaml \
         generated-templates/cardinal-cleanup.yaml \
         generated-templates/cardinal-lakerunner.yaml \
         generated-templates/cardinal-lakerunner/*.yaml || \
  echo "cfn-lint completed with warnings"

echo
echo "Generated artifacts:"
ls -la generated-templates/
echo "  cardinal-lakerunner/:"
ls generated-templates/cardinal-lakerunner/
