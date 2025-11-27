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

echo "1. Generating Lakerunner VPC..."
python3 src/lakerunner_vpc.py > generated-templates/lakerunner-vpc.yaml
cfn-lint generated-templates/lakerunner-vpc.yaml

echo "2. Generating Lakerunner Common Infrastructure..."
python3 src/lakerunner_common.py > generated-templates/lakerunner-common.yaml
cfn-lint generated-templates/lakerunner-common.yaml

echo "3. Generating Lakerunner Migration Task..."
python3 src/lakerunner_migration.py > generated-templates/lakerunner-migration.yaml
cfn-lint generated-templates/lakerunner-migration.yaml

echo "4. Generating Lakerunner Utility Task..."
python3 src/lakerunner_utility.py > generated-templates/lakerunner-utility.yaml
cfn-lint generated-templates/lakerunner-utility.yaml

echo "5. Generating Lakerunner Services..."
python3 src/lakerunner_services.py > generated-templates/lakerunner-services.yaml
cfn-lint generated-templates/lakerunner-services.yaml

echo "6. Generating Lakerunner Grafana Service..."
python3 src/lakerunner_grafana_service.py > generated-templates/lakerunner-grafana-service.yaml
cfn-lint generated-templates/lakerunner-grafana-service.yaml

echo "7. Generating Demo OTEL Collector..."
python3 src/demo_otel_collector.py > generated-templates/lakerunner-demo-otel-collector.yaml
cfn-lint generated-templates/lakerunner-demo-otel-collector.yaml

echo "8. Generating Lakerunner MCP Combined Service..."
python3 src/lakerunner_mcp_combined.py > generated-templates/lakerunner-mcp-combined.yaml
cfn-lint generated-templates/lakerunner-mcp-combined.yaml

echo -e "\nGenerated CloudFormation templates:"
ls -la generated-templates/

echo -e "\nNote: cfn-lint warnings above are safe to ignore:"
echo "  - W1030: Empty PublicSubnets parameter is expected when using internal ALB"
echo "  - W1020: Unnecessary Fn::Sub warnings are cosmetic and don't affect functionality"
