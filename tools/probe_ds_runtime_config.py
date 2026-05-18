#!/usr/bin/env python3
"""Probe DolphinScheduler runtime values needed by country config.

This script is read-only. It lists environment and tenant candidates and
inspects configured recheck workflows for task-level environment codes.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config.config import DS_CONFIG, FUYAN_WORKFLOWS  # noqa: E402


DS_BASE = DS_CONFIG["base_url"].rstrip("/")
DS_TOKEN = DS_CONFIG["token"]
PROJECT_CODE = DS_CONFIG["project_code"]
FUYAN_PROJECT_CODE = DS_CONFIG["fuyan_project_code"]


def ds_api_get(endpoint: str):
    url = f"{DS_BASE}{endpoint}"
    req = urllib.request.Request(url)
    req.add_header("token", DS_TOKEN)
    req.add_header("Accept", "application/json, text/plain, */*")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            text = response.read().decode("utf-8")
    except Exception as exc:
        return False, None, str(exc)

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return False, None, f"non-json response: {text[:200]}"

    if result.get("code") != 0:
        return False, result.get("data"), result.get("msg", str(result))
    return True, result.get("data"), result.get("msg", "")


def compact_rows(rows, keys):
    output = []
    for row in rows or []:
        output.append({key: row.get(key) for key in keys if key in row})
    return output


def list_endpoint_candidates(name, endpoints, keys):
    attempts = []
    for endpoint in endpoints:
        success, data, msg = ds_api_get(endpoint)
        rows = []
        if isinstance(data, dict):
            rows = data.get("totalList") or data.get("list") or []
        elif isinstance(data, list):
            rows = data
        attempts.append(
            {
                "endpoint": endpoint,
                "success": success,
                "msg": msg,
                "count": len(rows),
                "items": compact_rows(rows, keys),
            }
        )
    return {name: attempts}


def workflow_detail(project_code, workflow_code):
    details = []
    for style in ("process-definition", "workflow-definition"):
        endpoint = f"/projects/{project_code}/{style}/{workflow_code}"
        success, data, msg = ds_api_get(endpoint)
        tasks = data.get("taskDefinitionList", []) if isinstance(data, dict) else []
        env_codes = sorted(
            {
                str(task.get("environmentCode"))
                for task in tasks
                if task.get("environmentCode") not in (None, "", -1)
            }
        )
        details.append(
            {
                "endpoint": endpoint,
                "success": success,
                "msg": msg,
                "task_count": len(tasks),
                "task_environment_codes": env_codes,
            }
        )
    return details


def inspect_recheck_workflows():
    workflows = []
    for workflow in FUYAN_WORKFLOWS:
        workflow_code = workflow.get("workflow_code") or workflow.get("code")
        workflow_name = workflow.get("workflow_name") or workflow.get("name")
        project_code = workflow.get("project_code") or FUYAN_PROJECT_CODE
        workflows.append(
            {
                "workflow_name": workflow_name,
                "workflow_code": workflow_code,
                "project_code": project_code,
                "details": workflow_detail(project_code, workflow_code),
            }
        )
    return workflows


def search_project_by_name(project_name):
    endpoint = f"/projects?pageNo=1&pageSize=50&searchVal={quote(project_name)}"
    success, data, msg = ds_api_get(endpoint)
    rows = data.get("totalList", []) if isinstance(data, dict) else []
    return {
        "endpoint": endpoint,
        "success": success,
        "msg": msg,
        "items": compact_rows(rows, ["id", "code", "name", "userName", "description"]),
    }


def main():
    report = {
        "base_url": DS_BASE,
        "project_code": PROJECT_CODE,
        "fuyan_project_code": FUYAN_PROJECT_CODE,
    }
    report.update(
        list_endpoint_candidates(
            "environment_candidates",
            [
                "/environment/list-paging?pageNo=1&pageSize=200&searchVal=",
                "/environment/list",
            ],
            ["id", "code", "name", "config", "description", "workerGroups"],
        )
    )
    report.update(
        list_endpoint_candidates(
            "tenant_candidates",
            [
                "/tenants/list-paging?pageNo=1&pageSize=200&searchVal=",
                "/tenants/list",
                "/tenant/list-paging?pageNo=1&pageSize=200&searchVal=",
                "/tenant/list",
            ],
            ["id", "tenantCode", "tenantName", "description", "queueName"],
        )
    )
    report["fuyan_project_search"] = search_project_by_name(DS_CONFIG["fuyan_project_name"])
    report["recheck_workflow_task_environment_codes"] = inspect_recheck_workflows()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
