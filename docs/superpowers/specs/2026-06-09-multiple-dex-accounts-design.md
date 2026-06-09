# Multiple Dex Login Accounts

Status: approved (2026-06-09)

## Goal

Let an operator configure more than one bundled-Dex login account, each with
its own username and password, so non-admin users can be invited as org
members and sign in to Maestro without an external IdP. Today the bundled Dex
renders exactly one `staticPasswords` entry from `DEX_ADMIN_EMAIL` +
`DEX_ADMIN_HASH`; this adds an additive list of further accounts.

## Background: why this needs two repos

Two mechanisms were conflated in the original request:

1. `OidcSuperadminEmails` (Maestro env, CSV) is an **authorization allowlist**:
   "if someone with this email authenticates, grant them superadmin." It does
   not create login accounts.
2. Dex `staticPasswords` are the actual **credentials**. The live config is
   rendered inside the `dex-customization` image by gomplate from env vars
   (`config/config.docker.yaml`), and emits a single entry. Dex uses in-memory
   storage with no runtime user management, so every account must be baked into
   config at task start.

A user cannot log in unless Dex has a `staticPasswords` record for them, so the
real enabler lives in the `dex-customization` image. The CloudFormation repo
only threads env vars into the Dex container. Therefore the feature is a
coordinated change across:

- `dex-customization` — render N `staticPasswords` from a new env var.
- `lakerunner-cloudformation` — add the parameter and wire it through the
  shell driver, root stack, and Maestro child to the Dex container env.

Maestro needs no change: it provisions a user on first OIDC login and keys org
membership by email. "Inviting a member" is a runtime in-app admin action.

## Repo 1: dex-customization

Extend `config/config.docker.yaml`. The admin stays as entry 0, unchanged; a
new `DEX_EXTRA_USERS` env var (JSON array) renders additional entries:

```gotemplate
staticPasswords:
  - email: "{{ getenv "DEX_ADMIN_EMAIL" | required "DEX_ADMIN_EMAIL is required" }}"
    hash: "{{ getenv "DEX_ADMIN_HASH" | required "DEX_ADMIN_HASH is required" }}"
    username: "admin"
    userID: "00000000-0000-0000-0000-000000000001"
{{- $extra := getenv "DEX_EXTRA_USERS" "[]" | data.JSONArray }}
{{- range $u := $extra }}
{{-   $email := $u.email | required "each DEX_EXTRA_USERS entry needs an email" }}
{{-   $un := index $u "username" }}
{{-   $uid := index $u "userID" }}
{{-   $h := crypto.SHA256 $email }}
  - email: "{{ $email }}"
    hash: "{{ $u.hash | required "each DEX_EXTRA_USERS entry needs a hash" }}"
    username: "{{ if $un }}{{ $un }}{{ else }}{{ index (strings.Split "@" $email) 0 }}{{ end }}"
    userID: "{{ if $uid }}{{ $uid }}{{ else }}{{ printf "%s-%s-%s-%s-%s" (slice $h 0 8) (slice $h 8 12) (slice $h 12 16) (slice $h 16 20) (slice $h 20 32) }}{{ end }}"
{{- end }}
```

Properties:

- Uses `data.JSONArray` (gomplate's `data.JSON` parses only a top-level object,
  not an array). Unset/empty `DEX_EXTRA_USERS` defaults to `"[]"` -> zero extra
  entries -> byte-identical render to today (back-compat).
- The operator supplies only `email` + `hash` per entry. Optional `username`/
  `userID` are read with `index` (a missing map key under gomplate's strict mode
  errors via `.field` access but returns nil via `index`). `username` defaults
  to the email local part; `userID` defaults to a stable UUID-shaped digest of
  the email -- gomplate has no `uuid.V5`, so the id is built from the first 32
  hex chars of `crypto.SHA256(email)`. Dex only needs a stable, unique, non-empty
  id; both fields may be overridden explicitly.
- bcrypt `$` survives: JSON does not escape `$`, and gomplate renders in a
  single pass without re-scanning its output (the same property the admin hash
  already relies on).
