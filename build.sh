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

echo "Generating cardinal-cleanup.yaml..."
python3 -m cardinal_cfn.cardinal_cleanup > generated-templates/cardinal-cleanup.yaml

echo "Generating cleanup-images.txt..."
python3 -m cardinal_cfn.image_manifest manifest cleanup > generated-templates/cleanup-images.txt

echo "Generating cardinal-satellite-infra-base.yaml..."
python3 -m cardinal_cfn.satellite_infra_base > generated-templates/cardinal-satellite-infra-base.yaml

echo "Generating cardinal-satellite-services.yaml..."
python3 -m cardinal_cfn.satellite_services > generated-templates/cardinal-satellite-services.yaml

echo "Generating satellite-images.txt..."
python3 -m cardinal_cfn.image_manifest manifest satellite > generated-templates/satellite-images.txt

echo "Generating cardinal-lakerunner-infra-rds.yaml..."
python3 -m cardinal_cfn.lakerunner_infra_rds > generated-templates/cardinal-lakerunner-infra-rds.yaml

echo "Generating cardinal-lakerunner-infra-base.yaml..."
python3 -m cardinal_cfn.lakerunner_infra_base > generated-templates/cardinal-lakerunner-infra-base.yaml

# ---------------------------------------------------------------------------
# Lakerunner application-tier stack (param-driven root + 8 nested children).
# All SGs and IAM roles arrive as parameters (driver-wired from the infra
# stacks); there is no Security child.
# ---------------------------------------------------------------------------
echo "Generating cardinal-lakerunner-services.yaml (param-driven root)..."
python3 -m cardinal_cfn.lakerunner_services > generated-templates/cardinal-lakerunner-services.yaml

echo "Generating lakerunner-images.txt..."
python3 -m cardinal_cfn.image_manifest manifest lakerunner > generated-templates/lakerunner-images.txt

for child in alb cert migration \
             services_query services_process services_control maestro; do
  out_name=$(echo "$child" | tr '_' '-')
  echo "Generating cardinal-lakerunner/${out_name}.yaml..."
  python3 -m "cardinal_cfn.children.${child}" > "generated-templates/cardinal-lakerunner/${out_name}.yaml"
done

echo
echo "Generating single-file deploy drivers..."
sh scripts-src/build.sh

echo
echo "Linting CFN templates..."
cfn-lint generated-templates/lrdev-vpc.yaml \
         generated-templates/lrdev-baseinfra.yaml \
         generated-templates/cardinal-cleanup.yaml \
         generated-templates/cardinal-satellite-infra-base.yaml \
         generated-templates/cardinal-satellite-services.yaml \
         generated-templates/cardinal-lakerunner-infra-rds.yaml \
         generated-templates/cardinal-lakerunner-infra-base.yaml \
         generated-templates/cardinal-lakerunner-services.yaml \
         generated-templates/cardinal-lakerunner/*.yaml || \
  echo "cfn-lint completed with warnings"

echo
echo "Generated artifacts:"
ls -la generated-templates/
echo "  cardinal-lakerunner/:"
ls generated-templates/cardinal-lakerunner/
