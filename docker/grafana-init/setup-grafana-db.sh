#!/bin/bash
# Grafana Database Setup Script
# Creates PostgreSQL database and user for Grafana with proper permissions

set -e

echo "Starting Grafana database setup..."

# Validate required environment variables
if [ -z "$GRAFANA_DB_NAME" ]; then
    echo "ERROR: GRAFANA_DB_NAME environment variable is required"
    exit 1
fi

if [ -z "$GRAFANA_DB_USER" ]; then
    echo "ERROR: GRAFANA_DB_USER environment variable is required"
    exit 1
fi

if [ -z "$GRAFANA_DB_PASSWORD" ]; then
    echo "ERROR: GRAFANA_DB_PASSWORD environment variable is required"
    exit 1
fi

if [ -z "$PGHOST" ]; then
    echo "ERROR: PGHOST environment variable is required"
    exit 1
fi

if [ -z "$PGUSER" ]; then
    echo "ERROR: PGUSER environment variable is required"
    exit 1
fi

if [ -z "$PGPASSWORD" ]; then
    echo "ERROR: PGPASSWORD environment variable is required"
    exit 1
fi

echo "Database setup parameters:"
echo "  Host: $PGHOST"
echo "  Port: ${PGPORT:-5432}"
echo "  Admin DB: ${PGDATABASE:-postgres}"
echo "  SSL Mode: ${PGSSLMODE:-require}"
echo "  Grafana DB: $GRAFANA_DB_NAME"
echo "  Grafana User: $GRAFANA_DB_USER"

# Create database if it doesn't exist
echo "Checking if database '$GRAFANA_DB_NAME' exists..."
DB_EXISTS=$(psql -tc "SELECT 1 FROM pg_database WHERE datname = '$GRAFANA_DB_NAME'" | grep -q 1 && echo "yes" || echo "no")

if [ "$DB_EXISTS" = "no" ]; then
    echo "Creating database '$GRAFANA_DB_NAME'..."
    psql -c "CREATE DATABASE $GRAFANA_DB_NAME"
    echo "Database '$GRAFANA_DB_NAME' created successfully"
else
    echo "Database '$GRAFANA_DB_NAME' already exists"
fi

# Create user if it doesn't exist
echo "Checking if user '$GRAFANA_DB_USER' exists..."
USER_EXISTS=$(psql -tc "SELECT 1 FROM pg_user WHERE usename = '$GRAFANA_DB_USER'" | grep -q 1 && echo "yes" || echo "no")

if [ "$USER_EXISTS" = "no" ]; then
    echo "Creating user '$GRAFANA_DB_USER'..."
    psql -c "CREATE USER $GRAFANA_DB_USER WITH PASSWORD '$GRAFANA_DB_PASSWORD'"
    echo "User '$GRAFANA_DB_USER' created successfully"
else
    echo "User '$GRAFANA_DB_USER' already exists"
    # Update password in case it changed
    echo "Updating password for user '$GRAFANA_DB_USER'..."
    psql -c "ALTER USER $GRAFANA_DB_USER WITH PASSWORD '$GRAFANA_DB_PASSWORD'"
fi

# Grant database privileges
echo "Granting privileges on database '$GRAFANA_DB_NAME' to user '$GRAFANA_DB_USER'..."
psql -c "GRANT ALL PRIVILEGES ON DATABASE $GRAFANA_DB_NAME TO $GRAFANA_DB_USER"

# Connect to the Grafana database to set up schema permissions
echo "Setting up schema permissions in database '$GRAFANA_DB_NAME'..."
PGDATABASE=$GRAFANA_DB_NAME psql -c "GRANT ALL ON SCHEMA public TO $GRAFANA_DB_USER"
PGDATABASE=$GRAFANA_DB_NAME psql -c "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $GRAFANA_DB_USER"
PGDATABASE=$GRAFANA_DB_NAME psql -c "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $GRAFANA_DB_USER"

# Set default privileges for future objects
echo "Setting default privileges for future objects..."
PGDATABASE=$GRAFANA_DB_NAME psql -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $GRAFANA_DB_USER"
PGDATABASE=$GRAFANA_DB_NAME psql -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $GRAFANA_DB_USER"

echo "Grafana database setup completed successfully!"
echo "Database '$GRAFANA_DB_NAME' is ready for Grafana with user '$GRAFANA_DB_USER'"

# Check if we need to reset the admin password
if [ -f "/app/scripts/reset-grafana-admin.sh" ]; then
    echo ""
    echo "Checking for admin password reset..."
    # Export variables needed by reset script
    export GF_SECURITY_ADMIN_USER="${GF_SECURITY_ADMIN_USER:-admin}"
    export GF_SECURITY_ADMIN_PASSWORD="${GF_SECURITY_ADMIN_PASSWORD}"
    /app/scripts/reset-grafana-admin.sh
fi