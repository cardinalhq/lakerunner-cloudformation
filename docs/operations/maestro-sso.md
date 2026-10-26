# Maestro login through a customer IdP

By default Maestro users log in through the bundled DEX with local accounts
(`DEX_ADMIN_*`, `DEX_EXTRA_USERS`). To use the customer's own SSO instead, point
Maestro at their OIDC IdP — Okta, Entra ID, Keycloak, or anything that issues
OIDC tokens. This is how Cardinal's own production Maestro runs (against
Keycloak, which brokers Google).

Setting `OIDC_ISSUER_URL` replaces DEX: the services stack drops the dex
container, its target group and `/dex` listener rule, and the `DEX_*` settings
are ignored. Maestro trusts exactly one issuer, so DEX cannot stay on as a
fallback login.

## IdP setup

Register a **public** client (single-page app, authorization code + PKCE, no
client secret) with redirect URI `https://<host>/`, where `<host>` is
`PUBLIC_DNS_NAME` (or the ALB DNS name if that is unset). Its tokens must carry
the user's email; to grant superadmin by group, they must also carry groups.

IdPs that require a client secret for browser logins (Google OAuth used
directly, for one) do not work; put a broker such as Keycloak or Okta in front.

## Driver settings

The services driver passes each of these to the Maestro container under the
same name; they are the maestro helm chart's settings. Only `OIDC_ISSUER_URL`
is required.

| Env var | Default | Notes |
|---|---|---|
| `OIDC_ISSUER_URL` | (DEX) | Issuer URL; must match the tokens' `iss` exactly. |
| `OIDC_CLIENT_ID` | `OIDC_AUDIENCE` | Client id the UI sends to the IdP. |
| `OIDC_AUDIENCE` | `maestro-ui` | Expected `aud` (differs from the client id on Okta custom auth servers). |
| `OIDC_JWKS_URL` | `<issuer>/protocol/openid-connect/certs` | Keycloak's path; set it for other IdPs (Okta: `<issuer>/v1/keys`). |
| `OIDC_SUPERADMIN_EMAILS` | `admin@cardinal.local` | Emails granted superadmin. |
| `OIDC_SUPERADMIN_GROUP` | `maestro-superadmin` | Group granted superadmin; setting it also requests the `groups` scope. |
| `OIDC_EMAIL_CLAIM` | `email` | Claim mapping overrides for IdPs whose tokens differ from Keycloak's. |
| `OIDC_EMAIL_VERIFIED_CLAIM` | `email_verified` | |
| `OIDC_DISPLAY_NAME_CLAIMS` | `name,preferred_username,email` | |
| `OIDC_GROUPS_CLAIM` | `groups` | |
| `OIDC_EXTERNAL_ID_CLAIM` | `sub` | Persistent account key; changing it on a live install is a migration event. |
| `OIDC_TRUST_UNVERIFIED_EMAILS` | `false` | For IdPs that omit `email_verified` (often Entra ID). |
| `MCP_OAUTH_ENABLED` | `false` | OAuth sign-in for MCP clients on `/mcp`; needs an external issuer. |
| `MCP_OAUTH_ISSUER` | `OIDC_ISSUER_URL` | Authorization server advertised to MCP clients. |
| `MCP_OAUTH_AUDIENCE` | `<base URL>/mcp` | `aud` accepted on `/mcp` only. |
| `MCP_OAUTH_AS_METADATA_PROXY` | `false` | Serve OAuth AS metadata mirrored from the issuer (Keycloak realms). |
| `MCP_OAUTH_SELF_SIGNUP` | `false` | Personal workspaces for new MCP sign-ins. |

Per-IdP claim mapping guidance: <https://docs.cardinalhq.io/maestro/install/oidc#claim-mapping>.

Also set `DEX_ADMIN_EMAIL` to the owner's IdP email: Maestro makes that address
the owner of the org it pre-populates. `DEX_ADMIN_PASSWORD_HASH` is not needed.

```sh
OIDC_ISSUER_URL=https://auth.example.com/realms/example \
OIDC_SUPERADMIN_GROUP=maestro-superadmin \
DEX_ADMIN_EMAIL=owner@example.com \
STACK_NAME=cardinal-lakerunner-services REGION=us-east-1 ... \
  ./deploy-lakerunner-services.sh
```

Unset variables keep the stack's current values on update. To go back to DEX,
pass `PARAMS="OidcIssuerUrl="` along with `DEX_ADMIN_PASSWORD_HASH`.
