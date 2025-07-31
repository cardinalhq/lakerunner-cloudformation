import ecs from 'aws-cdk-lib/aws-ecs';

export interface ServiceConfig {
  readonly name: string;
  readonly image: string;
  readonly command: string[];
  readonly healthCheck?: ecs.HealthCheck;
  readonly cpu?: number;
  readonly memoryMiB?: number;
  readonly replicas?: number; // Optional, default is 1
}

const goHealthCheck: ecs.HealthCheck = {
  command: [
    '/app/bin/lakerunner',
    'sysinfo',
  ],
};

export const services: ServiceConfig[] = [
  {
    name: 'lakerunner-pubsub-sqs',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'pubsub', 'sqs'],
    cpu: 512,
    memoryMiB: 1024,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-ingest-logs',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'ingest-logs'],
    cpu: 512,
    memoryMiB: 1024,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-ingest-metrics',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'ingest-metrics'],
    cpu: 512,
    memoryMiB: 1024,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-compact-logs',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'compact-logs'],
    cpu: 512,
    memoryMiB: 1024,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-compact-metrics',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'compact-metrics'],
    cpu: 512,
    memoryMiB: 1024,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-rollup-metrics',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'rollup-metrics'],
    cpu: 512,
    memoryMiB: 1024,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-sweeper',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'sweeper'],
    cpu: 512,
    memoryMiB: 1024,
    replicas: 1,
    healthCheck: goHealthCheck,
  }
];
