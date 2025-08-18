# Grafana Init Container
# This container handles Grafana datasource provisioning and data reset functionality
FROM alpine:3.20

# Install bash for script execution
RUN apk add --no-cache bash

# Copy the init script (COPY automatically creates parent directories)
COPY init-grafana.sh /app/scripts/init-grafana.sh

# Make the script executable
RUN chmod +x /app/scripts/init-grafana.sh

# Set the entrypoint
ENTRYPOINT ["/app/scripts/init-grafana.sh"]