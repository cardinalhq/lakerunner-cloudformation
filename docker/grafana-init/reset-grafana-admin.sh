#!/bin/bash
# Grafana Admin Password Reset Script
# Resets the Grafana admin password in PostgreSQL database when reset token changes

set -e

echo "Starting Grafana admin password reset check..."

# Check for required environment variables
if [ -z "$RESET_TOKEN" ] || [ "$RESET_TOKEN" = "" ]; then
    echo "No reset token provided - skipping password reset check"
    exit 0
fi

if [ -z "$GRAFANA_DB_NAME" ] || [ -z "$GRAFANA_DB_USER" ] || [ -z "$GRAFANA_DB_PASSWORD" ]; then
    echo "Database credentials not provided - skipping password reset"
    exit 0
fi

if [ -z "$GF_SECURITY_ADMIN_USER" ] || [ -z "$GF_SECURITY_ADMIN_PASSWORD" ]; then
    echo "Admin credentials not provided - skipping password reset"
    exit 0
fi

echo "Reset token provided: $RESET_TOKEN"
echo "Admin user to reset: $GF_SECURITY_ADMIN_USER"

# Set up PostgreSQL connection for Grafana database
export PGHOST="${PGHOST}"
export PGPORT="${PGPORT:-5432}"
export PGDATABASE="${GRAFANA_DB_NAME}"
export PGUSER="${GRAFANA_DB_USER}"
export PGPASSWORD="${GRAFANA_DB_PASSWORD}"
export PGSSLMODE="${PGSSLMODE:-require}"

# Check if Grafana tables exist
echo "Checking if Grafana is already initialized..."
TABLE_EXISTS=$(psql -tc "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='user'" 2>/dev/null | grep -q 1 && echo "yes" || echo "no")

if [ "$TABLE_EXISTS" = "no" ]; then
    echo "Grafana tables don't exist yet - first run will use environment variables"
    exit 0
fi

# Store reset token in database to track changes
echo "Checking reset token..."
RESET_TOKEN_TABLE_EXISTS=$(psql -tc "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='reset_token'" 2>/dev/null | grep -q 1 && echo "yes" || echo "no")

if [ "$RESET_TOKEN_TABLE_EXISTS" = "no" ]; then
    echo "Creating reset token tracking table..."
    psql -c "CREATE TABLE reset_token (token VARCHAR(255) PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
fi

# Check if this token has been applied before
TOKEN_EXISTS=$(psql -tc "SELECT 1 FROM reset_token WHERE token = '$RESET_TOKEN'" 2>/dev/null | grep -q 1 && echo "yes" || echo "no")

if [ "$TOKEN_EXISTS" = "yes" ]; then
    echo "Reset token '$RESET_TOKEN' already applied - skipping password reset"
    exit 0
fi

echo "New reset token detected - resetting admin password..."

# Generate bcrypt hash for the password
# Grafana uses bcrypt with cost 10 by default
# Since we can't easily generate bcrypt in bash, we'll delete the user and let Grafana recreate it
echo "Resetting admin user '$GF_SECURITY_ADMIN_USER'..."

# Delete the admin user - Grafana will recreate it on startup with the password from environment
psql -c "DELETE FROM \"user\" WHERE login = '$GF_SECURITY_ADMIN_USER'" || true

# Also delete any existing sessions to force re-login
psql -c "DELETE FROM user_auth_token WHERE user_id IN (SELECT id FROM \"user\" WHERE login = '$GF_SECURITY_ADMIN_USER')" || true

# Record that this token has been applied
psql -c "INSERT INTO reset_token (token) VALUES ('$RESET_TOKEN')"

echo "Admin user deleted from database. Grafana will recreate it with the new password on startup."
echo "Password reset completed successfully!"