- Malformed JSON, or an entry missing `email`/`hash`, fails the render loudly
  via `required` -> Dex will not start -> ECS deployment circuit breaker rolls
  back. Consistent with the existing fail-loud config contract.

Testing: extend `test/config_render_smoke.sh` with a 2-user `DEX_EXTRA_USERS`
case asserting two extra `staticPasswords` entries render with derived username
and userID, and that an empty/unset value renders exactly the single admin
entry. Release as `v0.4.0`.

## Repo 2: lakerunner-cloudformation

1. `src/cardinal_cfn/children/maestro.py`: add one `NoEcho` parameter
   `DexExtraUsers` (default `""`), set it on the Dex container as
   `Environment(Name="DEX_EXTRA_USERS", Value=Ref("DexExtraUsers"))`, and add it
   to the "DEX configuration" parameter group. Hashes stay in env (one-way,
   matching `DEX_ADMIN_HASH` today; no Secrets Manager indirection).
2. `src/cardinal_cfn/root.py`: declare `DexExtraUsers` and forward it to the
   Maestro child stack.
3. `scripts/deploy-lakerunner-services.sh`: mirror the cert string-or-file
   duality.
   - `DEX_EXTRA_USERS` (inline, single-line JSON) -> appended to `PARAMS` as
     `DexExtraUsers=...`. Safe because `scripts-src/parts/base.sh` assembles the
     parameter list as JSON via `jq`, not the AWS CLI `--parameter-overrides`
     shorthand, so commas / quotes / `$` in the value survive. The only
     restriction is no embedded newlines (newlines delimit `PARAMS` entries).
   - `DEX_EXTRA_USERS_FILE` (path to a pretty-printed JSON file) -> routed via
     `FILE_PARAMS` (multi-line safe), exactly like `CERTIFICATE_BODY_FILE`. The
     inline form wins when both are set.
4. Image pin: bump `cardinal-defaults.yaml` `images.dex` and the driver's
   `DEX_IMAGE_SUFFIX` to the published `v0.4.0` digest. **Ordering: publish the
   dex image first, then bump the pin** -- the old image ignores the new env
   var, so a premature pin bump would ship a silently-inert parameter.
5. Tests under `tests/templates/`: assert `DexExtraUsers` exists, is `NoEcho`,
   defaults empty, sits in the DEX parameter group, and renders the
   `DEX_EXTRA_USERS` env var on the Dex container; root forwards it to the
   Maestro child.

## Operator story (documentation, no code)

Supply accounts as JSON, e.g.:

```json
[{"email":"alice@acme.com","hash":"$2y$10$..."},
 {"email":"bob@acme.com","hash":"$2y$10$..."}]
```

via `DEX_EXTRA_USERS` (inline) or `DEX_EXTRA_USERS_FILE` (file). Generate each
hash the same way as the admin hash
(`htpasswd -bnBC 10 "" 'password' | tr -d ':\n' | sed 's/^/$/' | sed 's/2y/2a/'`).

- To make one of them a **superadmin**, also add their email to
  `OIDC_SUPERADMIN_EMAILS`.
- For a **plain org member**, leave them out of superadmin; an admin invites
  them to the org in-app after their first login (Maestro keys membership by
  email -- no stack change).
- Do not repeat the admin's email in the list (duplicate `staticPasswords`
  email is undefined; first match wins on login).

## Non-goals / YAGNI

- No runtime user management: Dex is in-memory and static-config by design;
  every account is a config entry applied at task start. Add/remove is a stack
  update.
- No plaintext-password hashing in the stack: operators supply pre-bcrypted
  hashes, as today.
- No HA for Dex: inherits upstream's single-replica, in-memory, POC-grade
  posture. Login state is lost on task restart.
- Large lists (~30+) approach the 4 KB CloudFormation parameter-value limit;
  that is the documented signal to move to an external IdP rather than the
  bundled Dex.

## Upgrade / operational notes

- Existing installs that set nothing new render identically -- no upgrade action
  for operators who do not want extra accounts.
- Changing `DEX_EXTRA_USERS` replaces the Maestro task (Dex re-renders config on
  the next task), and rotates Dex's in-memory signing keys, so existing browser
  sessions must re-authenticate -- the same churn any Dex config change causes.
