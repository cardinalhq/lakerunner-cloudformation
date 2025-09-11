#!/bin/sh
# Copyright (C) 2025 CardinalHQ, Inc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

# Activate virtual environment
if [ -d ".venv" ]; then
  echo "Activating virtual environment..."
  . .venv/bin/activate
else
  echo "Virtual environment not found. Creating one..."
  python3 -m venv .venv
  . .venv/bin/activate
  echo "Installing dependencies..."
  pip install -r requirements.txt
fi

if [ -d "generated-templates" ]; then
  rm -rf generated-templates
fi
mkdir generated-templates

CFN_LINT="cfn-lint --ignore-checks W1020"

echo "1. Generating Lakerunner VPC..."
python3 src/lakerunner_vpc.py > generated-templates/lakerunner-vpc.yaml
$CFN_LINT generated-templates/lakerunner-vpc.yaml

echo "2. Generating Lakerunner ECS Infrastructure..."
python3 src/lakerunner_ecs.py > generated-templates/lakerunner-ecs.yaml
$CFN_LINT generated-templates/lakerunner-ecs.yaml

echo "3. Generating Lakerunner RDS..."
python3 src/lakerunner_rds.py > generated-templates/lakerunner-rds.yaml
$CFN_LINT generated-templates/lakerunner-rds.yaml

echo "4. Generating Lakerunner Storage..."
python3 src/lakerunner_storage.py > generated-templates/lakerunner-storage.yaml
$CFN_LINT generated-templates/lakerunner-storage.yaml

echo "5. Generating Lakerunner Migration Task..."
python3 src/lakerunner_migration.py > generated-templates/lakerunner-migration.yaml
$CFN_LINT generated-templates/lakerunner-migration.yaml

echo "6. Generating Lakerunner Services..."
python3 src/lakerunner_services.py > generated-templates/lakerunner-services.yaml
$CFN_LINT generated-templates/lakerunner-services.yaml

echo "7. Generating Lakerunner Grafana Service..."
python3 src/lakerunner_grafana_service.py > generated-templates/lakerunner-grafana-service.yaml
$CFN_LINT generated-templates/lakerunner-grafana-service.yaml

echo "8. Generating Demo OTEL Collector..."
python3 src/demo_otel_collector.py > generated-templates/lakerunner-demo-otel-collector.yaml
$CFN_LINT generated-templates/lakerunner-demo-otel-collector.yaml

echo "9. Generating Lakerunner Root Stack..."
python3 src/lakerunner_root.py > generated-templates/lakerunner-root.yaml
$CFN_LINT generated-templates/lakerunner-root.yaml

echo -e "\nGenerated CloudFormation templates:"
ls -la generated-templates/

echo -e "\nNote: cfn-lint warnings above are safe to ignore:"
echo "  - W1030: Empty PublicSubnets parameter is expected when using internal ALB"
