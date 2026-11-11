"""AWS Manager service catalog, reviewed deployments, and inventory."""
import hashlib
import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from boto3.dynamodb.types import TypeSerializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException

READ = Config(connect_timeout=4, read_timeout=12, retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True)
WRITE = Config(connect_timeout=4, read_timeout=28, retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True)
TAG = "HermesAWSManager"
PREFIX = "hermes-aws-"
NAME = r"[a-z][a-z0-9-]{0,39}"
FUNCTION = r"[A-Za-z0-9_-]{1,64}"
TABLE = r"[A-Za-z0-9_.-]{3,255}"
VPC = r"vpc-[a-f0-9]{8}(?:[a-f0-9]{9})?"
SUBNET = r"subnet-[a-f0-9]{8}(?:[a-f0-9]{9})?"
GROUP = r"sg-[a-f0-9]{8}(?:[a-f0-9]{9})?"
INSTANCE = r"i-[a-f0-9]{8}(?:[a-f0-9]{9})?"
PATH = r"/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*/?)?"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
STACK = r"arn:aws:cloudformation:[a-z0-9-]+:[0-9]{12}:stack/[A-Za-z0-9-]+/[a-f0-9-]+"
LOAD_BALANCER = r"arn:aws:elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:loadbalancer/app/[a-zA-Z0-9-]+/[a-f0-9]+"
DOMAIN = r"(?=.{1,253}\.?$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.?"



def field(key, label, *, pattern=None, default="", options=(), low=None, high=None,
          optional=False, hint="", limit=255, payload=False):
    return dict(key=key, label=label, pattern=pattern, default=default, options=options,
                low=low, high=high, optional=optional, hint=hint, limit=limit, payload=payload)


def operation(group, title, description, *fields, deploy=False, read=False, destructive=False, payload=None):
    return dict(group=group, title=title, description=description, fields=fields, deploy=deploy,
                read=read, destructive=destructive, payload=payload)


def payload_field(label, default="", optional=False):
    return field("payload", label, default=default, optional=optional, payload=True)


def f_name(label="Name", pattern=NAME):
    return field("name", label, pattern=pattern)


def f_function(optional=False):
    return field("function", "Lambda function name", pattern=FUNCTION, optional=optional)


def f_port(default):
    return field("port", "Port", default=default, low=1, high=65535)


def f_subnets():
    return (
        field("subnet_a", "First subnet ID", pattern=SUBNET),
        field("subnet_b", "Second subnet ID", pattern=SUBNET, hint="Choose a subnet in a different Availability Zone."),
    )


def f_group():
    return field("group", "Security group ID", pattern=GROUP)


def f_method(default="GET"):
    return field("method", "HTTP method", default=default, options=METHODS)


def f_path():
    return field("path", "Route path", pattern=PATH, default="/items", hint="Use a fixed path, such as /items.")


