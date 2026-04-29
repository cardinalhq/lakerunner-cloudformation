"""Source code for the cert custom-resource Lambda.

Embedded in cert.yaml as Code.ZipFile. Behavior contract:

- Create: imports CertificateBody/PrivateKey/Chain into ACM, returns the new
  cert ARN as PhysicalResourceId and as Data.CertificateArn.
- Update: re-imports into the existing ARN if any of body/key/chain changed
  (keeps the ARN stable so the ALB listener attachment does not flap).
- Delete: deletes the ACM cert. Retries on ResourceInUseException — when the
  root stack is being torn down, the ALB listener releases the cert
  asynchronously; the retry loop covers that window.
- The Lambda is only deployed when the parent template's ImportCert condition
  is true; consumers that pass an existing CertificateArn never see this code.
"""

SOURCE = '''\
import json
import time
import traceback
import urllib.request

import boto3
from botocore.exceptions import ClientError


acm = boto3.client("acm")

MAX_REASON_LEN = 1000


def _send(event, context, status, reason="", physical_id=None, data=None):
    body = json.dumps({
        "Status": status,
        "Reason": (reason or f"see CloudWatch log {context.log_stream_name}")[:MAX_REASON_LEN],
        "PhysicalResourceId": physical_id or event.get("PhysicalResourceId") or "cardinal-cert-unknown",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }).encode("utf-8")
    req = urllib.request.Request(
        url=event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"content-type": "", "content-length": str(len(body))},
    )
    urllib.request.urlopen(req).read()


def _import_kwargs(props, existing_arn=None):
    body = props.get("CertificateBody") or ""
    key = props.get("CertificatePrivateKey") or ""
    if not body or not key:
        raise ValueError("CertificateBody and CertificatePrivateKey are required")
    kwargs = {
        "Certificate": body.encode("utf-8"),
        "PrivateKey": key.encode("utf-8"),
    }
    chain = props.get("CertificateChain") or ""
    if chain:
        kwargs["CertificateChain"] = chain.encode("utf-8")
    if existing_arn:
        kwargs["CertificateArn"] = existing_arn
    return kwargs


def _do_import(props, existing_arn=None):
    response = acm.import_certificate(**_import_kwargs(props, existing_arn))
    arn = response["CertificateArn"]
    install_id_long = props.get("InstallIdLong") or ""
    tags = [
        {"Key": "Name", "Value": f"cardinal-cert-{install_id_long}" if install_id_long else "cardinal-cert"},
        {"Key": "cardinal:component", "Value": "cert"},
    ]
    if install_id_long:
        tags.append({"Key": "cardinal:install-id-long", "Value": install_id_long})
    try:
        acm.add_tags_to_certificate(CertificateArn=arn, Tags=tags)
    except ClientError as exc:
        # Tagging is best-effort; the cert is still usable without tags.
        print(f"add_tags_to_certificate failed: {exc!r}", flush=True)
    return arn


def _delete_with_retry(arn, deadline):
    last_exc = None
    while time.time() < deadline:
        try:
            acm.delete_certificate(CertificateArn=arn)
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                return
            if code == "ResourceInUseException":
                last_exc = exc
                time.sleep(15)
                continue
            raise
    if last_exc is not None:
        raise last_exc


def _is_acm_arn(value):
    return isinstance(value, str) and value.startswith("arn:") and ":acm:" in value


def lambda_handler(event, context):
    request_type = event["RequestType"]
    props = event.get("ResourceProperties") or {}
    old_props = event.get("OldResourceProperties") or {}
    physical_id = event.get("PhysicalResourceId")
    existing_arn = physical_id if _is_acm_arn(physical_id) else None

    try:
        if request_type == "Delete":
            if existing_arn:
                _delete_with_retry(existing_arn, time.time() + 14 * 60)
            _send(event, context, "SUCCESS",
                  physical_id=physical_id or "cardinal-cert-noop")
            return

        if request_type == "Create":
            arn = _do_import(props)
            _send(event, context, "SUCCESS", physical_id=arn,
                  data={"CertificateArn": arn})
            return

        # Update
        cert_changed = (
            (props.get("CertificateBody") or "") != (old_props.get("CertificateBody") or "")
            or (props.get("CertificatePrivateKey") or "") != (old_props.get("CertificatePrivateKey") or "")
            or (props.get("CertificateChain") or "") != (old_props.get("CertificateChain") or "")
        )
        if existing_arn and cert_changed:
            arn = _do_import(props, existing_arn=existing_arn)
        elif existing_arn:
            arn = existing_arn
        else:
            arn = _do_import(props)
        _send(event, context, "SUCCESS", physical_id=arn,
              data={"CertificateArn": arn})

    except Exception as exc:
        print(f"CERT IMPORT FAILED: {exc!r}", flush=True)
        traceback.print_exc()
        try:
            _send(event, context, "FAILED", reason=str(exc),
                  physical_id=physical_id or "cardinal-cert-failed")
        except Exception as send_exc:
            print(f"FAILED to notify CFN: {send_exc!r}", flush=True)
            raise
'''
