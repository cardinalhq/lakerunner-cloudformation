# Obtaining a TLS certificate for the Cardinal ALB

The Cardinal stack does not issue or auto-create TLS certificates. The operator brings one. This document covers the three supported paths.

## Pick the hostname first

The cert is bound to the **hostname users will type in the browser**, not the ALB DNS name. AWS owns the ALB DNS (`internal-cardinal-...elb.amazonaws.com`); no public CA will issue a cert for it.

Pick a hostname inside a domain you control, for example `cardinal.acme.com`. Issue the cert for that name. After install, create a DNS record `cardinal.acme.com → <ALB DNS>` (Route 53 Alias if your zone is in Route 53, otherwise CNAME). Browsers hit `cardinal.acme.com`, SNI sends that hostname to the ALB, the ALB serves the matching cert, validation passes.

## Path 1: ACM (recommended for production)

Use this when the stack is in an AWS account where you can manage DNS records for the chosen hostname. Cert is auto-renewed by AWS.

### Console flow

1. Open **AWS Certificate Manager** in the same region as the stack (e.g. `us-east-2`).
2. **Request** → **Request a public certificate** → Next.
3. **Fully qualified domain name**: `cardinal.acme.com`. Add additional names if you want a SAN cert.
4. **Validation method**: **DNS validation** (recommended). Email validation also works but requires WHOIS-listed contacts to click a link.
5. Submit. ACM displays one CNAME record per name to add to your DNS.
6. If the hosted zone is in Route 53 in the same account, click **Create records in Route 53** — ACM writes the validation CNAMEs for you. Otherwise add them by hand in your DNS provider.
7. Wait for status to flip from **Pending validation** → **Issued** (typically a few minutes once DNS propagates).
8. Copy the **ARN** from the cert details page. It looks like `arn:aws:acm:us-east-2:123456789012:certificate/abcd1234-...`.

### CLI equivalent

```sh
aws acm request-certificate \
    --region us-east-2 \
    --domain-name cardinal.acme.com \
    --validation-method DNS \
    --query CertificateArn --output text
# -> arn:aws:acm:us-east-2:123456789012:certificate/abcd1234-...

# Get the validation CNAME record(s) to add to DNS:
aws acm describe-certificate \
    --region us-east-2 \
    --certificate-arn arn:aws:acm:us-east-2:123456789012:certificate/abcd1234-... \
    --query 'Certificate.DomainValidationOptions[].ResourceRecord'
```

After validation propagates and the cert flips to `ISSUED`, supply the ARN to the deploy:

| Cardinal parameter | Value |
|---|---|
| `CertificateArn` | the ACM ARN |
| `CertificateBody`, `CertificatePrivateKey`, `CertificateChain` | leave empty |

### Constraints