ACTIONS = {
    "dynamodb.create": operation("databases", "Create DynamoDB table",
        "Create an on-demand table with one string partition key.", f_name("Table name", TABLE),
        field("partition_key", "Partition key", default="id", pattern=r"[A-Za-z_][A-Za-z0-9_]{0,63}"), deploy=True),
    "dynamodb.put": operation("databases", "Write new item",
        "Write one JSON object. The request fails if this key already exists.",
        field("id", "Table", pattern=TABLE), payload_field("Item JSON", '{"id":"sample","status":"ready"}'), payload="object"),
    "dynamodb.get": operation("databases", "Read item", "Read an item by its string partition key.",
        field("id", "Table", pattern=TABLE), field("item_key", "Partition key value", limit=512), read=True),
    "dynamodb.remove": operation("databases", "Delete item", "Permanently remove the item with this key.",
        field("id", "Table", pattern=TABLE), field("item_key", "Partition key value", limit=512), destructive=True),
    "dynamodb.role": operation("databases", "Create table access role",
        "Create a Lambda execution role with read and write permissions on this table and logging permissions for the named function.",
        field("id", "Table", pattern=TABLE), f_name("Role label"), f_function(), deploy=True),
    "rds.create": operation("databases", "Create RDS database",
        "Create an encrypted private database and store its master password in Secrets Manager. Stack deletion takes a final snapshot.",
        f_name("Database identifier", r"[a-z](?:[a-z0-9]|-(?=[a-z0-9])){0,39}"),
        field("engine", "Database engine", default="postgres", options=("postgres", "mysql")),
        field("instance_class", "Instance class", default="db.t4g.micro", options=("db.t4g.micro", "db.t4g.small", "db.t4g.medium")),
        field("storage", "Storage (GiB)", default=20, low=20, high=200),
        field("username", "Master username", pattern=r"[A-Za-z][A-Za-z0-9]{0,15}", default="dbadmin"),
        *f_subnets(), f_group(), deploy=True),
    "api.create": operation("apis", "Create HTTP API",
        "Create an IAM-authenticated HTTPS endpoint and attach the first Lambda route.", f_name("API name"),
        f_method(), f_path(), f_function(), deploy=True),
    "api.route": operation("apis", "Add Lambda route",
        "Add an IAM-authenticated route to an HTTP API with an auto-deploying default stage.",
        field("id", "API ID", pattern=r"[a-z0-9]{6,20}"), f_name("Route deployment name"),
        f_method(), f_path(), f_function(), deploy=True),
    "api.test": operation("apis", "Test HTTP route",
        "Send a signed HTTPS request with your current AWS identity. The function can make changes using its execution role.",
        field("id", "API ID", pattern=r"[a-z0-9]{6,20}"), f_method(), f_path(),
        payload_field("Request JSON (optional)", optional=True), payload="optional"),
    "vpc.create": operation("networking", "Create VPC",
        "Create a VPC with DNS support. Add subnets as separate deployments.", f_name("VPC name"),
        field("cidr", "IPv4 CIDR", default="10.30.0.0/16"), deploy=True),
    "vpc.subnet": operation("networking", "Create subnet",
        "Create a subnet with automatic public IPv4 assignment disabled.", f_name("Subnet name"),
        field("vpc", "VPC ID", pattern=VPC), field("cidr", "IPv4 CIDR", default="10.30.1.0/24"),
        field("zone", "Availability Zone", hint="For example, us-east-1a in us-east-1."), deploy=True),
    "alb.create": operation("networking", "Create load balancer",
        "Create an application load balancer with one HTTP listener and one existing EC2 target. The selected security group controls access.",
        f_name("Load balancer label"), *f_subnets(), f_group(), field("instance", "EC2 instance ID", pattern=INSTANCE),
        f_port(80), field("scheme", "Visibility", default="internal", options=("internal", "internet-facing")), deploy=True),
    "dns.zone": operation("networking", "Create public hosted zone",
        "Create a public Route 53 zone. Use its name servers at your domain registrar to delegate the domain.",
        f_name("Domain name", DOMAIN), deploy=True),
    "dns.alias": operation("networking", "Create DNS alias",
        "Create an IPv4 alias in this public zone for a CloudFront distribution or internet-facing application load balancer.",
        field("id", "Hosted zone ID", pattern=r"[A-Z0-9]{1,32}"), f_name("Record name", DOMAIN),
        field("target_type", "Target service", options=("CloudFront", "Application load balancer"), default="CloudFront"),
        field("destination", "Distribution ID or load balancer ARN", limit=512), deploy=True),
    "ecr.create": operation("containers", "Create ECR repository",
        "Create a private image repository with immutable tags and scanning on push.", f_name("Repository name"), deploy=True),
    "ecr.remove": operation("containers", "Delete image",
        "Delete this digest and its tags from the repository. Services may need this image to start replacement tasks.",
        field("id", "Image digest", pattern=r"sha256:[a-f0-9]{64}"),
        field("repository", "Repository", pattern=r"[a-z0-9][a-z0-9._/-]{1,255}"), destructive=True),
    "ecs.role": operation("containers", "Create task execution role",
        "Create an ECS task execution role using the AWS managed policy for pulling images and writing logs.",
        f_name("Role label"), deploy=True),
    "ecs.deploy": operation("containers", "Deploy Fargate service",
        "Create an ECS cluster and Fargate service for a tagged or digest-pinned private ECR image. Private subnets need NAT or ECR endpoints to pull.",
        f_name("Service name"), field("image", "Private ECR image URI", limit=512),
        field("role", "Task execution role ARN"), field("subnet_a", "Subnet ID", pattern=SUBNET), f_group(),
        f_port(8080), field("count", "Desired tasks", default=1, low=1, high=5),
        field("public_ip", "Public IPv4", default="DISABLED", options=("DISABLED", "ENABLED")), deploy=True),
    "ecs.scale": operation("containers", "Set desired tasks", "Change how many tasks ECS should run for this service.",
        field("id", "Service ARN", limit=512), field("cluster", "Cluster ARN", limit=512),
        field("count", "Desired tasks", default=1, low=0, high=5)),
    "events.schedule": operation("automation", "Schedule Lambda",
        "Create an EventBridge rule with permission scoped to this Lambda and rule.", f_name("Rule name"), f_function(),
        field("expression", "Schedule expression", default="rate(15 minutes)",
              pattern=r"(?:rate|cron)\([A-Za-z0-9 *,/?#L-]{1,96}\)"), deploy=True),
    "sqs.create": operation("automation", "Create queue",
        "Create an encrypted standard queue. If you select a Lambda, its execution role needs permission to receive and delete messages from this queue.",
        f_name("Queue name"), f_function(optional=True),
        field("visibility", "Visibility timeout (seconds)", default=60, low=30, high=43200), deploy=True),
    "sqs.role": operation("automation", "Create queue access role",
        "Create a Lambda execution role with consumer permissions on this queue and logging permissions for the named function.",
        field("id", "Queue name", pattern=r"[A-Za-z0-9_-]{1,80}"), f_name("Role label"), f_function(), deploy=True),
    "sqs.trigger": operation("automation", "Connect Lambda consumer",
        "Connect this existing queue to a Lambda function. The function's execution role needs permission to consume messages.",
        field("id", "Queue name", pattern=r"[A-Za-z0-9_-]{1,80}"), f_name("Consumer deployment name"), f_function(), deploy=True),
    "sqs.send": operation("automation", "Send message", "Send a message to this queue. An attached consumer can process it immediately.",
        field("id", "Queue name", pattern=r"[A-Za-z0-9_-]{1,80}"), payload_field("Message body", '{"task":"example"}'), payload="text"),
    "cdn.create": operation("delivery", "Create S3 distribution",
        "Create a private S3 bucket and a CloudFront distribution. Upload index.html in S3 after provisioning. The distribution serves files publicly over HTTPS.",
        f_name("Bucket name", r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"),
        field("domain", "Custom domain (optional)", pattern=DOMAIN, optional=True),
        field("certificate", "ACM certificate ARN (optional)", optional=True,
              hint="For a custom domain, use an issued certificate in us-east-1."), deploy=True),
    "monitoring.metrics": operation("monitoring", "Read CloudWatch metrics",
        "Read EC2 CPU or Lambda invocations, errors, and duration for the selected period.",
        field("resource", "Resource type", default="Lambda", options=("Lambda", "EC2")),
        field("id", "Function name or instance ID"), field("hours", "Lookback (hours)", default=3, low=1, high=24), read=True),
    "monitoring.logs": operation("monitoring", "Read log events",
        "Read recent events from this CloudWatch log group. Results appear in the dashboard without being stored in Activity.",
        field("id", "Log group", pattern=r"[A-Za-z0-9_./#-]{1,512}", limit=512), read=True),
    "cost.report": operation("cost", "Read cost report",
        "Query unblended AWS spend by service. Cost Explorer data can lag usage, and AWS charges for API queries.",
        field("days", "Lookback (days)", default=7, options=("7", "30", "90")), read=True),
    "deployment.delete": operation("", "Delete deployment",
        "Delete the stack and the resources it owns. RDS takes a final snapshot. Empty an S3 origin before deleting its deployment.",
        field("id", "Stack ARN", pattern=STACK, limit=512), destructive=True),
}


def view(label, *actions):
    return dict(label=label, actions=actions)


GROUPS = {
    "databases": ("Databases", ("dynamodb.create", "rds.create"), {
        "dynamodb": view("DynamoDB tables", "dynamodb.put", "dynamodb.get", "dynamodb.remove", "dynamodb.role"),
        "rds": view("RDS databases")}),
    "apis": ("APIs", ("api.create",), {"apis": view("HTTP APIs", "api.route", "api.test"), "routes": view("Routes")}),
    "monitoring": ("Monitoring", ("monitoring.metrics",), {
        "alarms": view("CloudWatch alarms"), "logs": view("Log groups", "monitoring.logs")}),
    "networking": ("Networking", ("vpc.create", "vpc.subnet", "alb.create", "dns.zone"), {
        "vpc": view("VPCs"), "subnet": view("Subnets"), "groups": view("Security groups"), "alb": view("Load balancers"),
        "zones": view("Hosted zones", "dns.alias"), "records": view("DNS records")}),
    "containers": ("Containers", ("ecr.create", "ecs.role", "ecs.deploy"), {
        "ecr": view("ECR repositories"), "images": view("ECR images", "ecr.remove"), "clusters": view("ECS clusters"),
        "tasks": view("ECS services", "ecs.scale")}),
    "automation": ("Automation", ("events.schedule", "sqs.create"), {
        "rules": view("EventBridge rules"), "queues": view("SQS queues", "sqs.send", "sqs.role", "sqs.trigger")}),
    "delivery": ("CDN", ("cdn.create",), {"distributions": view("CloudFront distributions")}),
    "cost": ("Costs", ("cost.report",), {}),
}
SUBVIEWS = {"routes": ("apis", "HTTP API"), "records": ("zones", "Hosted zone"), "images": ("ecr", "Repository"), "tasks": ("clusters", "Cluster")}


def catalog(group):
    if group not in GROUPS:
        fail("Unknown service group.", 404)
    label, actions, views = GROUPS[group]
    views = {**views}
    if any(spec["deploy"] and spec["group"] == group for spec in ACTIONS.values()):
        views["deployments"] = view("Deployments", "deployment.delete")
    return {
        "label": label,
        "actions": [action_spec(action) for action in actions],
        "views": [{"id": key, "label": spec["label"], "actions": [action_spec(action) for action in spec["actions"]],
                   "parent": SUBVIEWS.get(key)} for key, spec in views.items()],
    }


def action_spec(action):
    spec = ACTIONS[action]
    return {"id": action, "title": spec["title"], "description": spec["description"], "read": spec["read"],
            "fields": [{key: value for key, value in item.items() if key != "pattern"} for item in spec["fields"]]}


def fail(message, code=400):
    raise HTTPException(code, message)


def iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def error_code(error):
    return error.response.get("Error", {}).get("Code")


def client(session, service, *, write=False):
    return session.client(service, config=WRITE if write else READ)


def json_value(body):
    def invalid_constant(value):
        raise ValueError(value)
    return json.loads(body, parse_float=Decimal, parse_constant=invalid_constant)


def data_bytes(action, payload):
    payload_type = ACTIONS[action]["payload"]
    if not payload_type:
        if payload:
            fail("This action does not accept a payload.")
        return b""
    if len(payload.encode()) > 64 * 1024 or not payload and payload_type != "optional":
        fail("Provide a payload of up to 64 KiB.")
    if payload and payload_type != "text":
        try:
            value = json_value(payload)
        except (ValueError, RecursionError):
            fail("Enter valid JSON.")
        if payload_type == "object" and not isinstance(value, dict):
            fail("Enter a JSON object.")
    return payload.encode()


def normalize(action, values, account, region):
    spec = ACTIONS[action]
    settings = {}
    for item in spec["fields"]:
        key = item["key"]
        if item["payload"]:
            continue
        value = values.get(key, item["default"])
        label = item["label"]
        if item["options"]:
            if str(value) not in item["options"]:
                fail(f"Select a valid {label.lower()}.")
            value = str(value)
        elif item["low"] is not None:
            if type(value) is not int or not item["low"] <= value <= item["high"]:
                fail(f"{label} must be between {item['low']} and {item['high']}.")
        elif not isinstance(value, str) or len(value) > item["limit"] or any(ord(c) < 32 for c in value) or (
                not value and not item["optional"]) or (value and item["pattern"] and not re.fullmatch(item["pattern"], value)):
            fail(f"Enter a valid {label.lower()}.")
        settings[key] = value

    if "cidr" in settings:
        try:
            network = ipaddress.IPv4Network(settings["cidr"])
            if not 16 <= network.prefixlen <= 28:
                raise ValueError
        except ValueError:
            fail("Enter a canonical IPv4 CIDR between /16 and /28.")
    if action == "vpc.subnet" and not re.fullmatch(region + r"[a-z]", settings["zone"]):
        fail("Choose an Availability Zone in the selected region.")
    if action in {"api.create", "api.route", "api.test"} and (
            "//" in settings["path"] or any(segment in {".", ".."} for segment in settings["path"].split("/"))):
        fail("Enter a fixed route path without relative segments.")
    if action == "ecs.deploy":
        if not re.fullmatch(rf"{account}\.dkr\.ecr\.{region}\.amazonaws\.com/"
                            r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[a-f0-9]{64})",
                            settings["image"]):
            fail("Enter a tagged or digest-pinned private ECR image in this account and region.")
        if not re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9_+=,.@/\-]+", settings["role"]):
            fail("Enter an IAM execution role ARN in this account.")
    if action == "ecs.scale":
        for key, resource in (("cluster", "cluster"), ("id", "service")):
            pattern = rf"arn:aws:ecs:{region}:{account}:{resource}/[a-zA-Z0-9_/-]+"
            if not re.fullmatch(pattern, settings[key]):
                fail(f"Select an ECS {resource} in this region and account.")
    if action == "cdn.create":
        name = settings["name"]
        if name.startswith(("xn--", "sthree-", "amzn-s3-demo-")) or name.endswith(("-s3alias", "--ol-s3", "--x-s3", "--table-s3", "-an")):
            fail("Enter a valid general purpose S3 bucket name.")
    if action == "cdn.create" and (settings["domain"] or settings["certificate"]):
        if not settings["domain"] or not re.fullmatch(
                rf"arn:aws:acm:us-east-1:{account}:certificate/[a-f0-9-]{{36}}", settings["certificate"]):
            fail("Provide both a domain and an ACM certificate ARN in us-east-1 for this account.")
    if action == "monitoring.metrics":
        pattern = INSTANCE if settings["resource"] == "EC2" else FUNCTION
        if not re.fullmatch(pattern, settings["id"]):
            fail("Enter an EC2 instance ID or Lambda function name.")
    return settings


def stack_name(action, settings):
    name = settings["name"].lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")[:40]
    key = hashlib.sha256(settings["name"].encode()).hexdigest()[:8]
    return f"{PREFIX}{ACTIONS[action]['group']}-{action.replace('.', '-')}-{slug}-{key}"


def target(action, settings):
    if ACTIONS[action]["deploy"]:
        return settings["name"]
    if action == "dynamodb.remove":
        return settings["id"] + "/" + settings["item_key"]
    if action == "ecr.remove":
        return settings["repository"] + "@" + settings["id"]
    return settings.get("id", "AWS account")


def db_item(data, partition_key):
    item = json_value(data)
    key = item.get(partition_key)
    if not isinstance(key, str) or not key or len(key.encode()) > 2048:
        fail(f"Provide a non-empty string for the partition key {partition_key}.")
    try:
        attributes = {key: TypeSerializer().serialize(value) for key, value in item.items()}
    except (ArithmeticError, ValueError, TypeError, RecursionError):
        fail("The item contains a value DynamoDB cannot store.")
    return key, attributes


