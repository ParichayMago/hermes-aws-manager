"""AWS operations for the authenticated Hermes dashboard."""

import ast
import base64
import binascii
import hashlib
import io
import ipaddress
import json
import re
import sqlite3
import time
import uuid
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from typing import Literal

import boto3
from agent.secret_scope import get_secret
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException
from plugins.plugin_storage import plugin_db
from pydantic import BaseModel, Field

router = APIRouter()
REGIONS = ("us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-central-1", "ap-south-1",
           "ap-southeast-1", "ap-northeast-1", "ca-central-1")
INSTANCE_TYPES = ("t3.micro", "t3.small", "t3.medium", "t3.large", "m7i.large")
RUNTIMES = ("python3.12", "python3.13", "python3.14")
MAX_FILE = 4 * 1024 * 1024
DEMO_ACCOUNT = "123456789012"
PLAN_TTL = 300
READ_CONFIG = Config(connect_timeout=4, read_timeout=12, retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True)
WRITE_CONFIG = Config(connect_timeout=4, read_timeout=28, retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True)
EC2_ID = r"i-(?:[a-f0-9]{8}|[a-f0-9]{17})"
RESOURCE_ID = r"[a-f0-9]{8}(?:[a-f0-9]{9})?"
NAME = r"[A-Za-z0-9_-]{1,64}"
S3_NAME = r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"
KINDS = ("ec2", "lambda", "s3")
ACTIONS = {
    "ec2.create": "Launch EC2 instance", "ec2.start": "Start instance", "ec2.stop": "Stop instance",
    "ec2.reboot": "Reboot instance", "ec2.resize": "Change instance type", "ec2.rename": "Rename instance",
    "ec2.terminate": "Terminate instance", "ec2.default_vpc": "Create default VPC",
    "ec2.security_group": "Create security group", "ec2.key_import": "Import public key",
    "lambda.create": "Create Lambda function", "lambda.deploy": "Deploy Lambda code",
    "lambda.configure": "Update Lambda settings", "lambda.invoke": "Invoke Lambda function",
    "lambda.delete": "Delete Lambda function", "lambda.role_create": "Create Lambda execution role",
    "lambda.url_create": "Create function URL", "lambda.url_delete": "Remove function URL",
    "s3.create": "Create S3 bucket", "s3.connect": "Connect existing bucket", "s3.disconnect": "Disconnect bucket",
    "s3.upload": "Upload S3 object", "s3.remove": "Delete current S3 object",
    "s3.delete": "Delete empty bucket",
}
DESTRUCTIVE = {"ec2.terminate", "lambda.delete", "lambda.url_delete", "s3.remove", "s3.delete"}
READBACK_ACTIONS = {"ec2.create", "s3.upload"}
STARTER = 'def lambda_handler(event, context):\n    return {"message": "Hello from Hermes", "event": event}\n'


class Scope(BaseModel):
    mode: Literal["demo", "live"] = "demo"
    region: str = "us-east-1"


class Proposal(BaseModel):
    scope: Scope
    action: str
    values: dict = Field(default_factory=dict)


class Apply(BaseModel):
    confirmation: str = Field(default="", max_length=1024)
    payload: str = Field(default="", max_length=6 * 1024 * 1024)


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def invalid(message, status=400):
    raise HTTPException(status, message)


def aws_error(exc):
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "AWS error")
        message = exc.response.get("Error", {}).get("Message", "")
        return f"{code}: {message}"[:350]
    return type(exc).__name__


def safe_read(call):
    try:
        return call()
    except (BotoCoreError, ClientError) as exc:
        invalid(aws_error(exc), 502)


