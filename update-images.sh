#!/bin/bash
# Helper script to update CloudFormation stacks with latest container image versions
# Usage: ./update-images.sh <stack-name> <stack-type>
# Stack types: services, migration

set -e

STACK_NAME="$1"
STACK_TYPE="$2"

if [ -z "$STACK_NAME" ] || [ -z "$STACK_TYPE" ]; then
    echo "Usage: $0 <stack-name> <stack-type>"
    echo "Stack types: services, migration"
    echo "Example: $0 lakerunner-services services"
    exit 1
fi

# Current image versions
GO_SERVICES_IMAGE="public.ecr.aws/cardinalhq.io/lakerunner:v1.5.0"
MIGRATION_IMAGE="public.ecr.aws/cardinalhq.io/lakerunner:v1.5.0"

case "$STACK_TYPE" in
    "services")
        echo "Updating services stack: $STACK_NAME"
        aws cloudformation update-stack \
            --stack-name "$STACK_NAME" \
            --template-body file://generated-templates/lakerunner-services.yaml \
            --parameters \
                ParameterKey=CommonInfraStackName,UsePreviousValue=true \
                ParameterKey=GoServicesImage,ParameterValue="$GO_SERVICES_IMAGE" \
                ParameterKey=QueryApiImage,ParameterValue="$GO_SERVICES_IMAGE" \
                ParameterKey=QueryWorkerImage,ParameterValue="$GO_SERVICES_IMAGE" \
            --capabilities CAPABILITY_IAM
        ;;
    "migration")
        echo "Updating migration stack: $STACK_NAME"
        aws cloudformation update-stack \
            --stack-name "$STACK_NAME" \
            --template-body file://generated-templates/lakerunner-migration.yaml \
            --parameters \
                ParameterKey=CommonInfraStackName,UsePreviousValue=true \
                ParameterKey=ContainerImage,ParameterValue="$MIGRATION_IMAGE" \
            --capabilities CAPABILITY_IAM
        ;;
    *)
        echo "Error: Unknown stack type '$STACK_TYPE'"
        echo "Supported types: services, migration"
        exit 1
        ;;
esac

echo "Update initiated successfully. Monitor the stack status with:"
echo "aws cloudformation describe-stacks --stack-name $STACK_NAME --query 'Stacks[0].StackStatus'"