def table_key(response):
    table = response["Table"]
    keys = table["KeySchema"]
    types = {item["AttributeName"]: item["AttributeType"] for item in table["AttributeDefinitions"]}
    if len(keys) != 1 or keys[0]["KeyType"] != "HASH" or types.get(keys[0]["AttributeName"]) != "S":
        fail("Item actions require a table with one string partition key.")
    return keys[0]["AttributeName"]


def owned_stack(session, account, region, stack_id):
    if not stack_id.startswith(f"arn:aws:cloudformation:{region}:{account}:stack/{PREFIX}"):
        fail("Select a deployment in this account and region.", 403)
    stack = client(session, "cloudformation").describe_stacks(StackName=stack_id)["Stacks"][0]
    tags = {entry["Key"]: entry["Value"] for entry in stack.get("Tags", [])}
    if stack["StackId"] != stack_id or tags.get(TAG) != "true":
        fail("This deployment is not managed by AWS Manager.", 403)
    return stack


def check_network(session, settings):
    subnet_ids = [settings[key] for key in ("subnet_a", "subnet_b") if key in settings]
    if not subnet_ids:
        return
    if len(set(subnet_ids)) != len(subnet_ids):
        fail("Select distinct subnets.")
    ec2 = client(session, "ec2")
    subnets = ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]
    group = ec2.describe_security_groups(GroupIds=[settings["group"]])["SecurityGroups"][0]
    if len(subnets) != len(subnet_ids) or any(
            subnet["VpcId"] != group["VpcId"] or subnet["State"] != "available" for subnet in subnets):
        fail("The selected subnets and security group must be available in the same VPC.")
    if len({subnet["AvailabilityZone"] for subnet in subnets}) != len(subnet_ids):
        fail("Select subnets in different Availability Zones.")
    if "instance" in settings:
        instance = ec2.describe_instances(InstanceIds=[settings["instance"]])["Reservations"][0]["Instances"][0]
        if instance["VpcId"] != group["VpcId"]:
            fail("The target instance must be in the same VPC as the load balancer.")
        settings["vpc"] = group["VpcId"]


def api_description(session, api_id):
    result = client(session, "apigatewayv2").get_api(ApiId=api_id)
    if result["ProtocolType"] != "HTTP" or result.get("DisableExecuteApiEndpoint"):
        fail("Select an HTTP API with its AWS endpoint enabled.")
    return result


def prepare(action, settings, data, session, account, region):
    if ACTIONS[action]["deploy"]:
        settings["deployment"] = stack_name(action, settings)
    if action in {"rds.create", "alb.create", "ecs.deploy"}:
        if session:
            check_network(session, settings)
            if action == "ecs.deploy":
                registry, image = settings["image"].split("/", 1)
                repository, tag = image.rsplit(":", 1) if "@" not in image else image.split("@", 1)
                key = "imageDigest" if tag.startswith("sha256:") else "imageTag"
                resolved = client(session, "ecr").describe_images(repositoryName=repository, imageIds=[{key: tag}])["imageDetails"][0]
                settings["image"] = f"{registry}/{repository}@{resolved['imageDigest']}"
        elif action == "alb.create":
            settings["vpc"] = "vpc-12345678"
    if action in {"dynamodb.put", "dynamodb.get", "dynamodb.remove"} and session:
        settings["partition_key"] = table_key(client(session, "dynamodb").describe_table(TableName=settings["id"]))
    if action == "dynamodb.put" and "partition_key" in settings:
        settings["item_key"], _ = db_item(data, settings["partition_key"])
    if action in {"api.route", "api.test"} and session:
        api_description(session, settings["id"])
        if action == "api.route":
            stage = client(session, "apigatewayv2").get_stage(ApiId=settings["id"], StageName="$default")
            if not stage.get("AutoDeploy"):
                fail("The API default stage must use automatic deployments.")
    if action == "api.test" and data and settings["method"] == "GET":
        fail("Clear the request body for a GET request.")
    if action == "deployment.delete" and session:
        stack = owned_stack(session, account, region, settings["id"])
        settings["deployment"] = stack["StackName"]
    if action in {"sqs.role", "sqs.trigger"} and session:
        sqs = client(session, "sqs")
        url = sqs.get_queue_url(QueueName=settings["id"], QueueOwnerAWSAccountId=account)["QueueUrl"]
        attributes = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn", "VisibilityTimeout"])["Attributes"]
        settings["queue_arn"] = attributes["QueueArn"]
        settings["visibility"] = int(attributes["VisibilityTimeout"])
    if action in {"sqs.create", "sqs.trigger"} and settings["function"] and session:
        config = client(session, "lambda").get_function_configuration(FunctionName=settings["function"])
        if settings["visibility"] < config["Timeout"] * 6:
            fail("The queue visibility timeout must be at least six times the function timeout.")
    if action == "dns.alias":
        settings.update(resolve_alias(settings, session, region, account))
    return settings


def resolve_alias(settings, session, region, account):
    value = settings["destination"]
    zone = settings["id"]
    record = settings["name"].rstrip(".").lower() + "."
    if session:
        hosted = client(session, "route53").get_hosted_zone(Id=zone)["HostedZone"]
        zone_name = hosted["Name"].lower()
        if hosted.get("Config", {}).get("PrivateZone") or not (record == zone_name or record.endswith("." + zone_name)):
            fail("The record must be inside the selected public hosted zone.")
    if settings["target_type"] == "CloudFront":
        if not re.fullmatch(r"[A-Z0-9]{1,32}", value):
            fail("Enter a CloudFront distribution ID.")
        dns = (client(session, "cloudfront").get_distribution(Id=value)["Distribution"]["DomainName"]
               if session else "demo.cloudfront.net")
        if session:
            aliases = client(session, "cloudfront").get_distribution_config(Id=value)["DistributionConfig"]["Aliases"]
            if record.rstrip(".") not in aliases.get("Items", []):
                fail("Add this domain and its TLS certificate to the CloudFront distribution before creating the alias.")
        return {"alias_dns": dns, "alias_zone": "Z2FDTNDATAQYW2"}
    if not re.fullmatch(LOAD_BALANCER, value) or not value.startswith(f"arn:aws:elasticloadbalancing:{region}:{account}:"):
        fail("Enter an application load balancer ARN in the selected region and account.")
    if not session:
        return {"alias_dns": "demo.elb.amazonaws.com", "alias_zone": "DEMO"}
    lb = client(session, "elbv2").describe_load_balancers(LoadBalancerArns=[value])["LoadBalancers"][0]
    if lb["Scheme"] != "internet-facing":
        fail("Select an internet-facing load balancer for a public DNS record.")
    return {"alias_dns": "dualstack." + lb["DNSName"], "alias_zone": lb["CanonicalHostedZoneId"]}


def resource(kind, **properties):
    return {"Type": "AWS::" + kind, "Properties": properties}


def ref(logical_id):
    return {"Ref": logical_id}


def attr(logical_id, name):
    return {"Fn::GetAtt": [logical_id, name]}


def lambda_arn(settings, region, account):
    return f"arn:aws:lambda:{region}:{account}:function:{settings['function']}"


def lambda_access(principal, function, source):
    return resource("Lambda::Permission", Action="lambda:InvokeFunction", FunctionName=function,
                    Principal=principal, SourceArn=source)


def api_resources(action, settings, region, account):
    function = lambda_arn(settings, region, account)
    api_id = ref("Api") if action == "api.create" else settings["id"]
    resources = {
        "Integration": resource("ApiGatewayV2::Integration", ApiId=api_id, IntegrationType="AWS_PROXY",
                                IntegrationUri=function, PayloadFormatVersion="2.0"),
        "Route": resource("ApiGatewayV2::Route", ApiId=api_id,
                          RouteKey=settings["method"] + " " + settings["path"], AuthorizationType="AWS_IAM",
                          Target={"Fn::Join": ["/", ["integrations", ref("Integration")]]}),
        "InvokePermission": lambda_access("apigateway.amazonaws.com", function, {"Fn::Join": ["", [
            f"arn:aws:execute-api:{region}:{account}:", api_id,
            f"/$default/{settings['method']}{settings['path']}"]]}),
    }
    if action == "api.create":
        resources["Api"] = resource("ApiGatewayV2::Api", Name=settings["name"], ProtocolType="HTTP")
        resources["Stage"] = resource("ApiGatewayV2::Stage", ApiId=api_id, StageName="$default", AutoDeploy=True)
        resources["Stage"]["DependsOn"] = ["Route", "InvokePermission"]
    return resources, {"ApiId": api_id, "RouteId": ref("Route")}


def rds_resources(settings):
    database = resource("RDS::DBInstance", DBInstanceIdentifier=settings["name"],
        Engine=settings["engine"], DBInstanceClass=settings["instance_class"],
        DBSubnetGroupName=ref("SubnetGroup"), VPCSecurityGroups=[settings["group"]],
        StorageType="gp3", AllocatedStorage=str(settings["storage"]), StorageEncrypted=True,
        PubliclyAccessible=False, BackupRetentionPeriod=7, MasterUsername=settings["username"],
        ManageMasterUserPassword=True)
    database.update(DeletionPolicy="Snapshot", UpdateReplacePolicy="Snapshot")
    return {"SubnetGroup": resource("RDS::DBSubnetGroup", DBSubnetGroupDescription="Hermes AWS Manager database network",
                                     SubnetIds=[settings["subnet_a"], settings["subnet_b"]]),
            "Database": database}, {"Database": ref("Database"), "Endpoint": attr("Database", "Endpoint.Address"),
                                    "SecretArn": attr("Database", "MasterUserSecret.SecretArn")}