@contextmanager
def storage(write=False):
    with closing(plugin_db("aws-manager", "workspace.db")) as db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY, mode TEXT, region TEXT, account TEXT, principal TEXT,
            action TEXT, target TEXT, parameters TEXT, proof TEXT, status TEXT,
            submitted REAL, finished TEXT, result TEXT, error TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS connections (
            mode TEXT, account TEXT, region TEXT, bucket TEXT, PRIMARY KEY(mode, account, region, bucket)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS demo_resources (
            region TEXT, kind TEXT, id TEXT, data TEXT, PRIMARY KEY(region, kind, id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS demo_objects (
            region TEXT, bucket TEXT, key TEXT, body BLOB, modified TEXT,
            PRIMARY KEY(region, bucket, key)
        )""")
        db.execute("CREATE TABLE IF NOT EXISTS demo_regions (region TEXT PRIMARY KEY)")
        with db:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db


def credentials():
    key = get_secret("AWS_MANAGER_ACCESS_KEY_ID")
    secret = get_secret("AWS_MANAGER_SECRET_ACCESS_KEY")
    token = get_secret("AWS_MANAGER_SESSION_TOKEN")
    if not key or not secret:
        invalid("Add AWS credentials in the AWS Manager plugin settings, then refresh.", 409)
    return {"aws_access_key_id": key, "aws_secret_access_key": secret, "aws_session_token": token or None}


def connection(scope, *, write=False):
    if scope.region not in REGIONS:
        invalid("Select a supported AWS region.")
    if scope.mode == "demo":
        seed(scope.region)
        return DEMO_ACCOUNT, "demo", None
    session = boto3.Session(region_name=scope.region, **credentials())
    identity = safe_read(lambda: session.client("sts", config=READ_CONFIG).get_caller_identity())
    if write and identity["Arn"].endswith(":root"):
        invalid("Use an IAM role or IAM user for AWS changes.", 403)
    return identity["Account"], identity["Arn"], session


def text(values, key, *, pattern=None, limit=255, optional=False):
    value = values.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        invalid(f"Invalid {key.replace('_', ' ')}.")
    if not value and optional:
        return value
    if not value or (pattern and not re.fullmatch(pattern, value)):
        invalid(f"Invalid {key.replace('_', ' ')}.")
    return value


def choice(values, key, allowed):
    value = text(values, key)
    if value not in allowed:
        invalid(f"Unsupported {key.replace('_', ' ')}.")
    return value


def number(values, key, low, high):
    value = values.get(key)
    if type(value) is not int or not low <= value <= high:
        invalid(f"{key.replace('_', ' ').capitalize()} must be between {low} and {high}.")
    return value


def bucket_name(value):
    if not re.fullmatch(S3_NAME, value) or value.startswith(("xn--", "sthree-", "amzn-s3-demo-")) \
            or value.endswith(("-s3alias", "--ol-s3", "--x-s3", "--table-s3", "-an")):
        invalid("Enter a valid general purpose bucket name using lowercase letters, numbers and hyphens.")
    return value


def object_key(values):
    key = text(values, "key", limit=1024)
    if len(key.encode()) > 1024 or any(ord(char) < 32 for char in key):
        invalid("Enter a valid S3 object key.")
    return key


def role_arn(values, key, account, *, profile=False, optional=False):
    kind = "instance-profile" if profile else "role"
    return text(values, key, pattern=rf"arn:aws:iam::{account}:{kind}/[A-Za-z0-9_+=,.@/\-]+",
                limit=255, optional=optional)


def payload_bytes(action, payload):
    if action == "ec2.key_import":
        if len(payload) > 8192 or not re.fullmatch(r"(?:ssh-rsa|ssh-ed25519) [A-Za-z0-9+/=]+(?: [^\r\n]*)?", payload):
            invalid("Paste an OpenSSH RSA or Ed25519 public key.")
        return payload.encode()
    if action == "s3.upload":
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            invalid("Invalid upload content.")
        if len(data) > MAX_FILE:
            invalid("Choose a file up to 4 MiB.")
        return data
    if action in {"lambda.create", "lambda.deploy"}:
        if not 0 < len(payload.encode()) <= 64 * 1024:
            invalid("Provide up to 64 KiB of Python source.")
        try:
            tree = ast.parse(payload)
        except (SyntaxError, ValueError, RecursionError):
            invalid("The Python source has a syntax error.")
        if not any(isinstance(node, ast.FunctionDef) and node.name == "lambda_handler" for node in tree.body):
            invalid("Define a lambda_handler(event, context) function.")
        return payload.encode()
    if action == "lambda.invoke":
        if len(payload.encode()) > 64 * 1024:
            invalid("The test event must be at most 64 KiB.")
        try:
            json.loads(payload)
        except (ValueError, RecursionError):
            invalid("The test event must be valid JSON.")
        return payload.encode()
    if payload:
        invalid("This action does not accept a payload.")
    return b""


def normalized(action, values, account):
    kind, verb = action.split(".")
    if kind == "ec2":
        if verb == "default_vpc":
            return {"name": "Default VPC"}
        if verb == "security_group":
            cidr = text(values, "ssh_cidr", limit=32, optional=True)
            if cidr:
                try:
                    network = ipaddress.IPv4Network(cidr)
                except ValueError:
                    invalid("Enter an IPv4 address range such as 203.0.113.10/32.")
                if network.prefixlen == 0:
                    invalid("Use an SSH source smaller than the entire internet.")
                cidr = str(network)
            return {"name": text(values, "name", pattern=NAME),
                    "vpc": text(values, "vpc", pattern=f"vpc-{RESOURCE_ID}"), "ssh_cidr": cidr}
        if verb == "key_import":
            return {"name": text(values, "name", pattern=NAME)}
        if verb == "create":
            public = values.get("public_ip", False)
            if type(public) is not bool:
                invalid("Invalid public IP option.")
            return {
                "name": text(values, "name", pattern=r"[\w .-]{1,64}"),
                "type": choice(values, "type", INSTANCE_TYPES),
                "subnet": text(values, "subnet", pattern=f"subnet-{RESOURCE_ID}"),
                "security_group": text(values, "security_group", pattern=f"sg-{RESOURCE_ID}"),
                "disk_gib": number(values, "disk_gib", 8, 128),
                "key_pair": text(values, "key_pair", limit=255, optional=True),
                "instance_profile": role_arn(values, "instance_profile", account, profile=True, optional=True),
                "public_ip": public,
            }
        settings = {"id": text(values, "id", pattern=EC2_ID)}
        if verb == "resize":
            settings["type"] = choice(values, "type", INSTANCE_TYPES)
        if verb == "rename":
            settings["name"] = text(values, "name", pattern=r"[\w .-]{1,64}")
        return settings
    if kind == "lambda":
        settings = {"name": text(values, "name", pattern=NAME)}
        if verb == "role_create":
            settings["function"] = text(values, "function", pattern=NAME)
        if verb in {"create", "configure"}:
            settings.update(memory=number(values, "memory", 128, 10240),
                            timeout=number(values, "timeout", 1, 900))
        if verb == "create":
            settings.update(runtime=choice(values, "runtime", RUNTIMES),
                            role=role_arn(values, "role", account))
        return settings
    settings = {"bucket": bucket_name(text(values, "bucket", limit=63))}
    if verb in {"upload", "remove"}:
        settings["key"] = object_key(values)
    if verb == "remove":
        settings["etag"] = text(values, "etag", limit=100, pattern=r'"[A-Za-z0-9-]+"')
    return settings


def archived(row):
    result = json.loads(row["result"]) if row["result"] else None
    can_verify = (row["status"] == "completed" and row["action"] in READBACK_ACTIONS
                  and bool(result and result.get("id") and not result.get("verification")))
    return {"id": row["id"], "mode": row["mode"], "region": row["region"], "action": row["action"],
            "title": ACTIONS[row["action"]], "target": row["target"], "status": row["status"],
            "submitted": datetime.fromtimestamp(row["submitted"], timezone.utc).isoformat(),
            "finished": row["finished"], "result": result, "can_verify": can_verify, "error": row["error"]}


def target_for(action, settings):
    return settings.get("key") if action in {"s3.upload", "s3.remove"} else (
        settings.get("id") or settings.get("bucket") or settings["name"]
    )


def ec2_image(session, settings):
    ec2 = session.client("ec2", config=READ_CONFIG)
    subnet = ec2.describe_subnets(SubnetIds=[settings["subnet"]])["Subnets"][0]
    group = ec2.describe_security_groups(GroupIds=[settings["security_group"]])["SecurityGroups"][0]
    if subnet["VpcId"] != group["VpcId"] or subnet["State"] != "available":
        invalid("The selected subnet and security group must belong to the same VPC.")
    if settings["key_pair"]:
        ec2.describe_key_pairs(KeyNames=[settings["key_pair"]])
    ssm = session.client("ssm", config=READ_CONFIG)
    parameter = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
    ami = parameter["Parameter"]["Value"]
    image = ec2.describe_images(ImageIds=[ami])["Images"][0]
    device = image["RootDeviceName"]
    root = next((item.get("Ebs", {}) for item in image.get("BlockDeviceMappings", []) if item["DeviceName"] == device), {})
    if settings["disk_gib"] < root.get("VolumeSize", 8):
        invalid("The root disk must be at least as large as the AMI snapshot.")
    return {"ami": ami, "root_device": device}


def connected(scope, account, bucket):
    with storage() as db:
        return bool(db.execute("SELECT 1 FROM connections WHERE mode=? AND account=? AND region=? AND bucket=?",
                               (scope.mode, account, scope.region, bucket)).fetchone())


def change_connection(scope, account, bucket, verb):
    with storage(write=True) as db:
        if verb == "connect":
            db.execute("INSERT OR IGNORE INTO connections VALUES (?, ?, ?, ?)", (scope.mode, account, scope.region, bucket))
        else:
            db.execute("DELETE FROM connections WHERE mode=? AND account=? AND region=? AND bucket=?",
                       (scope.mode, account, scope.region, bucket))


@router.get("/connection")
def get_connection(scope: Scope = Depends()):
    account, principal, _ = connection(scope)
    return {"mode": scope.mode, "region": scope.region, "account": account,
            "identity": principal, "root": principal.endswith(":root"), "regions": REGIONS}


@router.post("/plans")
def prepare(request: Proposal):
    action, scope = request.action, request.scope
    if action not in ACTIONS:
        invalid("Unsupported action.")
    account, principal, session = connection(scope, write=action != "s3.disconnect")
    settings = normalized(action, request.values, account)
    payload = request.values.get("payload", "")
    if not isinstance(payload, str) or len(payload) > 6 * 1024 * 1024:
        invalid("Invalid payload.")
    data = payload_bytes(action, payload)
    proof = hashlib.sha256(data).hexdigest()
    if action == "ec2.create":
        settings.update(safe_read(lambda: ec2_image(session, settings)) if session else
                        {"ami": "ami-demo-al2023", "root_device": "/dev/xvda"})
    if session and action in {"lambda.deploy", "lambda.configure"}:
        client = session.client("lambda", config=READ_CONFIG)
        current = safe_read(lambda: client.get_function_configuration(FunctionName=settings["name"]))
        if current.get("RevisionId"):
            settings["revision"] = current["RevisionId"]
    if session and action in {"s3.connect", "s3.upload", "s3.remove", "s3.delete"}:
        verify_bucket(session, scope.region, settings["bucket"], account)
    plan_id = uuid.uuid4().hex
    target = target_for(action, settings)
    with storage(write=True) as db:
        db.execute("DELETE FROM actions WHERE status='ready' AND submitted < ?", (time.time() - PLAN_TTL,))
        db.execute("""INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, NULL, NULL, NULL)""",
                   (plan_id, scope.mode, scope.region, account, principal, action, target,
                    json.dumps(settings), proof, time.time()))
    return {"id": plan_id, "title": ACTIONS[action], "target": target, "mode": scope.mode,
            "region": scope.region, "account": account, "settings": settings, "size": len(data),
            "requires_confirmation": action in DESTRUCTIVE, "expires_in": PLAN_TTL}


@router.post("/plans/{plan_id}/apply")
def apply(plan_id: str, body: Apply):
    with storage() as db:
        row = db.execute("SELECT * FROM actions WHERE id=?", (plan_id,)).fetchone()
    if not row:
        invalid("Plan not found. Review a new plan.", 404)
    if row["status"] != "ready":
        return archived(row)
    if time.time() - row["submitted"] >= PLAN_TTL:
        invalid("This plan expired. Review the action again.", 409)
    action = row["action"]
    if action in DESTRUCTIVE and body.confirmation != row["target"]:
        invalid("Type the target exactly to confirm.")
    data = payload_bytes(action, body.payload)
    if hashlib.sha256(data).hexdigest() != row["proof"]:
        invalid("The payload changed. Review a new plan.", 409)
    scope = Scope(mode=row["mode"], region=row["region"])
    account, principal, session = connection(scope, write=action != "s3.disconnect")
    if (account, principal) != (row["account"], row["principal"]):
        invalid("AWS identity changed after review. Review a new plan.", 409)
    with storage(write=True) as db:
        changed = db.execute("UPDATE actions SET status='running' WHERE id=? AND status='ready' AND submitted>?",
                             (plan_id, time.time() - PLAN_TTL)).rowcount
        if not changed:
            current = db.execute("SELECT * FROM actions WHERE id=?", (plan_id,)).fetchone()
            if not current or current["status"] == "ready":
                invalid("This plan expired. Review the action again.", 409)
            return archived(current)
    settings = json.loads(row["parameters"])
    status, result, error = "completed", None, None
    try:
        if action in {"s3.connect", "s3.disconnect"}:
            if session and action == "s3.connect":
                verify_bucket(session, scope.region, settings["bucket"], account)
            elif not session and action == "s3.connect":
                with storage() as db:
                    demo_lookup(db, scope.region, "s3", settings["bucket"])
            change_connection(scope, account, settings["bucket"], action.split(".")[1])
            result = {"id": settings["bucket"], "state": "connected" if action == "s3.connect" else "disconnected"}
        else:
            result = live_action(session, scope, account, action, settings, data, plan_id) if session else \
                demo_action(scope, action, settings, data, plan_id)
            if result.get("function_error"):
                status, error = "failed", "Lambda returned a function error."
            if result.get("warning"):
                status, error = "partial", result["warning"]
    except (ClientError, HTTPException) as exc:
        status, error = "failed", aws_error(exc)
    except BotoCoreError as exc:
        status, error = "unverified", f"AWS result could not be confirmed ({type(exc).__name__}). Refresh inventory before retrying."
    with storage(write=True) as db:
        summary = {key: value for key, value in (result or {}).items() if key not in {"output", "log"}}
        db.execute("UPDATE actions SET status=?, finished=?, result=?, error=? WHERE id=?",
                   (status, utc(), json.dumps(summary, default=str) if summary else None, error, plan_id))
        receipt = archived(db.execute("SELECT * FROM actions WHERE id=?", (plan_id,)).fetchone())
        return {**receipt, "result": result}


def verify_bucket(session, region, bucket, account):
    def read():
        client = session.client("s3", config=READ_CONFIG)
        response = client.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
        actual = response.get("BucketRegion") or response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("x-amz-bucket-region")
        if actual and actual != region:
            invalid(f"Bucket is in {actual}. Switch regions and try again.")
        return client
    return safe_read(read)


def lambda_package(data):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lambda_function.py", data)
    return buffer.getvalue()


def live_ec2(client, verb, settings, plan_id, data):
    if verb == "default_vpc":
        result = client.create_default_vpc()["Vpc"]
        return {"id": result["VpcId"], "state": result["State"]}
    if verb == "security_group":
        result = client.create_security_group(GroupName=settings["name"], VpcId=settings["vpc"],
                                              Description="Created by Hermes AWS Manager")
        group = {"id": result["GroupId"], "state": "created with no inbound rules"}
        if settings["ssh_cidr"]:
            try:
                client.authorize_security_group_ingress(GroupId=group["id"], IpPermissions=[{
                    "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                    "IpRanges": [{"CidrIp": settings["ssh_cidr"]}],
                }])
                group["state"] = "created with the requested SSH rule"
            except (ClientError, BotoCoreError) as exc:
                group["state"] = "created"
                group["warning"] = "Security group exists. Its SSH rule could not be confirmed. " + aws_error(exc)
        return group
    if verb == "key_import":
        result = client.import_key_pair(KeyName=settings["name"], PublicKeyMaterial=data)
        return {"id": result["KeyPairId"], "name": result["KeyName"], "state": "imported"}
    if verb == "create":
        options = {
            "ImageId": settings["ami"], "InstanceType": settings["type"], "MinCount": 1, "MaxCount": 1,
            "ClientToken": plan_id,
            "NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": settings["subnet"],
                                   "Groups": [settings["security_group"]], "DeleteOnTermination": True,
                                   "AssociatePublicIpAddress": settings["public_ip"]}],
            "MetadataOptions": {"HttpTokens": "required"},
            "BlockDeviceMappings": [{"DeviceName": settings["root_device"], "Ebs": {
                "VolumeSize": settings["disk_gib"], "VolumeType": "gp3", "Encrypted": True,
                "DeleteOnTermination": True,
            }}],
            "TagSpecifications": [{"ResourceType": "instance", "Tags": [
                {"Key": "Name", "Value": settings["name"]}, {"Key": "ManagedBy", "Value": "HermesAWSManager"},
            ]}],
        }
        if settings["type"].startswith("t3."):
            options["CreditSpecification"] = {"CpuCredits": "standard"}
        if settings["key_pair"]:
            options["KeyName"] = settings["key_pair"]
        if settings["instance_profile"]:
            options["IamInstanceProfile"] = {"Arn": settings["instance_profile"]}
        instance = client.run_instances(**options)["Instances"][0]
        return {"id": instance["InstanceId"], "state": instance["State"]["Name"]}
    instance_id = settings["id"]
    if verb == "resize":
        client.modify_instance_attribute(InstanceId=instance_id, InstanceType={"Value": settings["type"]})
        return {"id": instance_id, "type": settings["type"]}
    if verb == "rename":
        client.create_tags(Resources=[instance_id], Tags=[{"Key": "Name", "Value": settings["name"]}])
        return {"id": instance_id, "name": settings["name"]}
    method, state_key = {
        "start": (client.start_instances, "StartingInstances"), "stop": (client.stop_instances, "StoppingInstances"),
        "reboot": (client.reboot_instances, ""), "terminate": (client.terminate_instances, "TerminatingInstances"),
    }[verb]
    response = method(InstanceIds=[instance_id])
    state = response[state_key][0]["CurrentState"]["Name"] if state_key else "reboot requested"
    return {"id": instance_id, "state": state}


def live_lambda(client, verb, settings, data):
    name = settings["name"]
    if verb == "url_create":
        response = client.create_function_url_config(FunctionName=name, AuthType="AWS_IAM")
        return {"id": name, "url": response["FunctionUrl"], "state": "created"}
    if verb == "url_delete":
        client.delete_function_url_config(FunctionName=name)
        return {"id": name, "state": "URL removed"}
    revision = {"RevisionId": settings["revision"]} if settings.get("revision") else {}
    if verb == "create":
        response = client.create_function(
            FunctionName=name, Runtime=settings["runtime"], Role=settings["role"], Handler="lambda_function.lambda_handler",
            Code={"ZipFile": lambda_package(data)}, MemorySize=settings["memory"], Timeout=settings["timeout"],
            Tags={"ManagedBy": "HermesAWSManager"},
        )
    elif verb == "deploy":
        response = client.update_function_code(FunctionName=name, ZipFile=lambda_package(data), **revision)
    elif verb == "configure":
        response = client.update_function_configuration(
            FunctionName=name, MemorySize=settings["memory"], Timeout=settings["timeout"], **revision,
        )
    elif verb == "delete":
        client.delete_function(FunctionName=name)
        return {"id": name, "state": "deleted"}
    else:
        response = client.invoke(FunctionName=name, InvocationType="RequestResponse", LogType="Tail", Payload=data)
        with closing(response["Payload"]) as stream:
            output = stream.read(32 * 1024 + 1)
        log = base64.b64decode(response.get("LogResult") or "").decode(errors="replace")
        result = {"id": name, "status_code": response["StatusCode"], "function_error": response.get("FunctionError"),
                  "output": output[:32 * 1024].decode(errors="replace"), "truncated": len(output) > 32 * 1024,
                  "log": log, "request_id": response.get("ResponseMetadata", {}).get("RequestId")}
        if result["status_code"] == 200 and not result["function_error"]:
            result["verification"] = {"detail": "Lambda returned HTTP 200", "checked": utc()}
        return result
    return {"id": name, "arn": response["FunctionArn"], "state": response.get("State", "unknown"),
            "update_status": response.get("LastUpdateStatus")}


def live_s3(client, region, account, verb, settings, data):
    bucket = settings["bucket"]
    owner = {"ExpectedBucketOwner": account}
    if verb == "create":
        try:
            client.head_bucket(Bucket=bucket, **owner)
        except ClientError as exc:
            if str(exc.response["Error"]["Code"]) not in {"404", "NoSuchBucket", "NotFound"}:
                raise
        else:
            invalid("A bucket with this name already exists. Connect to it instead.", 409)
        args = {"CreateBucketConfiguration": {"LocationConstraint": region}} if region != "us-east-1" else {}
        client.create_bucket(Bucket=bucket, **args)
        return {"id": bucket, "state": "created", "region": region}
    if verb == "upload":
        client.put_object(Bucket=bucket, Key=settings["key"], Body=data, IfNoneMatch="*", **owner)
        return {"id": bucket, "key": settings["key"], "bytes": len(data), "state": "uploaded"}
    if verb == "remove":
        client.delete_object(Bucket=bucket, Key=settings["key"], IfMatch=settings["etag"], **owner)
        return {"id": bucket, "key": settings["key"], "state": "deleted current object"}
    client.delete_bucket(Bucket=bucket, **owner)
    return {"id": bucket, "state": "deleted"}


def live_action(session, scope, account, action, settings, data, plan_id):
    kind, verb = action.split(".")
    if action == "lambda.role_create":
        return create_lambda_role(session.client("iam", config=WRITE_CONFIG), scope, account, settings)
    client = session.client(kind, config=WRITE_CONFIG)
    if kind == "ec2":
        return live_ec2(client, verb, settings, plan_id, data)
    if kind == "lambda":
        return live_lambda(client, verb, settings, data)
    result = live_s3(client, scope.region, account, verb, settings, data)
    if verb == "delete":
        change_connection(scope, account, settings["bucket"], "disconnect")
    return result


def create_lambda_role(client, scope, account, settings):
    trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    name = settings["name"]
    role = client.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Lambda execution role created by Hermes AWS Manager")["Role"]
    log_group = f"arn:aws:logs:{scope.region}:{account}:log-group:/aws/lambda/{settings['function']}"
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "logs:CreateLogGroup", "Resource": log_group},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": log_group + ":log-stream:*"}]}
    result = {"id": name, "arn": role["Arn"], "state": "created"}
    try:
        client.put_role_policy(RoleName=name, PolicyName="FunctionLogs", PolicyDocument=json.dumps(policy))
    except (ClientError, BotoCoreError) as exc:
        result["warning"] = "Role exists. Its CloudWatch logs policy could not be confirmed. " + aws_error(exc)
    return result


def stamp(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def ec2_row(item):
    return {
        "id": item["InstanceId"], "name": next((tag["Value"] for tag in item.get("Tags", []) if tag["Key"] == "Name"), ""),
        "state": item["State"]["Name"], "type": item["InstanceType"],
        "zone": item.get("Placement", {}).get("AvailabilityZone"), "public_ip": item.get("PublicIpAddress"),
        "private_ip": item.get("PrivateIpAddress"), "subnet": item.get("SubnetId"), "vpc": item.get("VpcId"),
        "ami": item.get("ImageId"), "key_pair": item.get("KeyName"),
        "groups": [group["GroupId"] for group in item.get("SecurityGroups", [])],
        "instance_profile": item.get("IamInstanceProfile", {}).get("Arn"), "created": stamp(item.get("LaunchTime")),
    }


def lambda_row(item):
    return {"id": item["FunctionName"], "name": item["FunctionName"], "arn": item["FunctionArn"],
            "runtime": item.get("Runtime", "container image"), "state": item.get("State", "unknown"),
            "memory": item.get("MemorySize"), "timeout": item.get("Timeout"), "role": item.get("Role"),
            "updated": item.get("LastModified"), "update_status": item.get("LastUpdateStatus")}


def live_inventory(session, scope, kind, cursor):
    client = session.client(kind, config=READ_CONFIG)
    if kind == "ec2":
        response = client.describe_instances(MaxResults=50, **({"NextToken": cursor} if cursor else {}))
        rows = [ec2_row(item) for group in response["Reservations"] for item in group["Instances"]
                if item["State"]["Name"] != "terminated"]
        return rows, response.get("NextToken")
    if kind == "lambda":
        response = client.list_functions(MaxItems=50, **({"Marker": cursor} if cursor else {}))
        return [lambda_row(item) for item in response["Functions"]], response.get("NextMarker")
    response = client.list_buckets(BucketRegion=scope.region, MaxBuckets=50, **({"ContinuationToken": cursor} if cursor else {}))
    return [{"id": item["Name"], "name": item["Name"], "region": item.get("BucketRegion", scope.region),
             "created": stamp(item.get("CreationDate"))} for item in response["Buckets"]], response.get("ContinuationToken")


@router.get("/inventory/{kind}")
def inventory(kind: str, cursor: str = "", scope: Scope = Depends()):
    if kind not in KINDS:
        invalid("Unknown resource type.")
    account, _, session = connection(scope)
    warning = None
    if session:
        try:
            rows, next_cursor = live_inventory(session, scope, kind, cursor)
        except (BotoCoreError, ClientError) as exc:
            if kind != "s3":
                invalid(aws_error(exc), 502)
            rows, next_cursor = [], None
            warning = "Bucket inventory could not be read. Saved connections are still available. " + aws_error(exc)
    else:
        with storage() as db:
            rows = [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM demo_resources WHERE region=? AND kind=? ORDER BY id", (scope.region, kind))]
        next_cursor = None
    if kind == "s3":
        with storage() as db:
            saved = {row[0] for row in db.execute("SELECT bucket FROM connections WHERE mode=? AND account=? AND region=?",
                                                  (scope.mode, account, scope.region))}
        for row in rows:
            row["connected"] = row["id"] in saved
            saved.discard(row["id"])
        rows.extend({"id": bucket, "name": bucket, "region": scope.region, "connected": True}
                    for bucket in sorted(saved))
    return {"items": rows, "cursor": next_cursor, "warning": warning}


@router.get("/network")
def network(scope: Scope = Depends()):
    _, _, session = connection(scope)
    if not session:
        with storage() as db:
            return {
                "subnets": demo_list(db, scope.region, "subnet"),
                "groups": demo_list(db, scope.region, "group"),
                "key_pairs": [item["id"] for item in demo_list(db, scope.region, "key")],
                "vpcs": demo_list(db, scope.region, "vpc"),
            }
    client = session.client("ec2", config=READ_CONFIG)
    def read():
        subnets = client.describe_subnets(Filters=[{"Name": "state", "Values": ["available"]}], MaxResults=100)["Subnets"]
        groups = client.describe_security_groups(MaxResults=100)["SecurityGroups"]
        try:
            keys = client.describe_key_pairs()["KeyPairs"]
        except ClientError:
            keys = []
        return {"subnets": [{"id": item["SubnetId"], "vpc": item["VpcId"], "zone": item["AvailabilityZone"],
                             "name": next((tag["Value"] for tag in item.get("Tags", []) if tag["Key"] == "Name"), "")}
                            for item in subnets],
                "groups": [{"id": item["GroupId"], "vpc": item["VpcId"], "name": item["GroupName"]} for item in groups],
                "key_pairs": [item["KeyName"] for item in keys],
                "vpcs": [{"id": vpc, "name": vpc} for vpc in sorted({item["VpcId"] for item in subnets + groups})]}
    return safe_read(read)


@router.get("/lambda/roles")
def lambda_roles(scope: Scope = Depends()):
    _, _, session = connection(scope)
    if not session:
        with storage() as db:
            return {"roles": demo_list(db, scope.region, "role")}
    client = session.client("iam", config=READ_CONFIG)
    roles = safe_read(lambda: client.list_roles(MaxItems=100))["Roles"]
    return {"roles": [{"arn": role["Arn"], "name": role["RoleName"]} for role in roles
                      if any("lambda.amazonaws.com" in (
                          stmt.get("Principal", {}).get("Service", []) if isinstance(stmt.get("Principal"), dict) else []
                      ) for stmt in role.get("AssumeRolePolicyDocument", {}).get("Statement", []))]}


@router.get("/lambda/detail/{name}")
def lambda_detail(name: str, scope: Scope = Depends()):
    text({"name": name}, "name", pattern=NAME)
    _, _, session = connection(scope)
    if not session:
        with storage() as db:
            return demo_lookup(db, scope.region, "lambda", name)
    client = session.client("lambda", config=READ_CONFIG)
    row = lambda_row(safe_read(lambda: client.get_function_configuration(FunctionName=name)))
    try:
        url = client.get_function_url_config(FunctionName=name)
        row.update(url=url["FunctionUrl"], url_auth=url["AuthType"])
    except client.exceptions.ResourceNotFoundException:
        pass
    except (BotoCoreError, ClientError):
        row["url_status"] = "unavailable"
    return row


@router.get("/lambda/logs/{name}")
def lambda_logs(name: str, scope: Scope = Depends()):
    text({"name": name}, "name", pattern=NAME)
    _, _, session = connection(scope)
    if not session:
        return {"events": [{"at": utc(), "message": "Demo workspace: function invocation output appears in the activity log."}]}
    client = session.client("logs", config=READ_CONFIG)
    try:
        response = client.filter_log_events(logGroupName=f"/aws/lambda/{name}", limit=50,
                                            startTime=int((time.time() - 3600) * 1000))
    except client.exceptions.ResourceNotFoundException:
        return {"events": []}
    except (BotoCoreError, ClientError) as exc:
        invalid(aws_error(exc), 502)
    return {"events": [{"at": datetime.fromtimestamp(item["timestamp"] / 1000, timezone.utc).isoformat(),
                        "message": item["message"][:4000]} for item in response["events"]]}


@router.get("/s3/objects/{bucket}")
def objects(bucket: str, prefix: str = "", cursor: str = "", scope: Scope = Depends()):
    bucket_name(bucket)
    if len(prefix.encode()) > 1024:
        invalid("Prefix is too long.")
    account, _, session = connection(scope)
    if not session:
        with storage() as db:
            demo_lookup(db, scope.region, "s3", bucket)
            rows = db.execute("SELECT key, body, modified FROM demo_objects WHERE region=? AND bucket=? ORDER BY key",
                              (scope.region, bucket)).fetchall()
        return {"items": [{"key": row["key"], "size": len(row["body"]), "modified": row["modified"],
                           "etag": '"' + hashlib.md5(row["body"], usedforsecurity=False).hexdigest() + '"'}
                          for row in rows if row["key"].startswith(prefix)], "cursor": None}
    client = verify_bucket(session, scope.region, bucket, account)
    response = safe_read(lambda: client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=50,
                         ExpectedBucketOwner=account, **({"ContinuationToken": cursor} if cursor else {})))
    return {"items": [{"key": row["Key"], "size": row["Size"], "modified": stamp(row["LastModified"]), "etag": row["ETag"]}
                      for row in response.get("Contents", [])], "cursor": response.get("NextContinuationToken")}


def read_object(scope, account, session, bucket, key):
    if session:
        client = verify_bucket(session, scope.region, bucket, account)
        def fetch():
            obj = client.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
            with closing(obj["Body"]) as stream:
                if obj.get("ContentLength", 0) > MAX_FILE:
                    invalid("In-dashboard downloads are limited to 4 MiB.")
                return stream.read(MAX_FILE + 1)
        data = safe_read(fetch)
    else:
        with storage() as db:
            row = db.execute("SELECT body FROM demo_objects WHERE region=? AND bucket=? AND key=?",
                             (scope.region, bucket, key)).fetchone()
        if row is None:
            invalid("Object not found.", 404)
        data = row["body"]
    if len(data) > MAX_FILE:
        invalid("In-dashboard downloads are limited to 4 MiB.")
    return data


@router.get("/s3/download/{bucket}")
def download(bucket: str, key: str, scope: Scope = Depends()):
    bucket_name(bucket)
    object_key({"key": key})
    account, _, session = connection(scope)
    data = read_object(scope, account, session, bucket, key)
    with storage() as db:
        upload = db.execute("""SELECT proof FROM actions WHERE mode=? AND account=? AND region=?
                            AND action='s3.upload' AND target=? AND status='completed'
                            AND json_extract(parameters, '$.bucket')=?
                            ORDER BY submitted DESC LIMIT 1""", (scope.mode, account, scope.region, key, bucket)).fetchone()
    digest = hashlib.sha256(data).hexdigest()
    return {"name": key.rsplit("/", 1)[-1] or "object", "content": base64.b64encode(data).decode(),
            "sha256": digest, "matches_upload": digest == upload["proof"] if upload else None}


@router.post("/activity/{receipt_id}/verify")
def verify_receipt(receipt_id: str, scope: Scope = Depends()):
    account, _, session = connection(scope)
    with storage() as db:
        row = db.execute("SELECT * FROM actions WHERE id=? AND mode=? AND account=? AND region=?",
                         (receipt_id, scope.mode, account, scope.region)).fetchone()
    if not row:
        invalid("Activity entry not found in this workspace.", 404)
    receipt = archived(row)
    if not receipt["can_verify"]:
        return receipt

    result = receipt["result"]
    if row["action"] == "ec2.create":
        instance_id = result["id"]
        if session:
            client = session.client("ec2", config=READ_CONFIG)
            response = safe_read(lambda: client.describe_instances(InstanceIds=[instance_id]))
            state = next((item["State"]["Name"] for group in response["Reservations"]
                          for item in group["Instances"] if item["InstanceId"] == instance_id), "unavailable")
        else:
            with storage() as db:
                state = demo_lookup(db, scope.region, "ec2", instance_id)["state"]
        if state != "running":
            invalid(f"Instance state is {state}. Check again when it is running.", 409)
        detail = f"{instance_id} is running"
    else:
        settings = json.loads(row["parameters"])
        data = read_object(scope, account, session, settings["bucket"], settings["key"])
        if hashlib.sha256(data).hexdigest() != row["proof"]:
            invalid("The current object differs from the reviewed upload.", 409)
        detail = "SHA-256 matches the reviewed upload"

    result["verification"] = {"detail": detail, "checked": utc()}
    with storage(write=True) as db:
        db.execute("UPDATE actions SET result=? WHERE id=? AND result=?", (json.dumps(result), receipt_id, row["result"]))
        return archived(db.execute("SELECT * FROM actions WHERE id=?", (receipt_id,)).fetchone())


@router.get("/activity")
def activity(scope: Scope = Depends()):
    account, _, _ = connection(scope)
    with storage() as db:
        rows = db.execute("""SELECT * FROM actions WHERE mode=? AND account=? AND region=? AND status!='ready'
                          ORDER BY submitted DESC LIMIT 50""", (scope.mode, account, scope.region)).fetchall()
    items = []
    for row in rows:
        receipt = archived(row)
        if receipt["result"]:
            receipt["result"].pop("output", None)
            receipt["result"].pop("log", None)
        items.append(receipt)
    return {"items": items}


def demo_save(db, region, kind, data):
    db.execute("INSERT OR REPLACE INTO demo_resources VALUES (?, ?, ?, ?)", (region, kind, data["id"], json.dumps(data)))


def demo_lookup(db, region, kind, name):
    row = db.execute("SELECT data FROM demo_resources WHERE region=? AND kind=? AND id=?", (region, kind, name)).fetchone()
    if not row:
        invalid("Resource not found in this region.", 404)
    return json.loads(row[0])


def demo_list(db, region, kind):
    return [json.loads(row[0]) for row in db.execute(
        "SELECT data FROM demo_resources WHERE region=? AND kind=? ORDER BY id", (region, kind))]


def seed(region):
    with storage(write=True) as db:
        if not db.execute("INSERT OR IGNORE INTO demo_regions VALUES (?)", (region,)).rowcount:
            return
        demo_save(db, region, "vpc", {"id": "vpc-12345678", "name": "sandbox-vpc"})
        for index, zone in enumerate(("a", "b")):
            demo_save(db, region, "subnet", {"id": f"subnet-1234567{8 + index}", "vpc": "vpc-12345678",
                      "zone": region + zone, "name": f"app-private-{zone}"})
        demo_save(db, region, "group", {"id": "sg-12345678", "vpc": "vpc-12345678", "name": "app-private"})
        demo_save(db, region, "key", {"id": "sandbox-key"})
        demo_save(db, region, "role", {"id": "sandbox-lambda", "name": "sandbox-lambda",
                  "arn": f"arn:aws:iam::{DEMO_ACCOUNT}:role/sandbox-lambda"})
        for index, name, size, state in [(1, "web-worker", "t3.small", "running"), (2, "batch-runner", "t3.micro", "stopped")]:
            demo_save(db, region, "ec2", {"id": f"i-0abc000000000000{index}", "name": name, "type": size,
                      "state": state, "zone": region + "a", "subnet": "subnet-12345678", "vpc": "vpc-12345678",
                      "groups": ["sg-12345678"], "private_ip": f"10.0.1.{20 + index}", "created": utc()})
        demo_save(db, region, "lambda", {"id": "process-event", "name": "process-event", "state": "Active",
                  "runtime": "python3.13", "memory": 256, "timeout": 15,
                  "role": f"arn:aws:iam::{DEMO_ACCOUNT}:role/sandbox-lambda", "updated": utc()})
        bucket = "hermes-demo-artifacts"
        demo_save(db, region, "s3", {"id": bucket, "name": bucket, "region": region, "created": utc()})
        db.execute("INSERT INTO demo_objects VALUES (?, ?, ?, ?, ?)",
                   (region, bucket, "reports/example.json", b'{"workspace":"demo"}\n', utc()))


def demo_action(scope, action, settings, data, plan_id):
    region = scope.region
    kind, verb = action.split(".")
    name = settings.get("bucket") or settings.get("id") or settings["name"]
    with storage(write=True) as db:
        if verb == "default_vpc":
            if any(row.get("default") for row in demo_list(db, region, "vpc")):
                invalid("This region already has a default VPC.", 409)
            vpc = "vpc-" + plan_id[:17]
            demo_save(db, region, "vpc", {"id": vpc, "name": "Default VPC", "default": True})
            demo_save(db, region, "subnet", {"id": "subnet-" + plan_id[:17], "vpc": vpc,
                      "zone": region + "a", "name": "default"})
            demo_save(db, region, "group", {"id": "sg-" + plan_id[:17], "vpc": vpc, "name": "default"})
            return {"id": vpc, "state": "available"}
        if verb == "security_group":
            demo_lookup(db, region, "vpc", settings["vpc"])
            group = "sg-" + plan_id[:17]
            demo_save(db, region, "group", {"id": group, "vpc": settings["vpc"], "name": name})
            return {"id": group, "state": "created with the requested SSH rule" if settings["ssh_cidr"] else "created with no inbound rules"}
        if verb in {"key_import", "role_create"}:
            resource_type = "key" if verb == "key_import" else "role"
            if any(row["id"] == name for row in demo_list(db, region, resource_type)):
                invalid("That name already exists.", 409)
            resource = {"id": name, "name": name}
            if resource_type == "role":
                resource["arn"] = f"arn:aws:iam::{DEMO_ACCOUNT}:role/{name}"
            demo_save(db, region, resource_type, resource)
            return {**resource, "state": "created"}
        if verb == "create":
            if kind == "ec2":
                subnet = demo_lookup(db, region, "subnet", settings["subnet"])
                group = demo_lookup(db, region, "group", settings["security_group"])
                if subnet["vpc"] != group["vpc"]:
                    invalid("The selected subnet and security group must belong to the same VPC.")
                name = "i-" + plan_id[:17]
                resource = {"id": name, "name": settings["name"], "type": settings["type"], "state": "running",
                            "zone": subnet["zone"], "subnet": settings["subnet"], "vpc": subnet["vpc"],
                            "groups": [settings["security_group"]],
                            "key_pair": settings["key_pair"], "instance_profile": settings["instance_profile"],
                            "private_ip": "10.0.1.84", "public_ip": "192.0.2.84" if settings["public_ip"] else None,
                            "ami": settings["ami"], "created": utc()}
            elif kind == "lambda":
                resource = {"id": name, "name": name, "state": "Active", "updated": utc(),
                            **{key: settings[key] for key in ("runtime", "memory", "timeout", "role")}}
            else:
                resource = {"id": name, "name": name, "region": region, "created": utc()}
            if db.execute("SELECT 1 FROM demo_resources WHERE region=? AND kind=? AND id=?", (region, kind, name)).fetchone():
                invalid("Resource already exists.", 409)
            demo_save(db, region, kind, resource)
            return {"id": name, "state": resource.get("state", "created")}
        resource = demo_lookup(db, region, kind, name)
        if verb in {"terminate", "delete"}:
            if kind == "s3":
                if db.execute("SELECT 1 FROM demo_objects WHERE region=? AND bucket=?", (region, name)).fetchone():
                    invalid("The bucket must be empty.", 409)
                db.execute("DELETE FROM connections WHERE mode='demo' AND account=? AND region=? AND bucket=?",
                           (DEMO_ACCOUNT, region, name))
            db.execute("DELETE FROM demo_resources WHERE region=? AND kind=? AND id=?", (region, kind, name))
            return {"id": name, "state": "deleted"}
        if kind == "ec2":
            if verb == "resize":
                if resource["state"] != "stopped":
                    invalid("Stop the instance before changing its type.", 409)
                resource["type"] = settings["type"]
            if verb == "rename":
                resource["name"] = settings["name"]
            if verb in {"start", "stop"}:
                resource["state"] = "running" if verb == "start" else "stopped"
            if verb == "reboot" and resource["state"] != "running":
                invalid("Start the instance before rebooting.", 409)
        elif kind == "lambda":
            if verb == "invoke":
                return {"id": name, "status_code": 200, "output": json.dumps({"simulated": True, "event": json.loads(data)}),
                        "log": "Demo workspace: invocation simulated."}
            if verb == "configure":
                resource.update(memory=settings["memory"], timeout=settings["timeout"])
            if verb == "url_create":
                if resource.get("url"):
                    invalid("This function already has a URL.", 409)
                resource.update(url=f"https://{plan_id[:12]}.lambda-url.{region}.on.aws/", url_auth="AWS_IAM")
            if verb == "url_delete":
                resource.pop("url", None)
                resource.pop("url_auth", None)
            resource["updated"] = utc()
        elif verb == "upload":
            changed = db.execute("INSERT OR IGNORE INTO demo_objects VALUES (?, ?, ?, ?, ?)",
                                 (region, name, settings["key"], data, utc())).rowcount
            if not changed:
                invalid("An object already exists at that key.", 409)
            return {"id": name, "key": settings["key"], "bytes": len(data), "state": "uploaded"}
        elif verb == "remove":
            row = db.execute("SELECT body FROM demo_objects WHERE region=? AND bucket=? AND key=?",
                             (region, name, settings["key"])).fetchone()
            if row is None:
                invalid("Object not found.", 404)
            etag = '"' + hashlib.md5(row["body"], usedforsecurity=False).hexdigest() + '"'
            if etag != settings["etag"]:
                invalid("Object changed after it was listed.", 409)
            db.execute("DELETE FROM demo_objects WHERE region=? AND bucket=? AND key=?", (region, name, settings["key"]))
            return {"id": name, "key": settings["key"], "state": "deleted current object"}
        demo_save(db, region, kind, resource)
        return {"id": name, "state": resource.get("state", "updated")}
