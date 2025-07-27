export interface ServiceConfig {
  readonly name: string;
  readonly image: string;
  readonly command: string[];
  readonly cpu?: number;
  readonly memoryMiB?: number;
}

export const services: ServiceConfig[] = [
  {
    name:    'ingest-logs',
    image:   'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner','ingest-logs'],
    cpu:     512,
    memoryMiB: 1024,
  },
  {
    name:    'ingest-metrics',
    image:   'public.ecr.aws/cardinalhq.io/lakerunner:latest',
    command: ['/app/bin/lakerunner','ingest-metrics'],
    cpu:     512,
    memoryMiB: 1024,
  },
  // …all 15 services…
];
