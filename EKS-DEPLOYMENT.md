# Lakerunner EKS Deployment Guide

This guide covers deploying Lakerunner on AWS using EKS (Elastic Kubernetes Service) with CloudFormation nested stacks.

## Overview

The EKS deployment consists of nested CloudFormation stacks for production-ready deployment:

1. **Production Stack** - Master orchestration stack
   - **VPC Stack** - Networking infrastructure (VPC, subnets, security groups, VPC endpoints)
   - **Data Stack** - Data layer (RDS PostgreSQL, S3 bucket, SQS queue)
   - **Cluster Stack** - EKS cluster with node groups and IRSA setup

Lakerunner services are deployed via Helm charts with KEDA for autoscaling.

## Prerequisites

- AWS CLI configured with appropriate permissions
- kubectl installed and configured
- Helm 3.x installed
- Python 3.8+ with virtual environment

## Architecture

### Networking
- Private subnets only (no public subnets)
- NAT gateways for internet access
- VPC endpoints for AWS services (S3, ECR, CloudWatch, STS)
- Security groups for EKS control plane and worker nodes

### Data Layer
- RDS PostgreSQL 17 database
- S3 bucket with lifecycle policies
- SQS queue with S3 event notifications
- SSM parameters for API keys and storage profiles

### EKS Cluster
- Managed EKS cluster with private endpoint access
- Managed node groups with autoscaling
- IRSA (IAM Roles for Service Accounts) for secure AWS service access
- KEDA installed for event-driven autoscaling

## Quick Start

1. **Generate Templates**
   ```bash
   ./build.sh
   ```
   Templates will be generated in `generated-templates/eks/`

2. **Deploy Production Stack**
   ```bash
   aws cloudformation create-stack \
     --stack-name lakerunner-eks-production \
     --template-body file://generated-templates/eks/lakerunner-eks-production.yaml \
     --parameters \
       ParameterKey=VpcCidr,ParameterValue=10.0.0.0/16 \
       ParameterKey=PrivateSubnet1Cidr,ParameterValue=10.0.1.0/24 \
       ParameterKey=PrivateSubnet2Cidr,ParameterValue=10.0.2.0/24 \
     --capabilities CAPABILITY_IAM
   ```

3. **Wait for Stack Creation**
   ```bash
   aws cloudformation wait stack-create-complete --stack-name lakerunner-eks-production
   ```

4. **Configure kubectl**
   ```bash
   aws eks update-kubeconfig --region us-east-1 --name lakerunner-eks-cluster
   ```

5. **Verify Cluster Access**
   ```bash
   kubectl get nodes
   kubectl get pods -A
   ```

6. **Deploy Lakerunner Services**
   ```bash
   # Add Lakerunner Helm repository
   helm repo add lakerunner https://charts.example.com/lakerunner
   helm repo update
   
   # Deploy Lakerunner with values from stack outputs
   helm install lakerunner lakerunner/lakerunner \
     --set database.host=$(aws cloudformation describe-stacks --stack-name lakerunner-eks-production --query 'Stacks[0].Outputs[?OutputKey==`DatabaseHost`].OutputValue' --output text) \
     --set sqs.queueUrl=$(aws cloudformation describe-stacks --stack-name lakerunner-eks-production --query 'Stacks[0].Outputs[?OutputKey==`QueueUrl`].OutputValue' --output text) \
     --set s3.bucketName=$(aws cloudformation describe-stacks --stack-name lakerunner-eks-production --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' --output text)
   ```

7. **Deploy KEDA**
   ```bash
   helm repo add kedacore https://kedacore.github.io/charts
   helm repo update
   helm install keda kedacore/keda --namespace keda-system --create-namespace
   ```

## Configuration Parameters

### VPC Configuration
- **VpcCidr** - CIDR block for VPC (default: 10.0.0.0/16)
- **PrivateSubnet1Cidr** - First private subnet CIDR (default: 10.0.1.0/24)
- **PrivateSubnet2Cidr** - Second private subnet CIDR (default: 10.0.2.0/24)

### Database Configuration
- **DbInstanceClass** - RDS instance type (default: db.t4g.micro)
- **DbAllocatedStorage** - Database storage in GB (default: 20)
- **DbEngineVersion** - PostgreSQL version (default: 17)

### EKS Configuration
- **EksVersion** - EKS cluster version (default: 1.31)
- **NodeInstanceType** - EC2 instance type for nodes (default: t3.medium)
- **NodeGroupMinSize** - Minimum nodes (default: 1)
- **NodeGroupMaxSize** - Maximum nodes (default: 3)
- **NodeGroupDesiredSize** - Desired nodes (default: 2)

## Networking Details

### Private-Only Architecture
- No public subnets created
- All EKS worker nodes in private subnets
- NAT gateways provide outbound internet access
- EKS control plane accessible via private endpoints only

### VPC Endpoints
The deployment creates VPC endpoints for:
- **S3** - S3 gateway endpoint for data access
- **ECR** - Container image repository access
- **CloudWatch** - Logging and monitoring
- **STS** - IAM role assumption for IRSA

### Security Groups
- **Control Plane Security Group** - EKS cluster API server access
- **Node Group Security Group** - Worker node communication
- **Database Security Group** - PostgreSQL access from EKS nodes

## IRSA (IAM Roles for Service Accounts)

The cluster is configured with OpenID Connect for secure AWS service access:

- **Pod Identity** - Each service gets its own IAM role
- **No Instance Profiles** - No long-lived credentials on nodes
- **Least Privilege** - Fine-grained permissions per service

