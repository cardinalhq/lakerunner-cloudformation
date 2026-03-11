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

echo "00. Generating Lakerunner VPC..."
python3 src/lakerunner_vpc.py > generated-templates/lakerunner-00-vpc.yaml
cfn-lint generated-templates/lakerunner-00-vpc.yaml

echo "01. Generating Lakerunner Common Infrastructure..."
python3 src/lakerunner_common.py > generated-templates/lakerunner-01-common.yaml
cfn-lint generated-templates/lakerunner-01-common.yaml

echo "02. Generating Lakerunner Migration Task..."
python3 src/lakerunner_migration.py > generated-templates/lakerunner-02-migration.yaml
cfn-lint generated-templates/lakerunner-02-migration.yaml

echo "03. Generating Lakerunner Services..."
python3 src/lakerunner_services.py > generated-templates/lakerunner-03-services.yaml
cfn-lint generated-templates/lakerunner-03-services.yaml

echo "04. Generating Lakerunner Alerting..."
python3 src/lakerunner_alerting.py > generated-templates/lakerunner-04-alerting.yaml
cfn-lint generated-templates/lakerunner-04-alerting.yaml

echo "05. Generating Lakerunner Grafana Service..."
python3 src/lakerunner_grafana_service.py > generated-templates/lakerunner-05-grafana-service.yaml
cfn-lint generated-templates/lakerunner-05-grafana-service.yaml

echo "06. Generating Lakerunner OTEL Collector Service..."
python3 src/lakerunner_otel_collector_service.py > generated-templates/lakerunner-06-otel-collector-service.yaml
cfn-lint generated-templates/lakerunner-06-otel-collector-service.yaml

echo "98. Generating Lakerunner Bedrock Setup..."
python3 src/lakerunner_bedrock_setup.py > generated-templates/lakerunner-98-bedrock-setup.yaml
cfn-lint generated-templates/lakerunner-98-bedrock-setup.yaml

echo "99. Generating Lakerunner Debug Utility..."
python3 src/lakerunner_debug_utility.py > generated-templates/lakerunner-99-debug-utility.yaml
cfn-lint generated-templates/lakerunner-99-debug-utility.yaml

echo -e "\nGenerated CloudFormation templates:"
ls -la generated-templates/

echo -e "\nNote: cfn-lint warnings above are safe to ignore:"
echo "  - W1030: Empty PublicSubnets parameter is expected when using internal ALB"
echo "  - W1020: Unnecessary Fn::Sub warnings are cosmetic and don't affect functionality"
