import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APIFY_API = "https://api.apify.com/v2"
ACTOR_ID = os.environ.get("APIFY_ACTOR_ID", "QJX8h1Odtl2o8RdVq")
ALLOWED_ACTOR_IDS = {ACTOR_ID, "QJX8h1Odtl2o8RdVq", "M0aiz7xLf5Z0iqOi4"}
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
CONTROL_API_KEY = os.environ.get("CONTROL_API_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY_BYTES = 25 * 1024 * 1024

# Public GitHub issue used as a secret-free command bus.
CONTROL_REPO = os.environ.get("CONTROL_REPO", "metatron9911-star/catalogfix-ai")
CONTROL_ISSUE = int(os.environ.get("CONTROL_ISSUE", "1"))
CONTROL_OWNER = os.environ.get("CONTROL_OWNER", "metatron9911-star")
CONTROL_PREFIX = "CATALOGFIX_CONTROL "
COMMAND_MAX_AGE_SECONDS = 900
CONTROL_POLL_SECONDS = 15
CONTROL_COMMAND_URL = f"https://raw.githubusercontent.com/{CONTROL_REPO}/main/control_bridge/command.json"

_state_lock = threading.Lock()
_control_state = {
    "ready": False,
    "lastCommentId": None,
    "lastCommand": None,
    "lastStatus": None,
    "lastMessage": "Bridge started; waiting for control queue.",
    "queuePollWarning": None,
    "queuePollWarningAt": None,
    "updatedAt": None,
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _apify(path, method="GET", body=None, content_type="application/json"):
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN is not configured")
    data = None
    headers = {
        "Authorization": f"Bearer {APIFY_TOKEN}",
        "User-Agent": "CatalogFix-Control/1.1",
    }
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type
        elif isinstance(body, bytes):
            data = body
            headers["Content-Type"] = content_type
        else:
            data = str(body).encode("utf-8")
            headers["Content-Type"] = content_type
    req = urllib.request.Request(APIFY_API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                return resp.status, json.loads(raw.decode("utf-8") or "{}"), ctype
            return resp.status, raw, ctype
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        ctype = exc.headers.get("content-type", "")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {"error": raw.decode("utf-8", "replace")[:4000]}
        return exc.code, payload, ctype


def _github_comments():
    owner, repo = CONTROL_REPO.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{CONTROL_ISSUE}/comments?per_page=100"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "CatalogFix-Control/1.1",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "[]")


def _safe_run_id(value):
    return value if re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value or "") else None


def _safe_public_catalog_input(payload):
    actor_input = payload.get("input", payload)
    if not isinstance(actor_input, dict):
        raise ValueError("run input must be a JSON object")
    source = actor_input.get("catalogFile")
    if source:
        parsed = urllib.parse.urlparse(str(source))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("catalogFile must be a public HTTP(S) URL for issue-queue runs")
        # The GitHub issue queue is public. Never allow signed/private URLs here.
        if parsed.query or parsed.fragment or "token" in str(source).lower():
            raise ValueError("private/signed catalog URLs are not allowed in the public control queue")
    return actor_input


def _brief_apify_result(action, code, payload):
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if action == "status":
        return {
            "httpStatus": code,
            "actor": {
                "id": data.get("id"),
                "name": data.get("name"),
                "title": data.get("title"),
                "username": data.get("username"),
                "modifiedAt": data.get("modifiedAt"),
                "isPublic": data.get("isPublic"),
                "isStoreActor": data.get("isStoreActor"),
                "pricingInfos": data.get("pricingInfos"),
                "defaultRunOptions": data.get("defaultRunOptions"),
                "categories": data.get("categories"),
            },
        }
    if action in {"build", "build-status"}:
        return {
            "httpStatus": code,
            "build": {
                "id": data.get("id"),
                "status": data.get("status"),
                "buildNumber": data.get("buildNumber"),
                "versionNumber": data.get("versionNumber"),
                "startedAt": data.get("startedAt"),
                "finishedAt": data.get("finishedAt"),
            },
        }
    if action in {"run", "run-status", "abort"}:
        return {
            "httpStatus": code,
            "run": {
                "id": data.get("id"),
                "status": data.get("status"),
                "startedAt": data.get("startedAt"),
                "finishedAt": data.get("finishedAt"),
                "buildNumber": data.get("buildNumber"),
            },
        }
    return {"httpStatus": code}


def _execute_queue_command(command):
    action = str(command.get("action", "")).lower().strip()
    target_actor_id = str(command.get("targetActorId") or ACTOR_ID).strip()
    if target_actor_id not in ALLOWED_ACTOR_IDS:
        raise ValueError("targetActorId is not allowed")

    if action == "status":
        code, data, _ = _apify(f"/acts/{target_actor_id}")
        return action, _brief_apify_result(action, code, data)

    if action == "actor-update":
        allowed = {"title", "description", "seoTitle", "seoDescription", "categories", "actorPermissionLevel", "defaultRunOptions", "exampleRunInput", "pricingInfos", "isPublic", "pictureUrl", "notice"}
        payload = {k: v for k, v in command.items() if k in allowed}
        if not payload:
            raise ValueError("no allowed Actor fields supplied")
        code, data, _ = _apify(f"/acts/{target_actor_id}", method="PUT", body=payload)
        return action, _brief_apify_result("status", code, data)

    if action == "task-list":
        code, data, _ = _apify("/actor-tasks?limit=1000")
        items = ((data.get("data") or {}).get("items") or []) if isinstance(data, dict) else []
        tasks = []
        for item in items:
            if item.get("actId") != ACTOR_ID:
                continue
            task_id = item.get("id")
            detail_code, detail, _ = _apify(f"/actor-tasks/{task_id}")
            task = (detail.get("data") or {}) if isinstance(detail, dict) else {}
            tasks.append({
                "id": task.get("id") or task_id,
                "name": task.get("name") or item.get("name"),
                "title": task.get("title"),
                "description": task.get("description"),
                "isPublic": task.get("isPublic"),
                "publicConfig": task.get("publicConfig"),
                "input": task.get("input"),
                "httpStatus": detail_code,
            })
        return action, {"httpStatus": code, "tasks": tasks}

    if action == "task-upsert":
        spec = command.get("task")
        if not isinstance(spec, dict):
            raise ValueError("task object is required")
        name = str(spec.get("name") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name):
            raise ValueError("task.name must be a lowercase slug")
        title = str(spec.get("title") or "").strip()
        description = str(spec.get("description") or "").strip()
        actor_input = spec.get("input")
        public_config = spec.get("publicConfig")
        if not (3 <= len(title) <= 63):
            raise ValueError("task.title must be 3..63 characters")
        if not description or len(description) > 400:
            raise ValueError("task.description must be 1..400 characters")
        if not isinstance(actor_input, dict):
            raise ValueError("task.input must be an object")
        if not isinstance(public_config, dict):
            raise ValueError("task.publicConfig must be an object")

        code, data, _ = _apify("/actor-tasks?limit=1000")
        items = ((data.get("data") or {}).get("items") or []) if isinstance(data, dict) else []
        existing = next((x for x in items if x.get("actId") == ACTOR_ID and x.get("name") == name), None)

        if existing:
            task_id = existing.get("id")
            payload = {
                "title": title,
                "description": description,
                "input": actor_input,
                "publicConfig": public_config,
            }
            if "options" in spec:
                payload["options"] = spec.get("options")
            if "isPublic" in spec:
                payload["isPublic"] = bool(spec.get("isPublic"))
            update_code, updated, _ = _apify(f"/actor-tasks/{task_id}", method="PUT", body=payload)
            task = (updated.get("data") or {}) if isinstance(updated, dict) else {}
            return action, {
                "httpStatus": update_code,
                "created": False,
                "task": {
                    "id": task.get("id") or task_id,
                    "name": task.get("name") or name,
                    "title": task.get("title") or title,
                    "isPublic": task.get("isPublic"),
                    "publicConfig": task.get("publicConfig"),
                },
            }

        create_payload = {
            "actId": ACTOR_ID,
            "name": name,
            "title": title,
            "description": description,
            "input": actor_input,
        }
        if "options" in spec:
            create_payload["options"] = spec.get("options")
        create_code, created, _ = _apify("/actor-tasks", method="POST", body=create_payload)
        task = (created.get("data") or {}) if isinstance(created, dict) else {}
        task_id = task.get("id")
        if create_code not in {200, 201} or not task_id:
            return action, {"httpStatus": create_code, "created": False, "error": created}

        update_payload = {"publicConfig": public_config}
        if "isPublic" in spec:
            update_payload["isPublic"] = bool(spec.get("isPublic"))
        update_code, updated, _ = _apify(f"/actor-tasks/{task_id}", method="PUT", body=update_payload)
        task2 = (updated.get("data") or {}) if isinstance(updated, dict) else {}
        return action, {
            "httpStatus": update_code,
            "created": True,
            "task": {
                "id": task2.get("id") or task_id,
                "name": task2.get("name") or name,
                "title": task2.get("title") or title,
                "isPublic": task2.get("isPublic"),
                "publicConfig": task2.get("publicConfig"),
            },
        }

    if action == "build":
        version = str(command.get("version") or "0.0")
        tag = str(command.get("tag") or "latest")
        query = urllib.parse.urlencode({
            "version": version,
            "tag": tag,
            "useCache": "1" if command.get("useCache", True) else "0",
            "waitForFinish": 0,
        })
        code, data, _ = _apify(f"/acts/{ACTOR_ID}/builds?{query}", method="POST", body={})
        return action, _brief_apify_result(action, code, data)

    if action == "build-status":
        build_id = _safe_run_id(str(command.get("buildId", "")))
        if not build_id:
            raise ValueError("valid buildId is required")
        code, data, _ = _apify(f"/actor-builds/{build_id}")
        return action, _brief_apify_result(action, code, data)

    if action == "run-status":
        run_id = _safe_run_id(str(command.get("runId", "")))
        if not run_id:
            raise ValueError("valid runId is required")
        code, data, _ = _apify(f"/actor-runs/{run_id}")
        return action, _brief_apify_result(action, code, data)

    if action == "run":
        actor_input = _safe_public_catalog_input(command)
        opts = {}
        if "memory" in command:
            opts["memory"] = int(command["memory"])
        if "timeout" in command:
            opts["timeout"] = int(command["timeout"])
        if "build" in command:
            opts["build"] = str(command["build"])
        suffix = ("?" + urllib.parse.urlencode(opts)) if opts else ""
        code, data, _ = _apify(f"/acts/{ACTOR_ID}/runs{suffix}", method="POST", body=actor_input)
        return action, _brief_apify_result(action, code, data)

    if action == "abort":
        run_id = _safe_run_id(str(command.get("runId", "")))
        if not run_id:
            raise ValueError("valid runId is required")
        code, data, _ = _apify(f"/actor-runs/{run_id}/abort", method="POST", body={})
        return action, _brief_apify_result(action, code, data)

    if action == "run-records":
        run_id = _safe_run_id(str(command.get("runId", "")))
        if not run_id:
            raise ValueError("valid runId is required")
        code, run, _ = _apify(f"/actor-runs/{run_id}")
        if code != 200:
            return action, {"httpStatus": code}
        dataset_id = (run.get("data") or {}).get("defaultDatasetId")
        if not dataset_id:
            return action, {"httpStatus": 404, "error": "run has no dataset"}
        limit = min(max(int(command.get("limit", 200)), 1), 500)
        code, payload, _ = _apify(f"/datasets/{dataset_id}/items?clean=1&format=json&limit={limit}")
        if isinstance(payload, bytes):
            payload = json.loads(payload.decode("utf-8") or "[]")
        skus = {str(x).upper() for x in (command.get("skus") or [])}
        rows = payload if isinstance(payload, list) else []
        if skus:
            rows = [r for r in rows if str(r.get("sku", "")).upper() in skus]
        safe = []
        keep = {"sku","title","price","category","brand","qa_status","quality_confidence","quality_flags","visual_confidence","import_method","supplier_code","currency","recordType","source_page","source_row"}
        for r in rows[:100]:
            safe.append({k:r.get(k) for k in keep if k in r})
        return action, {"httpStatus": code, "rows": safe}

    if action == "run-log-tail":
        run_id = _safe_run_id(str(command.get("runId", "")))
        if not run_id:
            raise ValueError("valid runId is required")
        code, logs, _ = _apify(f"/logs/{run_id}")
        if isinstance(logs, bytes):
            text_logs = logs.decode("utf-8", "replace")
        else:
            text_logs = json.dumps(logs, ensure_ascii=False, default=str)
        return action, {"httpStatus": code, "tail": text_logs[-5000:]}

    if action == "run-summary":
        run_id = _safe_run_id(str(command.get("runId", "")))
        if not run_id:
            raise ValueError("valid runId is required")
        code, run, _ = _apify(f"/actor-runs/{run_id}")
        if code != 200:
            return action, {"httpStatus": code}
        kv_id = (run.get("data") or {}).get("defaultKeyValueStoreId")
        if not kv_id:
            return action, {"httpStatus": 404, "error": "run has no key-value store"}
        code, summary, _ = _apify(f"/key-value-stores/{kv_id}/records/SUMMARY.json")
        if isinstance(summary, bytes):
            try:
                summary = json.loads(summary.decode("utf-8"))
            except Exception:
                summary = {"raw": summary.decode("utf-8", "replace")[:2000]}
        return action, {"httpStatus": code, "summary": summary}

    raise ValueError("unsupported action; allowed: status, actor-update, task-list, task-upsert, build, build-status, run, run-status, run-records, run-log-tail, run-summary, abort")


def _set_state(**kwargs):
    with _state_lock:
        _control_state.update(kwargs)
        _control_state["updatedAt"] = _now_iso()


def _startup_command():
    """Execute the checked-in command once at container start.

    Updating control_bridge/command.json triggers a fresh Railway deployment for
    this service, so no external polling or secret-bearing client is required.
    """
    path = os.path.join(os.path.dirname(__file__), "command.json")
    _set_state(ready=True, lastMessage="Bridge ready; loading startup command.")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            command = json.load(fh)
        command_id = str(command.get("id") or "").strip()
        if not command_id:
            return
        action, result = _execute_queue_command(command)
        _set_state(
            lastCommentId=command_id,
            lastCommand=action,
            lastStatus="OK",
            lastMessage="Command completed",
            queuePollWarning=None,
            queuePollWarningAt=None,
            result=result,
        )
        print("CONTROL_RESULT " + json.dumps({
            "id": command_id,
            "action": action,
            "status": "OK",
            "result": result,
        }, ensure_ascii=False, default=str), flush=True)
    except Exception as exc:
        _set_state(
            lastStatus="ERROR",
            lastMessage=f"{type(exc).__name__}: {str(exc)[:500]}",
            result=None,
        )
        print("CONTROL_RESULT " + json.dumps({
            "status": "ERROR",
            "errorType": type(exc).__name__,
            "message": str(exc)[:500],
        }, ensure_ascii=False), flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "CatalogFixControl/1.3"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _bytes(self, status, payload, ctype="application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self):
        if not CONTROL_API_KEY:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "CONTROL_API_KEY is not configured"})
            return False
        expected = f"Bearer {CONTROL_API_KEY}"
        if self.headers.get("Authorization", "") != expected:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        return True

    def _body_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/health":
            return self._json(200, {
                "ok": True,
                "service": "catalogfix-apify-control",
                "actorId": ACTOR_ID,
                "apifyTokenConfigured": bool(APIFY_TOKEN),
                "controlKeyConfigured": bool(CONTROL_API_KEY),
                "queueReady": bool(_control_state.get("ready")),
            })

        if path == "/control-status":
            with _state_lock:
                public_state = dict(_control_state)
            return self._json(200, public_state)

        if not self._authorized():
            return

        if path == "/v1/status":
            code, data, _ = _apify(f"/acts/{ACTOR_ID}")
            return self._json(code, data)

        if path == "/v1/runs":
            query = ("?" + parsed.query) if parsed.query else "?limit=20&desc=1"
            code, data, _ = _apify(f"/acts/{ACTOR_ID}/runs{query}")
            return self._json(code, data)

        m = re.fullmatch(r"/v1/runs/([^/]+)", path)
        if m:
            run_id = _safe_run_id(m.group(1))
            if not run_id:
                return self._json(400, {"error": "invalid run id"})
            code, data, _ = _apify(f"/actor-runs/{run_id}")
            return self._json(code, data)

        m = re.fullmatch(r"/v1/runs/([^/]+)/logs", path)
        if m:
            run_id = _safe_run_id(m.group(1))
            if not run_id:
                return self._json(400, {"error": "invalid run id"})
            code, data, ctype = _apify(f"/logs/{run_id}")
            if isinstance(data, bytes):
                return self._bytes(code, data, ctype or "text/plain; charset=utf-8")
            return self._json(code, data)

        m = re.fullmatch(r"/v1/runs/([^/]+)/summary", path)
        if m:
            run_id = _safe_run_id(m.group(1))
            if not run_id:
                return self._json(400, {"error": "invalid run id"})
            code, run, _ = _apify(f"/actor-runs/{run_id}")
            if code != 200:
                return self._json(code, run)
            kv_id = (run.get("data") or {}).get("defaultKeyValueStoreId")
            if not kv_id:
                return self._json(404, {"error": "run has no default key-value store"})
            code, data, ctype = _apify(f"/key-value-stores/{kv_id}/records/SUMMARY.json")
            if isinstance(data, bytes):
                return self._bytes(code, data, ctype or "application/json")
            return self._json(code, data)

        return self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._authorized():
            return

        if path == "/v1/build":
            payload = self._body_json()
            version = str(payload.get("version") or "0.0")
            tag = str(payload.get("tag") or "latest")
            use_cache = "1" if payload.get("useCache", True) else "0"
            query = urllib.parse.urlencode({
                "version": version,
                "tag": tag,
                "useCache": use_cache,
                "waitForFinish": 0,
            })
            code, data, _ = _apify(f"/acts/{ACTOR_ID}/builds?{query}", method="POST", body={})
            return self._json(code, data)

        if path == "/v1/run":
            payload = self._body_json()
            actor_input = payload.get("input", payload)
            opts = {}
            if "memory" in payload:
                opts["memory"] = int(payload["memory"])
            if "timeout" in payload:
                opts["timeout"] = int(payload["timeout"])
            if "build" in payload:
                opts["build"] = str(payload["build"])
            suffix = ("?" + urllib.parse.urlencode(opts)) if opts else ""
            code, data, _ = _apify(f"/acts/{ACTOR_ID}/runs{suffix}", method="POST", body=actor_input)
            return self._json(code, data)

        m = re.fullmatch(r"/v1/runs/([^/]+)/abort", path)
        if m:
            run_id = _safe_run_id(m.group(1))
            if not run_id:
                return self._json(400, {"error": "invalid run id"})
            code, data, _ = _apify(f"/actor-runs/{run_id}/abort", method="POST", body={})
            return self._json(code, data)

        return self._json(404, {"error": "not found"})


if __name__ == "__main__":
    if not APIFY_TOKEN:
        raise SystemExit("APIFY_TOKEN is required")
    if not CONTROL_API_KEY:
        raise SystemExit("CONTROL_API_KEY is required")
    _startup_command()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"CatalogFix Apify control listening on :{PORT} for actor {ACTOR_ID}", flush=True)
    server.serve_forever()