def alb_resources(settings):
    return {
        "LoadBalancer": resource("ElasticLoadBalancingV2::LoadBalancer", Type="application", Scheme=settings["scheme"],
                                 Subnets=[settings["subnet_a"], settings["subnet_b"]], SecurityGroups=[settings["group"]]),
        "TargetGroup": resource("ElasticLoadBalancingV2::TargetGroup", VpcId=settings["vpc"], Port=settings["port"],
                               Protocol="HTTP", TargetType="instance", Targets=[{"Id": settings["instance"]}]),
        "Listener": resource("ElasticLoadBalancingV2::Listener", LoadBalancerArn=ref("LoadBalancer"), Port=80,
                             Protocol="HTTP", DefaultActions=[{"Type": "forward", "TargetGroupArn": ref("TargetGroup")}]),
    }, {"LoadBalancer": ref("LoadBalancer"), "DnsName": attr("LoadBalancer", "DNSName"),
        "TargetGroup": ref("TargetGroup")}


def ecs_resources(settings, region):
    return {
        "Logs": resource("Logs::LogGroup", RetentionInDays=7),
        "Cluster": resource("ECS::Cluster"),
        "Task": resource("ECS::TaskDefinition", NetworkMode="awsvpc", RequiresCompatibilities=["FARGATE"],
            Cpu="256", Memory="512", ExecutionRoleArn=settings["role"], ContainerDefinitions=[{
                "Name": "app", "Image": settings["image"], "Essential": True,
                "PortMappings": [{"ContainerPort": settings["port"], "Protocol": "tcp"}],
                "LogConfiguration": {"LogDriver": "awslogs", "Options": {
                    "awslogs-group": ref("Logs"), "awslogs-region": region, "awslogs-stream-prefix": "app"}}}]),
        "Service": resource("ECS::Service", Cluster=ref("Cluster"), ServiceName=settings["name"],
            TaskDefinition=ref("Task"), LaunchType="FARGATE", DesiredCount=settings["count"],
            DeploymentConfiguration={"DeploymentCircuitBreaker": {"Enable": True, "Rollback": True}},
            NetworkConfiguration={"AwsvpcConfiguration": {"Subnets": [settings["subnet_a"]],
                "SecurityGroups": [settings["group"]], "AssignPublicIp": settings["public_ip"]}}),
    }, {"Cluster": attr("Cluster", "Arn"), "Service": ref("Service"), "LogGroup": ref("Logs")}


def cdn_resources(settings, account):
    origin = "private-s3"
    config = {
        "Enabled": True, "DefaultRootObject": "index.html", "HttpVersion": "http2",
        "Origins": [{"Id": origin, "DomainName": attr("Bucket", "RegionalDomainName"),
                     "OriginAccessControlId": attr("OriginAccess", "Id"), "S3OriginConfig": {"OriginAccessIdentity": ""}}],
        "DefaultCacheBehavior": {"TargetOriginId": origin, "ViewerProtocolPolicy": "redirect-to-https",
                                 "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
                                 "AllowedMethods": ["GET", "HEAD"], "Compress": True},
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
    }
    if settings["domain"]:
        config.update(Aliases=[settings["domain"].rstrip(".")], ViewerCertificate={
            "AcmCertificateArn": settings["certificate"], "SslSupportMethod": "sni-only",
            "MinimumProtocolVersion": "TLSv1.2_2021"})
    return {
        "Bucket": resource("S3::Bucket", BucketName=settings["name"], PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}),
        "OriginAccess": resource("CloudFront::OriginAccessControl", OriginAccessControlConfig={
            "Name": settings["deployment"][:64], "OriginAccessControlOriginType": "s3",
            "SigningBehavior": "always", "SigningProtocol": "sigv4"}),
        "Distribution": resource("CloudFront::Distribution", DistributionConfig=config),
        "BucketPolicy": resource("S3::BucketPolicy", Bucket=ref("Bucket"), PolicyDocument={"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "cloudfront.amazonaws.com"}, "Action": "s3:GetObject",
            "Resource": {"Fn::Join": ["", [attr("Bucket", "Arn"), "/*"]]},
            "Condition": {"StringEquals": {"AWS:SourceArn": {"Fn::Join": ["", [
                f"arn:aws:cloudfront::{account}:distribution/", ref("Distribution")]]}}}}]}),
    }, {"Bucket": ref("Bucket"), "Distribution": ref("Distribution"), "Domain": attr("Distribution", "DomainName")}


