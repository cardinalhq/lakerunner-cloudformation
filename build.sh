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

echo "1. Generating Common Infrastructure..."
python3 src/common_infra.py > generated-templates/common_infra.yaml
cfn-lint generated-templates/common_infra.yaml

echo "2. Generating Migration Task..."
python3 src/migration_task.py > generated-templates/migration_task.yaml
cfn-lint generated-templates/migration_task.yaml

echo "3. Generating Services..."
python3 src/services.py > generated-templates/services.yaml
cfn-lint generated-templates/services.yaml

echo "4. Generating OTEL Collector..."
python3 src/otel_collector.py > generated-templates/otel_collector.yaml
cfn-lint generated-templates/otel_collector.yaml

echo -e "\n✅ Generated CloudFormation templates:"
ls -la generated-templates/
