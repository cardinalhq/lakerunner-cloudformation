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
mkdir -p generated-templates/ecs generated-templates/eks

echo "=== Generating ECS Templates ==="

echo "1. Generating Lakerunner Common Infrastructure (ECS)..."
python3 src/ecs/lakerunner_common.py > generated-templates/ecs/lakerunner-common.yaml
cfn-lint generated-templates/ecs/lakerunner-common.yaml

echo "2. Generating Lakerunner Migration Task (ECS)..."
python3 src/ecs/lakerunner_migration.py > generated-templates/ecs/lakerunner-migration.yaml
cfn-lint generated-templates/ecs/lakerunner-migration.yaml

echo "3. Generating Lakerunner Services (ECS)..."
python3 src/ecs/lakerunner_services.py > generated-templates/ecs/lakerunner-services.yaml
cfn-lint generated-templates/ecs/lakerunner-services.yaml

echo "4. Generating Lakerunner Grafana Service (ECS)..."
python3 src/ecs/lakerunner_grafana_service.py > generated-templates/ecs/lakerunner-grafana-service.yaml
cfn-lint generated-templates/ecs/lakerunner-grafana-service.yaml

echo "=== Generating EKS Templates ==="

# Check if EKS templates exist before attempting to generate
if [ -f "src/eks/lakerunner_eks_vpc.py" ]; then
  echo "5. Generating EKS VPC Infrastructure..."
  python3 src/eks/lakerunner_eks_vpc.py > generated-templates/eks/lakerunner-eks-vpc.yaml
  cfn-lint generated-templates/eks/lakerunner-eks-vpc.yaml
fi

if [ -f "src/eks/lakerunner_eks_data.py" ]; then
  echo "6. Generating EKS Data Layer..."
  python3 src/eks/lakerunner_eks_data.py > generated-templates/eks/lakerunner-eks-data.yaml
  cfn-lint generated-templates/eks/lakerunner-eks-data.yaml
fi

if [ -f "src/eks/lakerunner_eks_cluster.py" ]; then
  echo "7. Generating EKS Cluster..."
  python3 src/eks/lakerunner_eks_cluster.py > generated-templates/eks/lakerunner-eks-cluster.yaml
  cfn-lint generated-templates/eks/lakerunner-eks-cluster.yaml
fi

if [ -f "src/eks/lakerunner_eks_production.py" ]; then
  echo "8. Generating EKS Production Orchestration..."
  python3 src/eks/lakerunner_eks_production.py > generated-templates/eks/lakerunner-eks-production.yaml
  cfn-lint generated-templates/eks/lakerunner-eks-production.yaml
fi

echo -e "\nGenerated CloudFormation templates:"
echo "ECS Templates:"
ls -la generated-templates/ecs/
echo -e "\nEKS Templates:"
ls -la generated-templates/eks/

echo -e "\nNote: cfn-lint warnings above are safe to ignore:"
echo "  - W1030: Empty PublicSubnets parameter is expected when using internal ALB"
echo "  - W1020: Unnecessary Fn::Sub warnings are cosmetic and don't affect functionality"