def role_resource(service, **properties):
    return resource("IAM::Role", AssumeRolePolicyDocument={"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}, **properties)


def lambda_execution_role(settings, region, account, policy_name, access):
    log = f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{settings['function']}"
    return role_resource("lambda.amazonaws.com", Policies=[{"PolicyName": policy_name,
        "PolicyDocument": {"Version": "2012-10-17", "Statement": [
            access, {"Effect": "Allow", "Action": "logs:CreateLogGroup", "Resource": log},
            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": log + ":log-stream:*"}]}}])


def queue_consumer(source, function):
    return resource("Lambda::EventSourceMapping", EventSourceArn=source, FunctionName=function, BatchSize=10, Enabled=True)


def deployment_template(action, settings, region, account):
    name = settings["name"]
    if action == "dynamodb.create":
        key = settings["partition_key"]
        resources = {"Table": resource("DynamoDB::Table", TableName=name, BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": key, "KeyType": "HASH"}])}
        outputs = {"Table": ref("Table")}
    elif action == "dynamodb.role":
        table = f"arn:aws:dynamodb:{region}:{account}:table/{settings['id']}"
        resources = {"Role": lambda_execution_role(settings, region, account, "TableAndFunctionLogs", {
            "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                                           "dynamodb:DeleteItem", "dynamodb:Query"], "Resource": table})}
        outputs = {"RoleArn": attr("Role", "Arn")}
    elif action == "rds.create":
        resources, outputs = rds_resources(settings)
    elif action in {"api.create", "api.route"}:
        resources, outputs = api_resources(action, settings, region, account)
    elif action == "vpc.create":
        resources = {"Vpc": resource("EC2::VPC", CidrBlock=settings["cidr"], EnableDnsSupport=True, EnableDnsHostnames=True,
                                     Tags=[{"Key": "Name", "Value": name}])}
        outputs = {"VpcId": ref("Vpc")}
    elif action == "vpc.subnet":
        resources = {"Subnet": resource("EC2::Subnet", VpcId=settings["vpc"], CidrBlock=settings["cidr"],
            AvailabilityZone=settings["zone"], MapPublicIpOnLaunch=False, Tags=[{"Key": "Name", "Value": name}])}
        outputs = {"SubnetId": ref("Subnet")}
    elif action == "alb.create":
        resources, outputs = alb_resources(settings)
    elif action == "dns.zone":
        resources = {"Zone": resource("Route53::HostedZone", Name=name)}
        outputs = {"ZoneId": ref("Zone"), "NameServers": {"Fn::Join": [", ", attr("Zone", "NameServers")]}}
    elif action == "dns.alias":
        resources = {"Record": resource("Route53::RecordSet", HostedZoneId=settings["id"], Name=name, Type="A",
            AliasTarget={"DNSName": settings["alias_dns"], "HostedZoneId": settings["alias_zone"], "EvaluateTargetHealth": False})}
        outputs = {"Record": name}
    elif action == "ecr.create":
        resources = {"Repository": resource("ECR::Repository", RepositoryName=name, ImageTagMutability="IMMUTABLE",
            ImageScanningConfiguration={"ScanOnPush": True})}
        outputs = {"RepositoryUri": attr("Repository", "RepositoryUri")}
    elif action == "ecs.role":
        resources = {"Role": role_resource("ecs-tasks.amazonaws.com",
            ManagedPolicyArns=["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"])}
        outputs = {"RoleArn": attr("Role", "Arn")}
    elif action == "ecs.deploy":
        resources, outputs = ecs_resources(settings, region)
    elif action == "events.schedule":
        function = lambda_arn(settings, region, account)
        resources = {"Rule": resource("Events::Rule", Name=name, ScheduleExpression=settings["expression"],
            State="ENABLED", Targets=[{"Id": "Lambda", "Arn": function}]),
            "Permission": lambda_access("events.amazonaws.com", function, attr("Rule", "Arn"))}
        outputs = {"Rule": ref("Rule")}
    elif action == "sqs.create":
        resources = {"Queue": resource("SQS::Queue", QueueName=name, SqsManagedSseEnabled=True,
                                       VisibilityTimeout=settings["visibility"])}
        if settings["function"]:
            resources["Consumer"] = queue_consumer(attr("Queue", "Arn"), lambda_arn(settings, region, account))
        outputs = {"Queue": ref("Queue"), "QueueArn": attr("Queue", "Arn")}
    elif action == "sqs.role":
        resources = {"Role": lambda_execution_role(settings, region, account, "QueueAndFunctionLogs", {
            "Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
            "Resource": settings["queue_arn"]})}
        outputs = {"RoleArn": attr("Role", "Arn")}
    elif action == "sqs.trigger":
        resources = {"Consumer": queue_consumer(settings["queue_arn"], lambda_arn(settings, region, account))}
        outputs = {"Consumer": ref("Consumer")}
    elif action == "cdn.create":
        resources, outputs = cdn_resources(settings, account)
    else:
        raise ValueError(action)
    return {"AWSTemplateFormatVersion": "2010-09-09", "Resources": resources,
            "Outputs": {key: {"Value": value} for key, value in outputs.items()}}


class BlockRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def test_api(session, region, settings, data):
    api_description(session, settings["id"])
    url = f"https://{settings['id']}.execute-api.{region}.amazonaws.com{settings['path']}"
    credentials = session.get_credentials().get_frozen_credentials()
    method = settings["method"]
    signed = AWSRequest(method=method, url=url, data=data, headers={"Content-Type": "application/json"} if data else {})
    SigV4Auth(credentials, "execute-api", region).add_auth(signed)
    request = Request(url, data=data or None, headers=dict(signed.headers), method=method)
    try:
        response = build_opener(BlockRedirects()).open(request, timeout=8)
    except HTTPError as error:
        response = error
    except (URLError, TimeoutError):
        fail("The request outcome could not be confirmed. Inspect the API or Lambda logs before retrying.", 504)
    with response:
        code = response.status
        request_id = response.headers.get("x-amzn-requestid")
        try:
            body = response.read(16385)
        except (OSError, IncompleteRead):
            body = b"Response body could not be read."
    return {"id": settings["id"], "state": f"HTTP {code}", "request_id": request_id,
            "output": body[:16384].decode("utf-8", "replace"), "truncated": len(body) > 16384,
            **({"warning": f"The API returned HTTP {code}."} if not 200 <= code < 300 else {})}


def cloudwatch_metrics(session, settings):
    resource_type = settings["resource"]
    namespace, dimension, metrics = (
        ("AWS/EC2", "InstanceId", [("CPUUtilization", "Average")]) if resource_type == "EC2" else
        ("AWS/Lambda", "FunctionName", [("Invocations", "Sum"), ("Errors", "Sum"), ("Duration", "Average")]))
    queries = [{"Id": f"m{index}", "Label": metric, "MetricStat": {
        "Metric": {"Namespace": namespace, "MetricName": metric,
                   "Dimensions": [{"Name": dimension, "Value": settings["id"]}]}, "Period": 300, "Stat": stat}}
        for index, (metric, stat) in enumerate(metrics)]
    end = datetime.now(timezone.utc)
    result = client(session, "cloudwatch").get_metric_data(MetricDataQueries=queries,
        StartTime=end - timedelta(hours=settings["hours"]), EndTime=end, ScanBy="TimestampAscending")
    return [{"label": row["Label"], "points": [[iso(ts), value] for ts, value in zip(row["Timestamps"], row["Values"])],
             "status": row["StatusCode"]} for row in result["MetricDataResults"]]


def costs(session, settings):
    end = datetime.now(timezone.utc).date()
    params = {"TimePeriod": {"Start": (end - timedelta(days=int(settings["days"]))).isoformat(), "End": end.isoformat()},
              "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"], "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]}
    costs_client = session.client("ce", region_name="us-east-1", config=READ)
    totals = {}
    estimated = False
    for _ in range(10):
        response = costs_client.get_cost_and_usage(**params)
        for window in response["ResultsByTime"]:
            estimated |= window["Estimated"]
            for group in window.get("Groups", []):
                cost = group["Metrics"]["UnblendedCost"]
                key = (group["Keys"][0], cost["Unit"])
                totals[key] = totals.get(key, Decimal("0")) + Decimal(cost["Amount"])
        token = response.get("NextPageToken")
        if not token:
            break
        params["NextPageToken"] = token
    return {"period": params["TimePeriod"], "estimated": estimated, "incomplete": bool(token),
            "services": [{"service": name, "unit": unit, "amount": str(amount)}
                         for (name, unit), amount in sorted(totals.items(), key=lambda entry: entry[1], reverse=True)]}


def live_action(session, scope, account, action, settings, data, plan_id):
    region = scope.region
    spec = ACTIONS[action]
    if spec["deploy"]:
        template = deployment_template(action, settings, region, account)
        params = {"StackName": settings["deployment"], "TemplateBody": json.dumps(template),
                  "ClientRequestToken": plan_id, "OnFailure": "ROLLBACK",
                  "Tags": [{"Key": TAG, "Value": "true"}, {"Key": TAG + "Service", "Value": spec["group"]}]}
        if any(item["Type"].startswith("AWS::IAM::") for item in template["Resources"].values()):
            params["Capabilities"] = ["CAPABILITY_IAM"]
        response = client(session, "cloudformation", write=True).create_stack(**params)
        return {"id": response["StackId"], "state": "submitted", "verification_kind": "stack"}
    if action == "deployment.delete":
        owned_stack(session, account, region, settings["id"])
        client(session, "cloudformation", write=True).delete_stack(StackName=settings["id"], ClientRequestToken=plan_id)
        return {"id": settings["id"], "state": "deleting", "verification_kind": "stack_deleted"}
    if action.startswith("dynamodb."):
        ddb = client(session, "dynamodb", write=not spec["read"])
        key_name = settings["partition_key"]
        table = settings["id"]
        if action == "dynamodb.put":
            _, item = db_item(data, key_name)
            ddb.put_item(TableName=table, Item=item, ConditionExpression="attribute_not_exists(#pk)",
                         ExpressionAttributeNames={"#pk": key_name})
            fingerprint = item_digest(item)
            return {"id": table, "state": "written", "verification_kind": "dynamodb_item", "item_digest": fingerprint}
        key = {key_name: {"S": settings["item_key"]}}
        if action == "dynamodb.get":
            result = ddb.get_item(TableName=table, Key=key, ConsistentRead=True)
            item = result.get("Item")
            return {"id": table, "state": "found" if item else "missing",
                    "output": json.dumps(item, indent=2, default=str) if item else "No item with this key."}
        ddb.delete_item(TableName=table, Key=key, ConditionExpression="attribute_exists(#pk)",
                        ExpressionAttributeNames={"#pk": key_name})
        return {"id": table, "state": "deleted"}
    if action == "api.test":
        return test_api(session, region, settings, data)
    if action == "sqs.send":
        sqs = client(session, "sqs", write=True)
        queue = sqs.get_queue_url(QueueName=settings["id"], QueueOwnerAWSAccountId=account)["QueueUrl"]
        response = sqs.send_message(QueueUrl=queue, MessageBody=data.decode())
        return {"id": settings["id"], "state": "queued", "message_id": response["MessageId"]}
    if action == "ecr.remove":
        response = client(session, "ecr", write=True).batch_delete_image(
            repositoryName=settings["repository"], imageIds=[{"imageDigest": settings["id"]}])
        if response.get("failures"):
            fail(response["failures"][0].get("failureReason") or "ECR did not delete this image.", 409)
        return {"id": settings["id"], "state": "deleted"}
    if action == "ecs.scale":
        ecs = client(session, "ecs", write=True)
        service = ecs.update_service(cluster=settings["cluster"], service=settings["id"], desiredCount=settings["count"])["service"]
        return {"id": settings["id"], "state": service["status"], "verification_kind": "ecs_scale"}
    if action == "monitoring.metrics":
        return {"id": settings["id"], "state": "read", "series": cloudwatch_metrics(session, settings)}
    if action == "monitoring.logs":
        response = client(session, "logs").filter_log_events(logGroupName=settings["id"], limit=50,
            startTime=int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp() * 1000))
        lines = [f"{iso(datetime.fromtimestamp(event['timestamp'] / 1000, timezone.utc))}  {event['message']}"
                 for event in response["events"]]
        return {"id": settings["id"], "state": "read", "output": "\n".join(lines)[:16384] or "No events in the last hour."}
    if action == "cost.report":
        return {"id": "AWS account", "state": "read", "costs": costs(session, settings)}
    raise ValueError(action)


def row(identifier, name=None, state=None, fields=None, **values):
    return {"id": identifier, "name": name or identifier, "state": state, "facts": fields or {}, **values}


def tags_name(tags, fallback):
    return next((tag["Value"] for tag in tags if tag["Key"] == "Name"), fallback)


def token_arg(key, cursor):
    return {key: cursor} if cursor else {}


def list_page(method, key, item, *, cursor="", request_cursor="NextToken", response_cursor=None, **kwargs):
    response = method(**kwargs, **token_arg(request_cursor, cursor))
    return [item(value) for value in response.get(key, [])], response.get(response_cursor or request_cursor)


def live_inventory(session, region, group, view_id, cursor, parent):
    if view_id == "deployments":
        cfn = client(session, "cloudformation")
        response = cfn.list_stacks(**token_arg("NextToken", cursor))
        prefix = f"{PREFIX}{group}-"
        return [row(item["StackId"], item["StackName"], item["StackStatus"],
                    {"Last status": item.get("StackStatusReason"), "Created": iso(item["CreationTime"])})
                for item in response["StackSummaries"] if item["StackName"].startswith(prefix)
                and item["StackStatus"] != "DELETE_COMPLETE"], response.get("NextToken")
    if view_id == "dynamodb":
        return list_page(client(session, "dynamodb").list_tables, "TableNames", row, cursor=cursor,
                         request_cursor="ExclusiveStartTableName", response_cursor="LastEvaluatedTableName", Limit=40)
    if view_id == "rds":
        return list_page(client(session, "rds").describe_db_instances, "DBInstances", lambda value: row(
            value["DBInstanceIdentifier"], state=value["DBInstanceStatus"], fields={
                "Engine": value["Engine"], "Instance class": value["DBInstanceClass"],
                "Endpoint": value.get("Endpoint", {}).get("Address"), "Port": value.get("Endpoint", {}).get("Port"),
                "Public": value.get("PubliclyAccessible"), "Secret ARN": value.get("MasterUserSecret", {}).get("SecretArn")}),
            cursor=cursor, request_cursor="Marker", MaxRecords=40)
    if view_id == "apis":
        rows, next_cursor = list_page(client(session, "apigatewayv2").get_apis, "Items", lambda value: value,
                                      cursor=cursor, MaxResults="40")
        return [row(value["ApiId"], value["Name"], "HTTP", {"Endpoint": value.get("ApiEndpoint")})
                for value in rows if value["ProtocolType"] == "HTTP" and not value.get("DisableExecuteApiEndpoint")], next_cursor
    if view_id == "routes":
        return list_page(client(session, "apigatewayv2").get_routes, "Items", lambda value: row(
            value["RouteId"], value["RouteKey"], fields={"Authorization": value.get("AuthorizationType"),
                "Integration": value.get("Target")}), cursor=cursor, ApiId=parent, MaxResults="40")
    if view_id == "alarms":
        return list_page(client(session, "cloudwatch").describe_alarms, "MetricAlarms", lambda value: row(
            value["AlarmName"], state=value["StateValue"], fields={
                "Metric": value.get("MetricName"), "Reason": value.get("StateReason"), "Threshold": value.get("Threshold")}),
            cursor=cursor, MaxRecords=40)
    if view_id == "logs":
        return list_page(client(session, "logs").describe_log_groups, "logGroups", lambda value: row(
            value["logGroupName"], fields={"Retention (days)": value.get("retentionInDays"), "Stored bytes": value.get("storedBytes")}),
            cursor=cursor, request_cursor="nextToken", limit=40)
    if view_id in {"vpc", "subnet", "groups"}:
        ec2 = client(session, "ec2")
        if view_id == "groups":
            return list_page(ec2.describe_security_groups, "SecurityGroups", lambda value: row(
                value["GroupId"], value["GroupName"], fields={"VPC": value.get("VpcId"), "Description": value.get("Description")}),
                cursor=cursor, MaxResults=40)
        if view_id == "vpc":
            return list_page(ec2.describe_vpcs, "Vpcs", lambda value: row(value["VpcId"],
                tags_name(value.get("Tags", []), value["VpcId"]), value["State"], {"CIDR": value["CidrBlock"]}),
                cursor=cursor, MaxResults=40)
        return list_page(ec2.describe_subnets, "Subnets", lambda value: row(value["SubnetId"],
            tags_name(value.get("Tags", []), value["SubnetId"]), value["State"], {
                "CIDR": value["CidrBlock"], "VPC": value["VpcId"], "Availability Zone": value["AvailabilityZone"],
                "Auto-assign public IPv4": value["MapPublicIpOnLaunch"]}), cursor=cursor, MaxResults=40)
    if view_id == "alb":
        return list_page(client(session, "elbv2").describe_load_balancers, "LoadBalancers", lambda value: row(
            value["LoadBalancerArn"], value["LoadBalancerName"], value["State"]["Code"], {
                "DNS name": value["DNSName"], "Visibility": value["Scheme"], "VPC": value["VpcId"]}),
            cursor=cursor, request_cursor="Marker", response_cursor="NextMarker", PageSize=40)
    if view_id == "zones":
        return list_page(client(session, "route53").list_hosted_zones, "HostedZones", lambda value: row(
            value["Id"].split("/")[-1], value["Name"], "Private" if value.get("Config", {}).get("PrivateZone") else "Public",
            {"Records": value.get("ResourceRecordSetCount")}), cursor=cursor, request_cursor="Marker", response_cursor="NextMarker", MaxItems="40")
    if view_id == "records":
        params = {"HostedZoneId": parent, "MaxItems": "40"}
        if cursor:
            try:
                next_record = json.loads(cursor)
                params.update(StartRecordName=next_record["name"], StartRecordType=next_record["type"])
                if next_record.get("id"):
                    params["StartRecordIdentifier"] = next_record["id"]
            except (ValueError, KeyError, TypeError):
                fail("Invalid record cursor.")
        response = client(session, "route53").list_resource_record_sets(**params)
        result = [row(f"{value['Name']} {value['Type']} {value.get('SetIdentifier', '')}".strip(), value["Name"],
                      value["Type"], {"Target": value["AliasTarget"]["DNSName"] if value.get("AliasTarget") else
                                      ", ".join(item["Value"] for item in value.get("ResourceRecords", []))})
                  for value in response["ResourceRecordSets"]]
        marker = json.dumps({"name": response["NextRecordName"], "type": response["NextRecordType"],
                             "id": response.get("NextRecordIdentifier")}) if response["IsTruncated"] else None
        return result, marker
    if view_id == "ecr":
        return list_page(client(session, "ecr").describe_repositories, "repositories", lambda value: row(
            value["repositoryName"], fields={"URI": value["repositoryUri"], "Tag mutability": value["imageTagMutability"]}),
            cursor=cursor, request_cursor="nextToken", maxResults=40)
    if view_id == "images":
        return list_page(client(session, "ecr").describe_images, "imageDetails", lambda value: row(
            value["imageDigest"], ", ".join(value.get("imageTags", [])) or "Untagged", fields={
                "Pushed": iso(value.get("imagePushedAt")), "Bytes": value.get("imageSizeInBytes"),
                "Digest URI": f"{value['registryId']}.dkr.ecr.{region}.amazonaws.com/{parent}@{value['imageDigest']}"}, repository=parent),
            cursor=cursor, request_cursor="nextToken", maxResults=40, repositoryName=parent)
    if view_id == "clusters":
        return list_page(client(session, "ecs").list_clusters, "clusterArns", lambda value: row(value, value.split("/")[-1]),
                         cursor=cursor, request_cursor="nextToken", maxResults=40)
    if view_id == "tasks":
        ecs = client(session, "ecs")
        response = ecs.list_services(cluster=parent, maxResults=10, **token_arg("nextToken", cursor))
        services = ecs.describe_services(cluster=parent, services=response["serviceArns"])["services"] if response["serviceArns"] else []
        return [row(value["serviceArn"], value["serviceName"], value["status"], {
            "Desired": value["desiredCount"], "Running": value["runningCount"], "Pending": value["pendingCount"],
            "Task definition": value["taskDefinition"]}, cluster=parent) for value in services], response.get("nextToken")
    if view_id == "rules":
        return list_page(client(session, "events").list_rules, "Rules", lambda value: row(
            value["Name"], state=value["State"], fields={"Schedule": value.get("ScheduleExpression"), "ARN": value["Arn"]}),
            cursor=cursor, request_cursor="NextToken", Limit=40)
    if view_id == "queues":
        sqs = client(session, "sqs")
        response = sqs.list_queues(MaxResults=10, **token_arg("NextToken", cursor))
        result = []
        for url in response.get("QueueUrls", []):
            try:
                attributes = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=[
                    "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
            except ClientError:
                attributes = {}
            result.append(row(url.split("/")[-1], fields={"Queue URL": url,
                "Ready messages": attributes.get("ApproximateNumberOfMessages"),
                "In-flight messages": attributes.get("ApproximateNumberOfMessagesNotVisible")}))
        return result, response.get("NextToken")
    if view_id == "distributions":
        response = client(session, "cloudfront").list_distributions(MaxItems="40", **token_arg("Marker", cursor))["DistributionList"]
        return [row(item["Id"], item["DomainName"], item["Status"], {
            "HTTPS URL": "https://" + item["DomainName"], "Enabled": item["Enabled"],
            "Aliases": ", ".join(item.get("Aliases", {}).get("Items", []))}) for item in response.get("Items", [])], response.get("NextMarker")
    fail("Unknown inventory view.", 404)


def stack_detail(session, account, region, stack_id):
    stack = owned_stack(session, account, region, stack_id)
    facts = {entry["OutputKey"]: entry["OutputValue"] for entry in stack.get("Outputs", [])}
    facts.update({"Stack status": stack["StackStatus"], "Status reason": stack.get("StackStatusReason")})
    if "FAILED" in stack["StackStatus"] or "ROLLBACK" in stack["StackStatus"]:
        try:
            events = client(session, "cloudformation").describe_stack_events(StackName=stack_id)["StackEvents"]
        except ClientError:
            events = []
        failures = [event for event in events if event["ResourceStatus"].endswith("_FAILED")
                    and event.get("ResourceStatusReason")]
        for index, event in enumerate(failures[:3], 1):
            facts[f"Failure {index}"] = f"{event['LogicalResourceId']} · {event['ResourceStatusReason'][:350]}"
    return facts


def verify(session, scope, account, action, settings, result):
    region = scope.region
    kind = result["verification_kind"]
    if kind == "stack_deleted":
        try:
            status = owned_stack(session, account, region, result["id"])["StackStatus"]
        except ClientError as error:
            if error_code(error) == "ValidationError" and "does not exist" in str(error):
                return "Deployment no longer exists."
            raise
        if status == "DELETE_COMPLETE":
            return "Deployment deleted."
        fail(f"Deployment is {status}. Check the deployment for details.", 409)
    if kind == "stack":
        stack = owned_stack(session, account, region, result["id"])
        status = stack["StackStatus"]
        if status != "CREATE_COMPLETE":
            fail(f"Deployment is {status}. Check the deployment for details.", 409)
        outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
        if action == "alb.create":
            health = client(session, "elbv2").describe_target_health(TargetGroupArn=outputs["TargetGroup"],
                Targets=[{"Id": settings["instance"]}])["TargetHealthDescriptions"]
            state = health[0]["TargetHealth"]["State"] if health else "missing"
            if state != "healthy":
                fail(f"Target is {state}. Check the target port, security groups, and HTTP health endpoint.", 409)
            return f"{settings['instance']} is healthy in the target group."
        if action == "cdn.create":
            distribution = client(session, "cloudfront").get_distribution(Id=outputs["Distribution"])["Distribution"]
            if distribution["Status"] != "Deployed":
                fail("CloudFront is still deploying.", 409)
            return "CloudFront reports Deployed. Upload index.html before opening the URL."
        if action == "ecs.deploy":
            return verify_ecs(session, outputs["Cluster"], outputs["Service"], settings["count"])
        if action == "rds.create":
            db = client(session, "rds").describe_db_instances(DBInstanceIdentifier=outputs["Database"])["DBInstances"][0]
            if db["DBInstanceStatus"] != "available":
                fail(f"Database is {db['DBInstanceStatus']}.", 409)
            return "RDS reports the database is available."
        return f"CloudFormation reports {status}."
    if kind == "ecs_scale":
        return verify_ecs(session, settings["cluster"], settings["id"], settings["count"])
    if kind == "dynamodb_item":
        key = {settings["partition_key"]: {"S": settings["item_key"]}}
        item = client(session, "dynamodb").get_item(TableName=settings["id"], Key=key, ConsistentRead=True).get("Item")
        if not item or item_digest(item) != result["item_digest"]:
            fail("The current item differs from the reviewed write.", 409)
        return "DynamoDB returned the exact written item."
    fail("This action has no read-back.", 409)


def verify_ecs(session, cluster, service, count):
    result = client(session, "ecs").describe_services(cluster=cluster, services=[service])["services"]
    if not result or result[0]["desiredCount"] != count or result[0]["runningCount"] != count or result[0]["pendingCount"]:
        fail("ECS has not reached the reviewed task count.", 409)
    return f"ECS reports {count} running tasks and no pending tasks."


def item_digest(item):
    def canonical(attribute):
        kind, value = next(iter(attribute.items()))
        if kind == "N":
            number = Decimal(value)
            value = format(number, "f") if number else "0"
            if "." in value:
                value = value.rstrip("0").rstrip(".")
        elif kind == "M":
            value = {key: canonical(child) for key, child in value.items()}
        elif kind == "L":
            value = [canonical(child) for child in value]
        return {kind: value}

    normalized = {key: canonical(attribute) for key, attribute in item.items()}
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, default=str).encode()).hexdigest()


def demo_find(db, region, kind, identifier, *, optional=False):
    entry = db.execute("SELECT data FROM demo_resources WHERE region=? AND kind=? AND id=?",
                       (region, kind, identifier)).fetchone()
    if entry:
        return json.loads(entry[0])
    if optional:
        return None
    fail("Resource not found in this Demo region.", 404)


def demo_store(db, region, kind, value, *, create=False):
    if create and demo_find(db, region, kind, value["id"], optional=True):
        fail(f"{value['id']} already exists in Demo.", 409)
    db.execute("INSERT OR REPLACE INTO demo_resources VALUES (?, ?, ?, ?)",
               (region, kind, value["id"], json.dumps(value)))


def demo_seed(db, region, account):
    if not db.execute("INSERT OR IGNORE INTO demo_resources VALUES (?, 'service_seed', 'v1', '{}')",
                      (region,)).rowcount:
        return
    repo = f"{account}.dkr.ecr.{region}.amazonaws.com/demo-web"
    digest = "sha256:" + hashlib.sha256(b"demo-web").hexdigest()
    fixtures = {
        "dynamodb": [row("demo-items", state="ACTIVE", fields={"Partition key": "id"}, partition_key="id")],
        "rds": [row("demo-postgres", state="available", fields={"Engine": "postgres", "Public": False})],
        "apis": [row("demoapi001", "demo-http", "HTTP")],
        "routes:demoapi001": [row("demoroute01", "GET /items", fields={"Authorization": "AWS_IAM", "Function": "process-event"})],
        "alarms": [row("demo-lambda-errors", state="OK", fields={"Metric": "Errors", "Threshold": 5})],
        "logs": [row("/aws/lambda/process-event", fields={"Retention (days)": 7})],
        "zones": [row("ZDEMO001", "example.test.", "Public")],
        "ecr": [row("demo-web", fields={"URI": repo, "Tag mutability": "IMMUTABLE"})],
        "images:demo-web": [row(digest, "v1", fields={"Digest URI": f"{repo}@{digest}"}, repository="demo-web")],
        "queues": [row("demo-tasks", fields={"Ready messages": 0, "In-flight messages": 0})],
    }
    for kind, items in fixtures.items():
        for item in items:
            demo_store(db, region, kind, item)


def demo_inventory(db, region, group, view_id, parent, cursor):
    kind = {"groups": "group", "deployments": "deployment:" + group}.get(view_id, view_id)
    if parent:
        kind += ":" + parent
    try:
        offset = int(cursor) if cursor else 0
        if offset < 0:
            raise ValueError
    except ValueError:
        fail("Invalid Demo cursor.")
    items = [json.loads(record[0]) for record in db.execute(
        "SELECT data FROM demo_resources WHERE region=? AND kind=? ORDER BY id LIMIT 41 OFFSET ?", (region, kind, offset))]
    for value in items:
        if "facts" not in value:
            value["facts"] = {"VPC": value.get("vpc"), "Availability Zone": value.get("zone")}
    return items[:40], str(offset + 40) if len(items) > 40 else None


def demo_prepare(db, region, action, settings, data):
    if action in {"api.route", "api.test"}:
        demo_find(db, region, "apis", settings["id"])
    if settings.get("function") and action not in {"dynamodb.role", "sqs.role"}:
        demo_find(db, region, "lambda", settings["function"])
    if action in {"sqs.role", "sqs.trigger"}:
        demo_find(db, region, "queues", settings["id"])
    if action in {"dynamodb.put", "dynamodb.get", "dynamodb.remove", "dynamodb.role"}:
        table = demo_find(db, region, "dynamodb", settings["id"])
        if action != "dynamodb.role":
            settings["partition_key"] = table["partition_key"]
        if action == "dynamodb.put":
            settings["item_key"], _ = db_item(data, settings["partition_key"])
    if action in {"rds.create", "alb.create", "ecs.deploy"}:
        subnets = [demo_find(db, region, "subnet", settings[key]) for key in ("subnet_a", "subnet_b") if key in settings]
        group = demo_find(db, region, "group", settings["group"])
        if len({subnet["zone"] for subnet in subnets}) != len(subnets) or any(subnet["vpc"] != group["vpc"] for subnet in subnets):
            fail("Select subnets in different zones of the security group's VPC.")
        if action == "alb.create":
            instance = demo_find(db, region, "ec2", settings["instance"])
            if instance["vpc"] != group["vpc"]:
                fail("The target and load balancer must use the same VPC.")
            settings["vpc"] = group["vpc"]
        if action == "ecs.deploy":
            demo_find(db, region, "role", settings["role"].split("/")[-1])
    if action == "vpc.subnet":
        demo_find(db, region, "vpc", settings["vpc"])
    if action == "dns.alias":
        zone = demo_find(db, region, "zones", settings["id"])["name"].rstrip(".")
        name = settings["name"].rstrip(".")
        if name != zone and not name.endswith("." + zone):
            fail("The record must be inside the hosted zone.")
        kind = "distributions" if settings["target_type"] == "CloudFront" else "alb"
        value = demo_find(db, region, kind, settings["destination"])
        if kind == "distributions" and name != value["facts"].get("Aliases"):
            fail("Add this domain to the distribution first.")
        if kind == "alb" and value["facts"]["Visibility"] != "internet-facing":
            fail("Select an internet-facing load balancer.")
        settings["alias_dns"] = value["name"] if kind == "distributions" else value["facts"]["DNS name"]
    if action == "deployment.delete":
        stack = demo_deployment(db, region, settings["id"])
        settings["deployment"] = stack["name"]
    return settings


def demo_deployment(db, region, stack_id):
    group = stack_id.split("/")[-2].removeprefix(PREFIX).split("-", 1)[0]
    return demo_find(db, region, "deployment:" + group, stack_id)


def demo_deploy(db, scope, account, action, settings, plan_id):
    region = scope.region
    name, deployment = settings["name"], settings["deployment"]
    stack_id = f"arn:aws:cloudformation:{region}:{account}:stack/{deployment}/{plan_id}"
    group = ACTIONS[action]["group"]
    kind = "deployment:" + group
    if db.execute("SELECT 1 FROM demo_resources WHERE region=? AND kind=? AND json_extract(data, '$.name')=?",
                  (region, kind, deployment)).fetchone():
        fail("A deployment with this name already exists in Demo.", 409)
    children = []

    def save(resource_kind, value):
        demo_store(db, region, resource_kind, value, create=True)
        children.append([resource_kind, value["id"]])

    if action == "dynamodb.create":
        key = settings["partition_key"]
        save("dynamodb", row(name, state="ACTIVE", fields={"Partition key": key}, partition_key=key))
        facts = {"Table": name}
    elif action == "rds.create":
        endpoint = f"{name}.demo.internal"
        save("rds", row(name, state="available", fields={"Engine": settings["engine"], "Endpoint": endpoint, "Public": False}))
        facts = {"Database": name, "Endpoint": endpoint}
    elif action in {"dynamodb.role", "ecs.role", "sqs.role"}:
        arn = f"arn:aws:iam::{account}:role/{name}-{plan_id[:8]}"
        save("role", {"id": arn.split("/")[-1], "name": name, "arn": arn})
        facts = {"RoleArn": arn}
    elif action in {"api.create", "api.route"}:
        api_id = settings.get("id", plan_id[:10])
        if action == "api.create":
            save("apis", row(api_id, name, "HTTP", {"Endpoint": f"https://{api_id}.demo.invalid"}))
        route_key = settings["method"] + " " + settings["path"]
        save("routes:" + api_id, row(route_key, fields={"Authorization": "AWS_IAM", "Function": settings["function"]}))
        facts = {"ApiId": api_id, "Route": route_key}
    elif action == "vpc.create":
        identifier = "vpc-" + plan_id[:17]
        save("vpc", row(identifier, name, "available", {"CIDR": settings["cidr"]}))
        facts = {"VpcId": identifier}
    elif action == "vpc.subnet":
        identifier = "subnet-" + plan_id[:17]
        save("subnet", row(identifier, name, "available", {"CIDR": settings["cidr"], "VPC": settings["vpc"],
            "Availability Zone": settings["zone"]}, vpc=settings["vpc"], zone=settings["zone"]))
        facts = {"SubnetId": identifier}
    elif action == "alb.create":
        arn = f"arn:aws:elasticloadbalancing:{region}:{account}:loadbalancer/app/{name}/{plan_id[:16]}"
        dns = f"{name}.demo.elb.amazonaws.com"
        save("alb", row(arn, name, "active", {"DNS name": dns, "Visibility": settings["scheme"], "VPC": settings["vpc"]}))
        facts = {"LoadBalancer": arn, "DnsName": dns}
    elif action == "dns.zone":
        identifier = "Z" + plan_id[:12].upper()
        save("zones", row(identifier, name.rstrip(".") + ".", "Public", {"Name servers": "ns1.example.test, ns2.example.test"}))
        facts = {"ZoneId": identifier}
    elif action == "dns.alias":
        save("records:" + settings["id"], row(name.rstrip(".") + ".", state="A", fields={"Target": settings["alias_dns"]}))
        facts = {"Record": name}
    elif action == "ecr.create":
        uri = f"{account}.dkr.ecr.{region}.amazonaws.com/{name}"
        save("ecr", row(name, fields={"URI": uri, "Tag mutability": "IMMUTABLE"}))
        facts = {"RepositoryUri": uri}
    elif action == "ecs.deploy":
        cluster = f"arn:aws:ecs:{region}:{account}:cluster/{name}-{plan_id[:8]}"
        service = f"arn:aws:ecs:{region}:{account}:service/{cluster.split('/')[-1]}/{name}"
        log_group = f"/aws/ecs/{name}-{plan_id[:8]}"
        save("clusters", row(cluster, cluster.split("/")[-1]))
        save("tasks:" + cluster, row(service, name, "ACTIVE", {
            "Desired": settings["count"], "Running": settings["count"], "Pending": 0,
            "Image": settings["image"]}, cluster=cluster))
        save("logs", row(log_group, fields={"Retention (days)": 7}))
        facts = {"Cluster": cluster, "Service": service, "LogGroup": log_group}
    elif action == "events.schedule":
        save("rules", row(name, state="ENABLED", fields={"Schedule": settings["expression"], "Function": settings["function"]}))
        facts = {"Rule": name}
    elif action == "sqs.create":
        url = f"https://sqs.{region}.demo.invalid/{account}/{name}"
        save("queues", row(name, fields={"Ready messages": 0, "In-flight messages": 0,
            "Queue URL": url, "Consumer": settings["function"]}))
        facts = {"Queue": url}
    elif action == "sqs.trigger":
        queue = demo_find(db, region, "queues", settings["id"])
        if queue["facts"].get("Consumer"):
            fail("This queue already has a Demo consumer.", 409)
        queue["facts"]["Consumer"] = settings["function"]
        demo_store(db, region, "queues", queue)
        children.append(["queue_consumer", settings["id"]])
        facts = {"Queue": settings["id"], "Function": settings["function"]}
    elif action == "cdn.create":
        identifier = "E" + plan_id[:12].upper()
        domain = f"{identifier.lower()}.demo.cloudfront.net"
        save("distributions", row(identifier, domain, "Deployed", {"HTTPS URL": "https://" + domain,
            "Aliases": settings["domain"].rstrip("."), "Bucket": name}))
        save("s3", {"id": name, "name": name, "region": region, "created": datetime.now(timezone.utc).isoformat()})
        facts = {"Distribution": identifier, "Domain": domain, "Bucket": name}
    else:
        raise ValueError(action)
    demo_store(db, region, kind, row(stack_id, deployment, "CREATE_COMPLETE", facts, children=children))
    return {"id": stack_id, "state": "submitted", "verification_kind": "stack"}


def demo_action(db, scope, account, action, settings, data, plan_id):
    region = scope.region
    if ACTIONS[action]["deploy"]:
        return demo_deploy(db, scope, account, action, settings, plan_id)
    identifier = settings.get("id")
    if action == "deployment.delete":
        deployment = demo_deployment(db, region, identifier)
        for kind, resource_id in deployment["children"]:
            if kind == "queue_consumer":
                queue = demo_find(db, region, "queues", resource_id, optional=True)
                if queue:
                    queue["facts"].pop("Consumer", None)
                    demo_store(db, region, "queues", queue)
                continue
            if kind == "s3" and db.execute("SELECT 1 FROM demo_objects WHERE region=? AND bucket=? LIMIT 1",
                                          (region, resource_id)).fetchone():
                fail("Empty the origin bucket before deleting the deployment.", 409)
            if kind == "vpc" and db.execute(
                    "SELECT 1 FROM demo_resources WHERE region=? AND kind='subnet' AND json_extract(data, '$.vpc')=?",
                    (region, resource_id)).fetchone():
                fail("Delete the VPC's subnets first.", 409)
            if kind in {"apis", "zones"}:
                child_kind = ("routes:" if kind == "apis" else "records:") + resource_id
                if db.execute("SELECT 1 FROM demo_resources WHERE region=? AND kind=? AND id NOT IN (" +
                              ",".join("?" for _ in deployment["children"]) + ")", (region, child_kind,
                              *(child_id for _, child_id in deployment["children"]))).fetchone():
                    fail("Delete the separate route or record deployments first.", 409)
            if kind == "ecr" and db.execute("SELECT 1 FROM demo_resources WHERE region=? AND kind=?",
                                            (region, "images:" + resource_id)).fetchone():
                fail("Delete the repository images first.", 409)
            if kind == "dynamodb":
                db.execute("DELETE FROM demo_resources WHERE region=? AND kind=?", (region, "items:" + resource_id))
            db.execute("DELETE FROM demo_resources WHERE region=? AND kind=? AND id=?", (region, kind, resource_id))
            if kind == "s3":
                db.execute("DELETE FROM connections WHERE mode='demo' AND account=? AND region=? AND bucket=?",
                           (account, region, resource_id))
        db.execute("DELETE FROM demo_resources WHERE region=? AND id=? AND kind LIKE 'deployment:%'", (region, identifier))
        return {"id": identifier, "state": "deleted", "verification_kind": "stack_deleted"}
    if action.startswith("dynamodb."):
        key = settings["item_key"]
        kind = "items:" + identifier
        if action == "dynamodb.put":
            _, item = db_item(data, settings["partition_key"])
            demo_store(db, region, kind, {"id": key, "item": item}, create=True)
            return {"id": identifier, "state": "written", "verification_kind": "dynamodb_item", "item_digest": item_digest(item)}
        item = demo_find(db, region, kind, key, optional=action == "dynamodb.get")
        if action == "dynamodb.get":
            return {"id": identifier, "state": "found" if item else "missing",
                    "output": json.dumps(item["item"], indent=2) if item else "No item with this key."}
        db.execute("DELETE FROM demo_resources WHERE region=? AND kind=? AND id=?", (region, kind, key))
        return {"id": identifier, "state": "deleted"}
    if action == "api.test":
        route = settings["method"] + " " + settings["path"]
        match = demo_find(db, region, "routes:" + identifier, route, optional=True)
        if not match:
            return {"id": identifier, "state": "HTTP 404", "warning": "No matching Demo route."}
        return {"id": identifier, "state": "HTTP 200 (Demo)",
                "output": json.dumps({"workspace": "demo", "route": route, "function": match["facts"]["Function"],
                                      "event": json.loads(data) if data else None}, indent=2)}
    if action == "sqs.send":
        queue = demo_find(db, region, "queues", identifier)
        queue["facts"]["Ready messages"] += 1
        demo_store(db, region, "queues", queue)
        return {"id": identifier, "state": "queued", "message_id": plan_id}
    if action == "ecr.remove":
        kind = "images:" + settings["repository"]
        demo_find(db, region, kind, identifier)
        db.execute("DELETE FROM demo_resources WHERE region=? AND kind=? AND id=?", (region, kind, identifier))
        return {"id": identifier, "state": "deleted"}
    if action == "ecs.scale":
        kind = "tasks:" + settings["cluster"]
        service = demo_find(db, region, kind, identifier)
        service["facts"].update(Desired=settings["count"], Running=settings["count"], Pending=0)
        demo_store(db, region, kind, service)
        return {"id": identifier, "state": "ACTIVE", "verification_kind": "ecs_scale"}
    if action == "monitoring.metrics":
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        labels = ["CPUUtilization"] if settings["resource"] == "EC2" else ["Invocations", "Errors", "Duration"]
        series = [{"label": label, "status": "Demo", "points": [
            [iso(now - timedelta(minutes=5 * (11 - index))), ((index + 4) * 17) % 35] for index in range(12)]}
            for label in labels]
        return {"id": identifier, "state": "read", "series": series}
    if action == "monitoring.logs":
        demo_find(db, region, "logs", identifier)
        return {"id": identifier, "state": "read", "output": "Demo workspace: sample log event."}
    if action == "cost.report":
        end = datetime.now(timezone.utc).date()
        return {"id": "AWS account", "state": "read", "costs": {"period": {
            "Start": iso(end - timedelta(days=int(settings["days"]))), "End": iso(end)}, "estimated": False,
            "services": [{"service": name, "unit": "USD", "amount": amount} for name, amount in
                         [("Amazon EC2", "8.12"), ("Amazon S3", "1.03"), ("AWS Lambda", "0.24")]]}}
    raise ValueError(action)


def demo_verify(db, scope, settings, result):
    region = scope.region
    identifier = result["id"]
    kind = result["verification_kind"]
    if kind == "stack_deleted":
        if db.execute("SELECT 1 FROM demo_resources WHERE region=? AND id=? AND kind LIKE 'deployment:%'",
                      (region, identifier)).fetchone():
            fail("The Demo deployment still exists.", 409)
        return "Deployment no longer exists in Demo."
    if kind == "stack":
        demo_deployment(db, region, identifier)
        return "Demo deployment completed."
    if kind == "ecs_scale":
        service = demo_find(db, region, "tasks:" + settings["cluster"], identifier)
        if service["facts"]["Running"] != settings["count"]:
            fail("The current Demo task count differs from the reviewed count.", 409)
        return f"Demo reports {settings['count']} running tasks."
    if kind == "dynamodb_item":
        item = demo_find(db, region, "items:" + identifier, settings["item_key"])
        if item_digest(item["item"]) != result["item_digest"]:
            fail("The current Demo item differs from the reviewed write.", 409)
        return "Demo returned the exact written item."
    fail("This action has no read-back.", 409)
