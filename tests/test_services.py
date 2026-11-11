"""Regression tests for the service review, apply, and read-back boundaries."""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import boto3
from botocore.stub import ANY, Stubber
from fastapi import FastAPI
from fastapi.testclient import TestClient

module_path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
spec = importlib.util.spec_from_file_location("aws_manager_test_api", module_path)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class ServiceWorkflow(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "workspace.db"
        db_patch = patch.object(api, "plugin_db", side_effect=lambda *_: sqlite3.connect(self.db))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        app = FastAPI()
        app.include_router(api.router)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)
        self.scope = {"mode": "demo", "region": "us-east-1"}

    def prepare(self, action, **values):
        response = self.http.post("/plans", json={"scope": self.scope, "action": action, "values": values})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def apply(self, plan, *, payload="", confirmation=""):
        response = self.http.post(f"/plans/{plan['id']}/apply", json={"payload": payload, "confirmation": confirmation})
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()
        self.assertEqual(receipt["status"], "completed", receipt)
        return receipt

    def verify(self, receipt):
        return self.http.post(f"/activity/{receipt['id']}/verify", params=self.scope)

    def test_dynamodb_content_is_bound_to_the_review_and_verified(self):
        self.apply(self.prepare("dynamodb.create", name="reviewed-items"))
        content = '{"id":"one","amount":0.125}'
        plan = self.prepare("dynamodb.put", id="reviewed-items", payload=content)
        self.assertEqual(plan["settings"]["item_key"], "one")
        url = f"/plans/{plan['id']}/apply"
        self.assertEqual(self.http.post(url, json={"payload": '{"id":"two"}'}).status_code, 409)
        receipt = self.apply(plan, payload=content)
        self.assertIs(receipt["can_verify"], True)
        self.assertEqual(self.apply(plan)["result"]["state"], "written")
        verified = self.verify(receipt)
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertIn("exact written item", verified.json()["result"]["verification"]["detail"])
        read = self.apply(self.prepare("dynamodb.get", id="reviewed-items", item_key="one"))
        self.assertEqual(json.loads(read["result"]["output"])["amount"], {"N": "0.125"})
        history = self.http.get("/activity", params=self.scope).json()["items"]
        self.assertNotIn("output", next(item for item in history if item["id"] == read["id"])["result"])

    def test_deployment_delete_requires_confirmation_and_removes_only_its_resources(self):
        plan = self.prepare("cdn.create", name="sandbox-static-files")
        receipt = self.apply(plan)
        stack_id = receipt["result"]["id"]
        detail = self.http.get("/deployments/detail", params={**self.scope, "stack_id": stack_id})
        self.assertEqual(detail.json()["Bucket"], "sandbox-static-files")
        self.assertEqual(self.verify(receipt).status_code, 200)
        deletion = self.prepare("deployment.delete", id=stack_id)
        url = f"/plans/{deletion['id']}/apply"
        self.assertEqual(self.http.post(url, json={"confirmation": "another-stack"}).status_code, 400)
        self.assertEqual(self.verify(self.apply(deletion, confirmation=stack_id)).status_code, 200)
        inventory = self.http.get("/inventory/s3", params=self.scope).json()["items"]
        self.assertEqual([item["id"] for item in inventory], ["hermes-demo-artifacts"])

    def test_queue_role_and_consumer_can_be_created_from_demo(self):
        self.apply(self.prepare("sqs.create", name="demo-queue"))
        self.apply(self.prepare("sqs.role", name="queue-role", id="demo-queue", function="process-event"))
        self.apply(self.prepare("sqs.trigger", name="queue-worker", id="demo-queue", function="process-event"))
        payload = '{"task":"one"}'
        self.apply(self.prepare("sqs.send", id="demo-queue", payload=payload), payload=payload)
        result = self.http.get("/services/automation/queues", params=self.scope).json()
        queue = next(item for item in result["items"] if item["id"] == "demo-queue")
        self.assertEqual(queue["facts"]["Consumer"], "process-event")
        self.assertEqual(queue["facts"]["Ready messages"], 1)

    def test_cloudformation_submission_waits_for_live_readback(self):
        self.scope["mode"] = "live"
        account = api.DEMO_ACCOUNT
        region = self.scope["region"]
        cfn = boto3.client("cloudformation", region_name=region, aws_access_key_id=uuid.uuid4().hex,
                           aws_secret_access_key=uuid.uuid4().hex)
        session = type("Session", (), {"client": lambda self, service, **kw: cfn})()
        identity = f"arn:aws:iam::{account}:role/test"
        with patch.object(api, "connection", return_value=(account, identity, session)), Stubber(cfn) as stub:
            plan = self.prepare("dynamodb.create", name="live-test-items")
            name = plan["settings"]["deployment"]
            stack_id = f"arn:aws:cloudformation:{region}:{account}:stack/{name}/{plan['id']}"
            stub.add_response("create_stack", {"StackId": stack_id}, {
                "StackName": name, "TemplateBody": ANY, "ClientRequestToken": plan["id"], "OnFailure": "ROLLBACK",
                "Tags": [{"Key": api.services.TAG, "Value": "true"},
                         {"Key": api.services.TAG + "Service", "Value": "databases"}],
            })
            receipt = self.apply(plan)
            self.assertIs(receipt["can_verify"], True)
            stack = {"StackId": stack_id, "StackName": name, "StackStatus": "CREATE_IN_PROGRESS",
                     "CreationTime": datetime.now(timezone.utc), "Tags": [{"Key": api.services.TAG, "Value": "true"}]}
            stub.add_response("describe_stacks", {"Stacks": [dict(stack)]}, {"StackName": stack_id})
            self.assertEqual(self.verify(receipt).status_code, 409)
            stub.add_response("describe_stacks", {"Stacks": [{**stack, "StackStatus": "CREATE_COMPLETE"}]}, {"StackName": stack_id})
            self.assertEqual(self.verify(receipt).json()["result"]["verification"]["detail"],
                             "CloudFormation reports CREATE_COMPLETE.")
            stub.assert_no_pending_responses()

    def test_scope_requires_review_and_rejects_unsupported_inventory(self):
        result = self.http.get("/services/apis/routes", params={**self.scope, "parent": "../secrets"})
        self.assertEqual(result.status_code, 400)
        result = self.http.post("/plans", json={"scope": self.scope, "action": "arbitrary.aws", "values": {}})
        self.assertEqual(result.status_code, 400)
        result = self.http.post("/plans/unknown/apply", json={})
        self.assertEqual(result.status_code, 404)


if __name__ == "__main__":
    unittest.main()
