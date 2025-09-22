# S3 and SQS Storage - Manual AWS Console Setup Guide

This guide provides step-by-step instructions to manually create S3 bucket and SQS queue for Lakerunner data processing using the AWS Management Console.

## Prerequisites

- AWS account with appropriate permissions
- [VPC Infrastructure](vpc-manual-setup.md) - For VPC endpoints (optional but recommended)

## What This Creates

- S3 bucket for data storage and processing
- SQS queue for S3 event notifications
- Bucket policies and event notifications
- Lifecycle policies for data retention (optional)

## Option A: Create New Storage Infrastructure

### 1. Create S3 Bucket

1. Navigate to **S3**
1. Click **Create bucket**
1. General configuration:
   - **Bucket name**: `lakerunner-{accountId}-{region}`
     - Replace {accountId} with your AWS account ID
     - Replace {region} with your AWS region
     - Example: `lakerunner-123456789012-us-east-1`
   - **AWS Region**: Select your region
1. Object Ownership:
   - **ACLs disabled (recommended)**
1. Block Public Access settings:
   - **Block all public access**: ✓ (Keep enabled)
1. Bucket Versioning:
   - **Versioning**: Enable (recommended for data recovery)
1. Tags:
   - **Name**: `lakerunner-bucket`
   - **Environment**: `lakerunner`
   - **Component**: `Storage`
1. Default encryption:
   - **Encryption type**: Server-side encryption with Amazon S3 managed keys (SSE-S3)
   - Or use SSE-KMS for additional security
1. Advanced settings:
   - **Object Lock**: Disabled
1. Click **Create bucket**

### 2. Create SQS Queue

1. Navigate to **SQS**
1. Click **Create queue**
1. Details:
   - **Type**: Standard
   - **Name**: `lakerunner-queue`
1. Configuration:
   - **Visibility timeout**: 300 seconds (5 minutes)
   - **Message retention period**: 1209600 seconds (14 days)
   - **Delivery delay**: 0 seconds
   - **Receive message wait time**: 0 seconds
   - **Maximum message size**: 256 KB
1. Encryption:
   - **Server-side encryption**: Enabled
   - **Encryption key type**: Amazon SQS key (SSE-SQS)
1. Access policy:
   - Keep **Basic** for now (will update later)
1. Redrive policy:
   - Optional: Create a dead-letter queue for failed messages
1. Tags:
   - **Name**: `lakerunner-queue`
   - **Environment**: `lakerunner`
   - **Component**: `Storage`
1. Click **Create queue**
1. **Save the Queue URL and ARN**

### 3. Configure SQS Access Policy

Allow S3 to send messages to the queue:

1. Select your queue in SQS console
1. Click **Edit**
1. Go to **Access policy** section
1. Switch to **JSON editor**
1. Replace with this policy (update placeholders):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "s3.amazonaws.com"
      },
      "Action": "SQS:SendMessage",
      "Resource": "arn:aws:sqs:{region}:{accountId}:lakerunner-queue",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "{accountId}"
        },
        "ArnLike": {
          "aws:SourceArn": "arn:aws:s3:::lakerunner-{accountId}-{region}"
        }
      }
    },
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::{accountId}:root"
      },
      "Action": [
        "SQS:ReceiveMessage",
        "SQS:DeleteMessage",
        "SQS:GetQueueAttributes",
        "SQS:ChangeMessageVisibility"
      ],
      "Resource": "arn:aws:sqs:{region}:{accountId}:lakerunner-queue"
    }
  ]
}
```

1. Click **Save**

### 4. Configure S3 Event Notifications

1. Navigate to **S3** and select your bucket
1. Go to **Properties** tab
1. Scroll to **Event notifications**
1. Click **Create event notification**
1. General configuration:
   - **Event name**: `lakerunner-s3-to-sqs`
   - **Prefix**: Leave empty (or specify if needed)
   - **Suffix**: Leave empty (or specify file extensions)
1. Event types:
   - Select all of these:
     - ✓ All object create events
     - Or select specific events:
       - ✓ s3:ObjectCreated:Put
       - ✓ s3:ObjectCreated:Post
       - ✓ s3:ObjectCreated:CompleteMultipartUpload
1. Destination:
   - **Destination type**: SQS queue
   - **SQS queue**: Choose from your account
   - Select `lakerunner-queue`
1. Click **Save changes**

### 5. Create Bucket Policy (Optional)

Add a bucket policy for additional access controls:

1. In S3, select your bucket
1. Go to **Permissions** tab
1. Scroll to **Bucket policy**
1. Click **Edit**
1. Add policy (example for restricting to VPC):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowVPCEndpointAccess",
      "Effect": "Allow",
      "Principal": "*",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::lakerunner-{accountId}-{region}",
        "arn:aws:s3:::lakerunner-{accountId}-{region}/*"
      ],
      "Condition": {
        "StringEquals": {
          "aws:SourceVpce": "vpce-xxxxxxxxx"
        }
      }
    }
  ]
}
```

