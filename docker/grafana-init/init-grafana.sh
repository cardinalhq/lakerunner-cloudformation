#!/bin/sh
# Grafana Initialization Script
# Handles datasource provisioning and data reset functionality

set -e

echo "Starting Grafana initialization..."

# Set up provisioning directory
PROVISIONING_DIR="${GF_PATHS_PROVISIONING:-/etc/grafana/provisioning}"
DATASOURCES_DIR="$PROVISIONING_DIR/datasources"
RESET_TOKEN_FILE="/var/lib/grafana/.grafana_reset_token"

echo "Provisioning directory: $PROVISIONING_DIR"
echo "Datasources directory: $DATASOURCES_DIR"
echo "Reset token file: $RESET_TOKEN_FILE"

# Create provisioning directories
mkdir -p "$DATASOURCES_DIR"

# All containers run as root, so no ownership changes needed

# Handle reset token logic
if [ -n "$RESET_TOKEN" ] && [ "$RESET_TOKEN" != "" ]; then
    echo "Reset token provided: $RESET_TOKEN"

    # Check if token file exists and compare
    if [ -f "$RESET_TOKEN_FILE" ]; then
        STORED_TOKEN=$(cat "$RESET_TOKEN_FILE")
        if [ "$STORED_TOKEN" != "$RESET_TOKEN" ]; then
            echo "Reset token changed from '$STORED_TOKEN' to '$RESET_TOKEN' - wiping Grafana data"
            # Remove all Grafana data
            find /var/lib/grafana -mindepth 1 -delete || true
            # Create new token file
            echo "$RESET_TOKEN" > "$RESET_TOKEN_FILE"
        else
            echo "Reset token unchanged - no reset needed"
        fi
    else
        echo "First time with reset token - storing: $RESET_TOKEN"
        echo "$RESET_TOKEN" > "$RESET_TOKEN_FILE"
    fi
else
    echo "No reset token provided - skipping reset logic"
fi

# Write datasource configuration
echo "Writing Grafana datasource configuration..."
# The datasource config comes from SSM parameter as YAML, just write it directly
cat > "$DATASOURCES_DIR/cardinal.yaml" << DATASOURCE_EOF
$GRAFANA_DATASOURCE_CONFIG
DATASOURCE_EOF

echo "Grafana datasource configuration written to $DATASOURCES_DIR/cardinal.yaml"
echo "Grafana initialization complete"

# Verify the configuration was written correctly
if [ -f "$DATASOURCES_DIR/cardinal.yaml" ]; then
    echo "Datasource configuration file created successfully"
    echo "Configuration preview:"
    head -10 "$DATASOURCES_DIR/cardinal.yaml" || true
else
    echo "Failed to create datasource configuration file"
    exit 1
fi

echo "Init container completed successfully"