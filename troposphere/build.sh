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

if [ -d "out" ]; then
  rm -rf out
fi
mkdir out

echo "1. Generating Common Infrastructure..."
python3 common_infra.py > out/common_infra.yaml
cfn-lint out/common_infra.yaml

echo "2. Generating Migration Task..."
python3 migration_task.py > out/migration_task.yaml
cfn-lint out/migration_task.yaml

echo "3. Generating Services..."
python3 services.py > out/services.yaml
cfn-lint out/services.yaml

echo -e "\n✅ Generated CloudFormation templates:"
ls -la out/
