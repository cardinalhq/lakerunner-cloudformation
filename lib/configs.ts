/*
 * Copyright (C) 2025 CardinalHQ, Inc
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, version 3.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program. If not, see <http://www.gnu.org/licenses/>.
 */

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
  readonly bindMounts?: ecs.MountPoint[];
  readonly efsMounts?: [{ containerPath: string, efsPath: string, apName: string }];
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
    cpu: 1024,
    memoryMiB: 2048,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-ingest-logs',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'ingest-logs'],
    cpu: 1024,
    memoryMiB: 2048,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-ingest-metrics',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'ingest-metrics'],
    cpu: 1024,
    memoryMiB: 2048,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-compact-logs',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'compact-logs'],
    cpu: 1024,
    memoryMiB: 2048,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-compact-metrics',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'compact-metrics'],
    cpu: 1024,
    memoryMiB: 2048,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-rollup-metrics',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'rollup-metrics'],
    cpu: 1024,
    memoryMiB: 2048,
    replicas: 1,
    healthCheck: goHealthCheck,
  },
  {
    name: 'lakerunner-sweeper',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'sweeper'],
    cpu: 1024,
    memoryMiB: 2048,
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
      NUM_MIN_QUERY_WORKERS: '1',
      NUM_MAX_QUERY_WORKERS: '4',
      SPRING_PROFILES_ACTIVE: 'aws',
      TOKEN_HMAC256_KEY: 'alksdjalksdjalkdjalskdjalskdjalkdjalskjdalskdjalk',
    },
    healthCheck: scalaHealthCheck,
    bindMounts: [
      {
        containerPath: '/db',
        readOnly: false,
        sourceVolume: 'scratch',
      },
    ],
  },
  {
    name: 'lakerunner-query-worker',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner/query-worker:latest-dev',
    cpu: 2048,
    memoryMiB: 8192,
    ingress: { port: 7101, desc: 'From Query API' },
    environment: {
      METRIC_PREFIX: 'lakerunner-query-worker',
      SPRING_PROFILES_ACTIVE: 'aws',
      TOKEN_HMAC256_KEY: 'alksdjalksdjalkdjalskdjalskdjalkdjalskjdalskdjalk',
    },
    healthCheck: scalaHealthCheck,
    bindMounts: [
      {
        containerPath: '/db',
        readOnly: false,
        sourceVolume: 'scratch',
      },
    ],
  },
  {
    name: 'grafana',
    image: 'grafana/grafana:latest',
    cpu: 512,
    memoryMiB: 1024,
    ingress: { port: 3000, desc: 'Grafana', attachAlb: true, healthcheckPath: '/api/health' },
    environment: {
      GF_SECURITY_ADMIN_USER: 'admin',
      GF_SECURITY_ADMIN_PASSWORD: 'f70603aa00e6f67999cc66e336134887',
      GF_SERVER_HTTP_PORT: '3000',
      GF_SERVER_ROOT_URL: "%(protocol)s://%(domain)s:%(http_port)s",
      GF_INSTALL_PLUGINS: "https://github.com/cardinalhq/cardinalhq-lakerunner-datasource/raw/refs/heads/main/cardinalhq-lakerunner-datasource.zip;cardinalhq-lakerunner-datasource",
    },
    efsMounts: [{ containerPath: '/var/lib/grafana', efsPath: '/grafana', apName: 'grafana' }],
    healthCheck: {
      command: [
        'curl',
        '-f',
        'http://localhost:3000/api/health',
      ],
    },
  },
];
