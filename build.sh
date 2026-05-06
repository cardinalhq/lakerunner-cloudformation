#!/bin/sh
# Generate all Cardinal CloudFormation templates.

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

echo "Generating cardinal-prereqs.sh..."
python3 -m cardinal_cfn.prereqs.render > generated-templates/cardinal-prereqs.sh
chmod +x generated-templates/cardinal-prereqs.sh

echo "Generating cardinal-data-setup.sh..."
python3 -m cardinal_cfn.data_setup.render > generated-templates/cardinal-data-setup.sh
chmod +x generated-templates/cardinal-data-setup.sh

echo "Generating cardinal-deployer-role.yaml..."
python3 -m cardinal_cfn.cardinal_deployer > generated-templates/cardinal-deployer-role.yaml

echo "Generating cardinal-lakerunner.yaml (root)..."
python3 -m cardinal_cfn.root > generated-templates/cardinal-lakerunner.yaml

for child in cluster database storage alb config cert migration \
             services_query services_process services_control otel maestro; do
  out_name=$(echo "$child" | tr '_' '-')
  echo "Generating cardinal-lakerunner/${out_name}.yaml..."
  python3 -m "cardinal_cfn.children.${child}" > "generated-templates/cardinal-lakerunner/${out_name}.yaml"
done

echo
echo "Linting..."
cfn-lint generated-templates/cardinal-vpc.yaml \
         generated-templates/cardinal-deployer-role.yaml \
         generated-templates/cardinal-lakerunner.yaml \
         generated-templates/cardinal-lakerunner/*.yaml || \
  echo "cfn-lint completed with warnings"

echo
echo "Generated templates:"
ls generated-templates/ generated-templates/cardinal-lakerunner/
