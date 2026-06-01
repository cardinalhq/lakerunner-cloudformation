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

echo "Generating lrdev-baseinfra.yaml..."
python3 -m cardinal_cfn.lrdev_baseinfra > generated-templates/lrdev-baseinfra.yaml

echo "Generating cardinal-infrastructure.yaml..."
python3 -m cardinal_cfn.cardinal_infrastructure > generated-templates/cardinal-infrastructure.yaml

echo "Generating cardinal-cleanup.yaml..."
python3 -m cardinal_cfn.cardinal_cleanup > generated-templates/cardinal-cleanup.yaml

echo "Generating cardinal-satellite-infra-base.yaml..."
python3 -m cardinal_cfn.satellite_infra_base > generated-templates/cardinal-satellite-infra-base.yaml

echo "Generating cardinal-satellite-services.yaml..."
python3 -m cardinal_cfn.satellite_services > generated-templates/cardinal-satellite-services.yaml

echo "Generating cardinal-lakerunner-infra-rds.yaml..."
python3 -m cardinal_cfn.lakerunner_infra_rds > generated-templates/cardinal-lakerunner-infra-rds.yaml

echo "Generating cardinal-lakerunner-infra-base.yaml..."
python3 -m cardinal_cfn.lakerunner_infra_base > generated-templates/cardinal-lakerunner-infra-base.yaml

# ---------------------------------------------------------------------------
# Lakerunner stack (root + 9 nested children). The Security child owns all
# SGs and IAM roles; other children take SG IDs and role ARNs from it
# rather than from customer-supplied parameters.
# ---------------------------------------------------------------------------
echo "Generating cardinal-lakerunner.yaml (root)..."
python3 -m cardinal_cfn.root > generated-templates/cardinal-lakerunner.yaml

echo "Generating cardinal-lakerunner-services.yaml (param-driven root)..."
python3 -m cardinal_cfn.lakerunner_services > generated-templates/cardinal-lakerunner-services.yaml

for child in security alb cert migration \
             services_query services_process services_control otel maestro; do
  out_name=$(echo "$child" | tr '_' '-')
  echo "Generating cardinal-lakerunner/${out_name}.yaml..."
  python3 -m "cardinal_cfn.children.${child}" > "generated-templates/cardinal-lakerunner/${out_name}.yaml"
done

echo
echo "Linting CFN templates..."
cfn-lint generated-templates/lrdev-vpc.yaml \
         generated-templates/lrdev-baseinfra.yaml \
         generated-templates/cardinal-infrastructure.yaml \
         generated-templates/cardinal-cleanup.yaml \
         generated-templates/cardinal-satellite-infra-base.yaml \
         generated-templates/cardinal-satellite-services.yaml \
         generated-templates/cardinal-lakerunner-infra-rds.yaml \
         generated-templates/cardinal-lakerunner-infra-base.yaml \
         generated-templates/cardinal-lakerunner.yaml \
         generated-templates/cardinal-lakerunner-services.yaml \
         generated-templates/cardinal-lakerunner/*.yaml || \
  echo "cfn-lint completed with warnings"

echo
echo "Generated artifacts:"
ls -la generated-templates/
echo "  cardinal-lakerunner/:"
ls generated-templates/cardinal-lakerunner/