### 6. Configure Lifecycle Rules (Optional)

Set up automatic data expiration:

1. In S3, select your bucket
1. Go to **Management** tab
1. Click **Create lifecycle rule**
1. Lifecycle rule configuration:
   - **Rule name**: `lakerunner-data-retention`
   - **Status**: Enabled
1. Rule scope:
   - Choose **Apply to all objects in the bucket**
   - Or use prefix/tags for specific data
1. Lifecycle rule actions:
   - ✓ **Expire current versions of objects**
   - Days after object creation: 90 (adjust as needed)
   - ✓ **Permanently delete noncurrent versions**
   - Days after objects become noncurrent: 7
1. Click **Create rule**

## Option B: Use Existing Storage

If you have existing S3 bucket and/or SQS queue:

### 1. Verify S3 Bucket Configuration

Ensure your existing bucket has:

- Appropriate permissions for Lakerunner services
- Event notifications configured (if using SQS)
- Encryption enabled
- Versioning enabled (recommended)

### 2. Update Bucket Policy

Add Lakerunner service access to your existing bucket policy:

```json
{
  "Sid": "LakerunnerAccess",
  "Effect": "Allow",
  "Principal": {
    "AWS": "arn:aws:iam::{accountId}:role/lakerunner-*"
  },
  "Action": [
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket"
  ],
  "Resource": [
    "arn:aws:s3:::your-bucket-name/lakerunner/*",
    "arn:aws:s3:::your-bucket-name"
  ]
}
```

### 3. Configure Existing SQS Queue

If using an existing queue, ensure:

- Visibility timeout is at least 300 seconds
- Access policy allows S3 event notifications
- Access policy allows Lakerunner services to consume messages

## Create Dead Letter Queue (Optional)

For better error handling:

### 1. Create DLQ

1. Navigate to **SQS**
1. Click **Create queue**
1. Configuration:
   - **Name**: `lakerunner-queue-dlq`
   - **Type**: Standard
   - **Message retention period**: 14 days
1. Create queue

### 2. Configure Redrive Policy

1. Select main `lakerunner-queue`
1. Click **Edit**
1. Dead-letter queue section:
   - **Enabled**: Yes
   - **Dead-letter queue**: Select `lakerunner-queue-dlq`
   - **Maximum receives**: 3
1. Save

## Testing the Configuration

### 1. Test S3 to SQS Integration

1. Upload a test file to S3:

   ```bash
   echo "test" > test.txt
   aws s3 cp test.txt s3://lakerunner-{accountId}-{region}/test.txt
   ```

1. Check SQS for message:

   ```bash
   aws sqs receive-message \
     --queue-url https://sqs.{region}.amazonaws.com/{accountId}/lakerunner-queue \
     --max-number-of-messages 1
   ```

1. You should see a message with S3 event details

### 2. Verify Permissions

Test that IAM roles can access the bucket:

```bash
# Assume a Lakerunner role
aws sts assume-role --role-arn arn:aws:iam::{accountId}:role/lakerunner-data-services-task-role \
  --role-session-name test

# Try to list bucket
aws s3 ls s3://lakerunner-{accountId}-{region}/
```

## Outputs to Record

After completing storage setup, record these values:

- **S3 Bucket Name**: `lakerunner-{accountId}-{region}`
- **S3 Bucket ARN**: `arn:aws:s3:::lakerunner-{accountId}-{region}`
- **SQS Queue URL**: `https://sqs.{region}.amazonaws.com/{accountId}/lakerunner-queue`
- **SQS Queue ARN**: `arn:aws:sqs:{region}:{accountId}:lakerunner-queue`
- **DLQ URL** (if created): `https://sqs.{region}.amazonaws.com/{accountId}/lakerunner-queue-dlq`

## Next Steps

With storage configured, proceed to:

1. [IAM Roles Setup](iam-roles-manual-setup.md) - Configure service permissions
1. [ECS Services Setup](../services-manual-setup.md) - Deploy data processing services

## Troubleshooting

### Common Issues

1. **S3 events not reaching SQS:**
   - Verify event notification configuration
   - Check SQS access policy allows S3
   - Ensure bucket and queue are in same region
   - Test with a simple file upload

1. **Access denied errors:**
   - Check bucket policy
   - Verify IAM role permissions
   - Ensure no conflicting deny policies
   - Check for S3 Block Public Access settings

1. **Messages stuck in queue:**
   - Check visibility timeout setting
   - Verify consumer services are running
   - Check for processing errors in logs
   - Monitor dead letter queue

1. **Storage issues:**
   - Review lifecycle policies
   - Check for incomplete multipart uploads
   - Review access patterns
   - Check storage configuration
