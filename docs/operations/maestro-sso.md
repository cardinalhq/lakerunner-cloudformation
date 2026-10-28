# Maestro login through a customer IdP

By default Maestro users log in through the bundled DEX with local accounts
(`DEX_ADMIN_*`, `DEX_EXTRA_USERS`). To use the customer's own SSO instead, point
Maestro at their OIDC IdP — Okta, Keycloak, or anything that issues
OIDC tokens (Entra ID through a broker; see below). This is how Cardinal's own production Maestro runs (against
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
| `OIDC_TRUST_UNVERIFIED_EMAILS` | `false` | For IdPs that omit `email_verified`. |
| `OIDC_BEARER_TOKEN` | `access` | `id` makes the UI send the ID token; needed for Amazon Cognito. |
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

## Microsoft Entra ID

Entra ID cannot be Maestro's issuer directly. Maestro's UI requests only
`openid profile email` and sends the resulting access token. Entra issues that
token for Microsoft Graph (`aud` `00000003-0000-0000-c000-000000000000`), and a
Graph token cannot be verified by anyone but Graph. Broker Entra through
Keycloak (or Okta) and point Maestro at the broker, or, when no broker is an
option, federate Entra into an Amazon Cognito user pool and use
[Cognito](#amazon-cognito) as the issuer.

In Entra (**App registrations → New registration**):

- Platform: **Web**, redirect URI
  `https://<keycloak-host>/realms/<realm>/broker/entra/endpoint`.
- **Certificates & secrets → New client secret**. Keycloak holds this secret,
  not Maestro.
- **Token configuration → Add optional claim → ID → `email`**, so users whose
  UPN differs from their mail address still get an email.
- Note the **Directory (tenant) ID** and **Application (client) ID**.

In Keycloak, in the realm Maestro will use:

- **Identity providers → OpenID Connect v1.0**, alias `entra`:
    - Discovery endpoint:
      `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration`
    - Client ID / secret: from the Entra app registration.
    - Client authentication: **Client secret sent as post**.
    - Default scopes: `openid profile email`.
    - Trust email: **on** (Entra sends no `email_verified`).
- The `maestro-ui` public client and `groups` mapper from
  <https://docs.cardinalhq.io/maestro/install/oidc#keycloak>, with redirect URI
  `https://<host>/*`.
- Optional: **Authentication → Browser flow → Identity Provider Redirector**,
  default provider `entra`, to skip the Keycloak login page.

Entra puts group object IDs (GUIDs), not names, in its `groups` claim. Either
map an Entra group to the Keycloak `maestro-superadmin` group with an identity
provider mapper (**Claim to Group**, claim `groups`, value the Entra group's
object ID), or grant superadmin by email.

```sh
OIDC_ISSUER_URL=https://auth.example.com/realms/example \
OIDC_SUPERADMIN_GROUP=maestro-superadmin \
DEX_ADMIN_EMAIL=owner@example.com \
STACK_NAME=cardinal-lakerunner-services REGION=us-east-1 ... \
  ./deploy-lakerunner-services.sh
```

These are the Keycloak values: Maestro sees only Keycloak, and the defaults for
`OIDC_AUDIENCE` and `OIDC_JWKS_URL` already fit it.

## Amazon Cognito

A Cognito user pool works as the issuer, including one that federates Entra ID
(**Sign-in experience → Federated identity provider sign-in → OpenID Connect**,
with the Entra app registration's client id and secret). Cognito access tokens
carry no `aud`, `azp` or `email`, so set `OIDC_BEARER_TOKEN=id` to have the UI
send the ID token, which has `aud` (the app client id), `email`,
`email_verified` and `cognito:groups`.

In the user pool, create an app client: public (no secret), authorization code
grant with PKCE, callback URL `https://<host>/`, sign-out URL `https://<host>/`,
scopes `openid profile email`.

```sh
OIDC_ISSUER_URL=https://cognito-idp.<region>.amazonaws.com/<pool-id> \
OIDC_JWKS_URL=https://cognito-idp.<region>.amazonaws.com/<pool-id>/.well-known/jwks.json \
OIDC_AUDIENCE=<app client id> \
OIDC_BEARER_TOKEN=id \
OIDC_GROUPS_CLAIM=cognito:groups \
OIDC_SUPERADMIN_GROUP=maestro-superadmin \
DEX_ADMIN_EMAIL=owner@example.com \
STACK_NAME=cardinal-lakerunner-services REGION=us-east-1 ... \
  ./deploy-lakerunner-services.sh
```

`OIDC_CLIENT_ID` defaults to `OIDC_AUDIENCE`. `OIDC_GROUPS_CLAIM` and
`OIDC_SUPERADMIN_GROUP` are only needed for group-based superadmin (a Cognito
group named `maestro-superadmin`); otherwise grant it by email.