- **Same account, same region.** ACM certs are regional. The cert must be in the same region as the ALB. (For CloudFront distributions the cert must be in `us-east-1`, but Cardinal uses an ALB, so use the stack's region.)
- ACM-issued certs **cannot be exported**. The private key never leaves AWS. That's fine for the ALB use case (the ALB pulls it directly).
- ACM auto-renews 60 days before expiry, provided the validation records are still in place.

## Path 2: Bring an existing cert

Use this when you already have a cert from your own CA (corporate CA, Let's Encrypt, DigiCert, etc.).

### Required PEM material

Three files (the third is optional):

- **Certificate body** — your leaf certificate, PEM-encoded. **Leaf only**, not the chain.
- **Private key** — PEM-encoded, **not password-protected**. ACM rejects encrypted keys. Convert with `openssl rsa -in encrypted.key -out plain.key` if needed.
- **Certificate chain** (optional, but usually required) — intermediate cert(s) PEM-encoded, in order from the cert that signed your leaf up toward (but typically not including) the root.

### Sanity checks before pasting

```sh
# Cert and key must match (these two should print the same hash):
openssl x509 -noout -modulus -in cert.pem    | openssl md5
openssl rsa  -noout -modulus -in private.key | openssl md5

# Inspect the cert: confirm CN/SAN matches your hostname and not-after is in the future:
openssl x509 -noout -text -in cert.pem | grep -E "Subject:|DNS:|Not After"

# Verify the chain (chain.pem can hold multiple intermediates concatenated):
openssl verify -CAfile chain.pem cert.pem
```

### Format gotchas

- PEM files must start with `-----BEGIN ...-----` and end with `-----END ...-----` followed by a **trailing newline**. Missing newline at end of file is a common rejection cause.
- Line endings must be **LF** (`\n`), not CRLF (`\r\n`). Strip with `tr -d '\r' < file > file.lf`.
- The chain file may contain multiple `-----BEGIN CERTIFICATE-----` blocks concatenated. Order: intermediate that signed your leaf first, then the next one up, etc. Most CAs publish a "chain" or "intermediate bundle" file that's already in this order.
- **Do not** include the leaf certificate in the chain file. Body = leaf only; chain = intermediates only.
- **Do not** include the root in the chain unless your CA explicitly says to. ALB clients trust the root from their local trust store.

### Where it goes

| Cardinal parameter | Value |
|---|---|
| `CertificateArn` | leave empty |
| `CertificateBodyCredentialId` | Jenkins **Secret File** ID for `cert.pem` |
| `CertificatePrivateKeyCredentialId` | Jenkins **Secret File** ID for `private.key` |
| `CertificateChainCredentialId` | Jenkins **Secret File** ID for `chain.pem` (optional but usually required) |

When running the script outside Jenkins, use `--certificate-body-file`, `--certificate-private-key-file`, `--certificate-chain-file` flags pointing at the local files.

The cert child stack imports the PEMs into ACM via a Lambda custom resource. The resulting ACM ARN is wired into the ALB. Replacing the cert later (renewal, key rotation) is a stack update with new PEM values.

## Path 3: Self-signed for dev / private deployments

Use this for internal testing or air-gapped deployments where browsers will warn and that's acceptable, or when clients are configured to trust your private CA.

### One-liner

```sh
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout private.key \
    -out cert.pem \
    -days 365 \
    -subj "/CN=cardinal.acme.local" \
    -addext "subjectAltName=DNS:cardinal.acme.local,DNS:*.cardinal.acme.local"
```

- `-nodes` skips key encryption (required — ACM rejects encrypted keys).
- `-days 365` sets validity. Bump for longer-lived dev certs.
- The CN and SANs should match the hostname users will type. SANs matter more than CN for modern browsers.
- No chain file is needed for a self-signed cert.

### Where it goes

Same as Path 2 (`CertificateBody*`/`CertificatePrivateKey*` credentials), but `CertificateChainCredentialId` stays empty.

Browser users will see a "Not secure" warning the first time and must click through. To eliminate the warning on internal machines, distribute the cert as a trusted root via your endpoint management or `Keychain Access` / Windows Cert Manager.

## After install

Once the stack reports `CREATE_COMPLETE`:

1. Find the ALB DNS name in the stack outputs (or in the EC2 console under **Load Balancers**).
2. Create a DNS record pointing your hostname at the ALB DNS:
    - **Route 53 in the same account**: use an **A** record with **Alias** target = the ALB.
    - **Other DNS provider**: use a **CNAME** record `cardinal.acme.com → internal-cardinal-...elb.amazonaws.com`.
3. Test: `curl -v https://cardinal.acme.com/` should complete the TLS handshake without warnings (or with the expected self-signed warning on Path 3).

## Renewing or rotating a cert

- **ACM (Path 1):** automatic. Nothing to do as long as the validation records remain.
- **Imported PEMs (Path 2/3):** generate new PEMs, run the deploy job again with new credential IDs (or new file contents in the same credential ID). The cert child re-imports and the ALB picks up the new ARN. Old ACM cert (the previous import) is left in place — clean up manually via `aws acm delete-certificate` once you've confirmed the new one is serving traffic.
