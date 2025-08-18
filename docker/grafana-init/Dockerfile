# Grafana Init Container
# This container handles Grafana datasource provisioning and data reset functionality
FROM gcr.io/distroless/base-debian12:debug

# Copy the init script
COPY init-grafana.sh /usr/local/bin/init-grafana.sh

# Make the script executable
USER root
RUN chmod +x /usr/local/bin/init-grafana.sh

# Set the entrypoint
ENTRYPOINT ["/usr/local/bin/init-grafana.sh"]