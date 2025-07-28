export interface ServiceConfig {
  readonly name: string;
  readonly image: string;
  readonly command: string[];
  readonly cpu?: number;
  readonly memoryMiB?: number;
  readonly replicas?: number; // Optional, default is 1
}

export const services: ServiceConfig[] = [
  {
    name: 'ingest-logs',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'ingest-logs'],
    cpu: 512,
    memoryMiB: 1024,
    replicas: 1,
  },
  {
    name: 'ingest-metrics',
    image: 'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner', 'ingest-metrics'],
    cpu: 512,
    memoryMiB: 1024,
    replicas: 1,
  },
  // …all 15 services…
];
