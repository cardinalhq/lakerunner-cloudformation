# Grafana Init Container
# This container handles Grafana datasource provisioning, data reset functionality, and database setup
FROM postgres:13-alpine

# Install bash for script execution (postgres image has bash, but being explicit)
RUN apk add --no-cache bash

# Copy the scripts (COPY automatically creates parent directories)
COPY init-grafana.sh /app/scripts/init-grafana.sh
COPY setup-grafana-db.sh /app/scripts/setup-grafana-db.sh

# Make the scripts executable
RUN chmod +x /app/scripts/init-grafana.sh /app/scripts/setup-grafana-db.sh

# Set the entrypoint
ENTRYPOINT ["/app/scripts/init-grafana.sh"]