## Monitoring and Logging

### CloudWatch Integration
- Container logs automatically sent to CloudWatch
- Cluster metrics available in CloudWatch
- VPC Flow Logs for network monitoring

### Observability Stack
Deploy additional monitoring with:

```bash
# Prometheus and Grafana
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack

# Fluent Bit for log forwarding
helm repo add fluent https://fluent.github.io/helm-charts
helm install fluent-bit fluent/fluent-bit
```

## KEDA Autoscaling

KEDA provides event-driven autoscaling based on:

- **SQS Queue Length** - Scale based on pending messages
- **CloudWatch Metrics** - Scale based on custom metrics
- **Prometheus Metrics** - Scale based on application metrics

Example KEDA ScaledObject:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: lakerunner-pubsub-scaler
spec:
  scaleTargetRef:
    name: lakerunner-pubsub
  triggers:
  - type: aws-sqs-queue
    metadata:
      queueURL: ${SQS_QUEUE_URL}
      queueLength: "10"
      awsRegion: us-east-1
```

## Scaling

### Cluster Autoscaler
The node groups support automatic scaling:

```bash
# Deploy cluster autoscaler
kubectl apply -f https://raw.githubusercontent.com/kubernetes/autoscaler/master/cluster-autoscaler/cloudprovider/aws/examples/cluster-autoscaler-autodiscover.yaml

# Configure for your cluster
kubectl -n kube-system annotate deployment.apps/cluster-autoscaler cluster-autoscaler.kubernetes.io/safe-to-evict="false"
kubectl -n kube-system edit deployment.apps/cluster-autoscaler
```

### Horizontal Pod Autoscaling
Services can scale based on CPU/memory:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: lakerunner-api-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: lakerunner-query-api
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 80
```

## Storage

### Persistent Volumes
EKS supports multiple storage classes:

- **gp3** - General purpose SSD (default)
- **gp2** - Previous generation SSD
- **efs** - Elastic File System for shared storage

### Database
- **RDS PostgreSQL** - Managed database service
- **Multi-AZ** - High availability deployment
- **Automated Backups** - Point-in-time recovery
- **Encryption** - Data encrypted at rest and in transit

## Security

### Network Security
- Private subnets isolate workloads
- Security groups restrict traffic
- VPC endpoints avoid internet routing
- Network policies for pod-to-pod communication

### Pod Security
- Pod Security Standards enforced
- Non-root containers by default
- Read-only root filesystems
- Resource limits and requests

### Secrets Management
- AWS Secrets Manager integration
- External Secrets Operator for GitOps
- Service account token volume projection

## Troubleshooting

### Common Issues

1. **Cluster Creation Fails**
   ```bash
   # Check CloudFormation events
   aws cloudformation describe-stack-events --stack-name lakerunner-eks-production
   
   # Check EKS cluster status
   aws eks describe-cluster --name lakerunner-eks-cluster
   ```

2. **Nodes Not Joining**
   ```bash
   # Check node group status
   aws eks describe-nodegroup --cluster-name lakerunner-eks-cluster --nodegroup-name lakerunner-nodes
   
   # Check EC2 instances
   kubectl get nodes -o wide
   ```

3. **Pod Scheduling Issues**
   ```bash
   # Check pod status
   kubectl describe pod <pod-name>
   
   # Check resource availability
   kubectl top nodes
   kubectl describe nodes
   ```

### Useful Commands

```bash
# Cluster information
kubectl cluster-info
kubectl get nodes -o wide

# Check system pods
kubectl get pods -A

# View logs
kubectl logs -f deployment/lakerunner-pubsub-sqs

# Check KEDA status
kubectl get scaledobjects
kubectl get hpa

# Debug networking
kubectl run debug --image=nicolaka/netshoot -it --rm
```

## Backup and Recovery

### Database Backups
- Automated daily backups with 7-day retention
- Manual snapshots before major changes
- Point-in-time recovery up to 35 days

### Application Backups
```bash
# Backup Kubernetes resources
kubectl get all -o yaml > lakerunner-backup.yaml

# Backup using Velero
velero backup create lakerunner-backup --include-namespaces default
```

## Cleanup

To remove the deployment:

```bash
# Remove Helm releases
helm uninstall lakerunner
helm uninstall keda -n keda-system

# Delete CloudFormation stack
aws cloudformation delete-stack --stack-name lakerunner-eks-production

# Wait for deletion
aws cloudformation wait stack-delete-complete --stack-name lakerunner-eks-production
```

## Customization

### Custom Helm Values
Create a `values.yaml` file:

```yaml
replicaCount: 3

image:
  repository: your-registry.com/lakerunner
  tag: "v2.0.0"

resources:
  limits:
    cpu: 1000m
    memory: 2Gi
  requests:
    cpu: 500m
    memory: 1Gi

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 10

keda:
  enabled: true
  triggers:
    - type: aws-sqs-queue
      metadata:
        queueURL: "${SQS_QUEUE_URL}"
        queueLength: "10"
```

Deploy with custom values:
```bash
helm install lakerunner lakerunner/lakerunner -f values.yaml
```

## Production Considerations

- **Multi-Region Deployment** - Deploy in multiple regions for HA
- **Cross-AZ Database** - Enable Multi-AZ for RDS
- **Backup Strategy** - Implement comprehensive backup/restore procedures
- **Monitoring** - Deploy full observability stack
- **Security Scanning** - Regularly scan container images
- **Cost Optimization** - Use Spot instances for non-critical workloads