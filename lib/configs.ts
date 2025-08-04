import ecs from 'aws-cdk-lib/aws-ecs';

export interface ServiceConfig {
  readonly name: string;
  readonly image: string;
  readonly command?: string[];
  readonly healthCheck?: ecs.HealthCheck;
  readonly cpu?: number;
  readonly memoryMiB?: number;
  readonly replicas?: number;
  readonly environment?: { [key: string]: string };
  readonly ingress?: { port: number; desc?: string, attachAlb?: boolean, healthcheckPath?: string };
}

const goHealthCheck: ecs.HealthCheck = {
  command: [
    '/app/bin/lakerunner',
    'sysinfo',
  ],
};

const scalaHealthCheck: ecs.HealthCheck = {
  command: [
    'curl',
    '-f',
    'http://localhost:7101/ready',
  ],
}

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
  },
  {
    name: 'lakerunner-query-api',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner/query-api:latest-dev',
    cpu: 2048,
    memoryMiB: 8192,
    replicas: 1,
    ingress: { port: 7101, desc: 'Query API', attachAlb: true, healthcheckPath: '/ready' },
    environment: {
      EXECUTION_ENVIRONMENT: 'ecs',
      QUERY_WORKER_DEPLOYMENT_NAME: 'lakerunner-query-worker',
      QUERY_WORKER_SERVICE_NAME: 'lakerunner-query-worker',
      QUERY_STACK: 'local',
      METRIC_PREFIX: 'lakerunner-query-api',
      HOME: '/mnt',
      NUM_MIN_QUERY_WORKERS: '1',
      NUM_MAX_QUERY_WORKERS: '4',
      SPRING_PROFILES_ACTIVE: 'aws',
      TOKEN_HMAC256_KEY: 'alksdjalksdjalkdjalskdjalskdjalkdjalskjdalskdjalk',
    },
    healthCheck: scalaHealthCheck,
  },
  {
    name: 'lakerunner-query-worker',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner/query-worker:latest-dev',
    cpu: 2048,
    memoryMiB: 8192,
    ingress: { port: 7101, desc: 'From Query API' },
    environment: {
      METRIC_PREFIX: 'lakerunner-query-worker',
      HOME: '/mnt',
      SPRING_PROFILES_ACTIVE: 'aws',
      TOKEN_HMAC256_KEY: 'alksdjalksdjalkdjalskdjalskdjalkdjalskjdalskdjalk',
    },
    healthCheck: scalaHealthCheck,
  }
];
