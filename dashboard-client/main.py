"""
main.py — Shadow-Lab Control Plane: Local Backend
==========================================
Runs on your local machine.
Proxies management calls to remote server agents and serves the web UI.

All routes under /api/* talk to remote agents.
GET / serves the single-page dashboard HTML.
"""
import sys
import logging
import asyncio
from pathlib import Path
import httpx
import requests
import json
import threading

import db
import deploy
import server_lifecycle
import system_control
import compatibility

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DASHBOARD_PORT = 7860
STATIC_DIR = Path(__file__).parent / "static"

db.init_db()

def _agent_headers(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}

# --- RUNTIME DETECTION ---
USE_FASTAPI = False
try:
    import fastapi
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
    import pydantic
    
    if pydantic.__version__.startswith('2.'):
        log.warning("Pydantic v2 detected. FastAPI mode requires Pydantic v1.x or zero Pydantic build errors.")
        USE_FASTAPI = False
    else:
        from pydantic import BaseModel
        USE_FASTAPI = True
except ImportError as e:
    USE_FASTAPI = False
    log.warning(f"FastAPI disabled due to environment limitations ({e}).")

if not USE_FASTAPI:
    log.info(f"Detected Python version: {sys.version.split()[0]}")
    log.info("Running in compatibility mode")
    log.info("Using Flask fallback")


if USE_FASTAPI:
    app = FastAPI(title="Shadow-Lab Control Plane", docs_url="/api/docs")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    _http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))

    async def _proxy_get(host: str, token: str, path: str):
        try:
            r = await _http.get(f"{host}{path}", headers=_agent_headers(token))
            return r.status_code, r.json()
        except httpx.ConnectError:
            return 503, {"error": "Agent unreachable"}
        except Exception as e:
            return 500, {"error": str(e)}

    async def _proxy_post(host: str, token: str, path: str, body: dict):
        try:
            r = await _http.post(f"{host}{path}", json=body, headers=_agent_headers(token))
            return r.status_code, r.json()
        except httpx.ConnectError:
            return 503, {"error": "Agent unreachable"}
        except Exception as e:
            return 500, {"error": str(e)}

    async def _proxy_delete(host: str, token: str, path: str, body: dict):
        try:
            r = await _http.request("DELETE", f"{host}{path}", json=body, headers=_agent_headers(token))
            return r.status_code, r.json()
        except httpx.ConnectError:
            return 503, {"error": "Agent unreachable"}
        except Exception as e:
            return 500, {"error": str(e)}

    @app.get("/", response_class=HTMLResponse)
    async def serve_ui():
        html_file = STATIC_DIR / "index.html"
        return HTMLResponse(content=html_file.read_text())

    class AddServerRequest(BaseModel):
        name: str
        host: str
        admin_token: str
        notes: str = ""

    class UpdateServerRequest(BaseModel):
        name: str
        host: str
        admin_token: str
        notes: str = ""

    @app.get("/api/servers")
    async def api_list_servers():
        servers = db.list_servers()
        for s in servers:
            s["admin_token"] = "***"
        return servers

    @app.post("/api/servers")
    async def api_add_server(body: AddServerRequest):
        server = db.add_server(body.name, body.host, body.admin_token, body.notes)
        return server

    @app.put("/api/servers/{server_id}")
    async def api_update_server(server_id: int, body: UpdateServerRequest):
        server = db.update_server(server_id, body.name, body.host, body.admin_token, body.notes)
        if not server:
            raise HTTPException(404, "Server not found")
        return server

    @app.delete("/api/servers/{server_id}")
    async def api_remove_server(server_id: int):
        ok = db.remove_server(server_id)
        if not ok:
            raise HTTPException(404, "Server not found")
        return {"status": "removed"}

    @app.get("/api/servers/{server_id}/status")
    async def api_server_status(server_id: int):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")

        status_code, health = await _proxy_get(server["host"], server["admin_token"], "/v1/health")
        metrics_code, metrics_data = await _proxy_get(server["host"], server["admin_token"], "/admin/metrics")
        models_code, models_data = await _proxy_get(server["host"], server["admin_token"], "/admin/models")

        state = "offline"
        if status_code == 200:
            db.update_server_seen(server_id)
            if metrics_data and not metrics_data.get("ollama_running"):
                state = "idle"
            else:
                state = "online"

        return {
            "online": status_code == 200,
            "state": state,
            "health": health if status_code == 200 else None,
            "metrics": metrics_data if metrics_code == 200 else None,
            "models": models_data.get("models", []) if models_code == 200 else [],
        }

    class PullModelBody(BaseModel):
        name: str

    class DeleteModelBody(BaseModel):
        name: str

    @app.post("/api/servers/{server_id}/models/pull")
    async def api_pull_model(server_id: int, body: PullModelBody):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        async def do_pull():
            try:
                async with _http.stream(
                    "POST",
                    f"{server['host']}/admin/models/pull",
                    json={"name": body.name},
                    headers=_agent_headers(server["admin_token"]),
                    timeout=600,
                ) as r:
                    async for _ in r.aiter_lines():
                        pass
            except Exception as e:
                log.error(f"Pull error: {e}")

        asyncio.create_task(do_pull())
        return {"status": "pulling", "model": body.name, "note": "Poll /status to track progress"}

    @app.delete("/api/servers/{server_id}/models")
    async def api_delete_model(server_id: int, body: DeleteModelBody):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_delete(server["host"], server["admin_token"], "/admin/models", {"name": body.name})
        if code != 200:
            raise HTTPException(code, data.get("error", "Failed"))
        return data

    @app.post("/api/servers/{server_id}/models/{action}")
    async def api_model_actions(server_id: int, action: str, body: dict):
        if action not in ["select", "load", "unload"]:
            raise HTTPException(400, "Invalid action")
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_post(server["host"], server["admin_token"], f"/admin/models/{action}", body)
        if code != 200:
            raise HTTPException(code, data.get("error", "Failed"))
        return data

    @app.post("/api/servers/{server_id}/keys/test")
    async def api_test_api_key(server_id: int, body: dict):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_post(server["host"], server["admin_token"], "/admin/test-api-key", body)
        if code != 200:
            raise HTTPException(code, data.get("error", "Failed"))
        return data

    class CreateKeyBody(BaseModel):
        label: str = ""
        limit_rpm: int = 0

    class RevokeKeyBody(BaseModel):
        key: str

    class DeleteKeyBody(BaseModel):
        key: str

    @app.get("/api/servers/{server_id}/keys")
    async def api_list_keys(server_id: int):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_get(server["host"], server["admin_token"], "/admin/keys")
        if code == 200:
            return data
        return db.list_cached_keys(server_id)

    @app.post("/api/servers/{server_id}/keys")
    async def api_create_key(server_id: int, body: CreateKeyBody):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_post(
            server["host"], server["admin_token"], "/admin/keys",
            {"label": body.label, "limit_rpm": body.limit_rpm}
        )
        if code != 200:
            raise HTTPException(code, data.get("error", "Failed to create key"))
        db.cache_key(server_id, data["key"], data.get("label", ""), data.get("created_at", ""))
        return data

    @app.post("/api/servers/{server_id}/keys/revoke")
    async def api_revoke_key(server_id: int, body: RevokeKeyBody):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_post(
            server["host"], server["admin_token"], "/admin/keys/revoke", {"key": body.key}
        )
        if code != 200:
            raise HTTPException(code, data.get("error", "Failed"))
        return data

    @app.delete("/api/servers/{server_id}/keys")
    async def api_delete_key(server_id: int, body: DeleteKeyBody):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_delete(
            server["host"], server["admin_token"], "/admin/keys", {"key": body.key}
        )
        if code != 200:
            raise HTTPException(code, data.get("error", "Failed"))
        db.remove_cached_key(server_id, body.key)
        return data

    @app.post("/api/deploy/start")
    async def api_deploy_start(body: dict):
        host = body.get("host")
        port = int(body.get("port", 22))
        username = body.get("username")
        password = body.get("password")
        server_name = body.get("server_name", host)
        if not host or not username:
            raise HTTPException(400, "Host and username are required")

        deploy_id = deploy.start_deployment(
            host, port, username, password=password, server_name=server_name
        )
        return {"deploy_id": deploy_id}

    @app.get("/api/deploy/status/{deploy_id}")
    async def api_deploy_status(deploy_id: str):
        st = deploy.active_deployments.get(deploy_id)
        if not st:
            raise HTTPException(404, "Deployment not found")
        return st

    # ── Control Plane: Model & API Testing ─────────────────────────────────

    @app.post("/api/servers/{server_id}/test-model")
    async def api_test_model(server_id: int, body: dict):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_post(
            server["host"], server["admin_token"],
            "/admin/test-model", {"name": body.get("name", ""), "prompt": body.get("prompt", "")}
        )
        return JSONResponse(content=data, status_code=code)

    @app.get("/api/servers/{server_id}/models/health")
    async def api_models_health(server_id: int):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_get(server["host"], server["admin_token"], "/admin/models/health")
        return JSONResponse(content=data, status_code=code)

    @app.post("/api/servers/{server_id}/test-api")
    async def api_test_api(server_id: int, body: dict):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_post(
            server["host"], server["admin_token"],
            "/admin/test-api", {"model": body.get("model", "")}
        )
        return JSONResponse(content=data, status_code=code)

    # ── Control Plane: Uptime & Runtime ─────────────────────────────────────

    @app.get("/api/servers/{server_id}/uptime")
    async def api_server_uptime(server_id: int):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_get(server["host"], server["admin_token"], "/metrics/uptime")
        if code == 200 and data:
            db.record_uptime_snapshot(
                server_id,
                data.get("current_session_seconds", 0),
                0,
            )
        return JSONResponse(content=data, status_code=code)

    @app.get("/api/servers/{server_id}/monthly-runtime")
    async def api_monthly_runtime(server_id: int):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_get(server["host"], server["admin_token"], "/metrics/monthly-runtime")
        return JSONResponse(content=data, status_code=code)

    # ── Control Plane: Lifecycle ─────────────────────────────────────────────

    @app.post("/api/servers/{server_id}/lifecycle/{action}")
    async def api_lifecycle(server_id: int, action: str):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")

        if action == "restart-ollama":
            result = await server_lifecycle.restart_ollama(server)
        elif action == "restart-agent":
            result = await server_lifecycle.restart_agent(server)
        elif action == "stop-agent":
            result = await server_lifecycle.stop_agent(server)
        elif action == "start-agent":
            result = await server_lifecycle.start_agent(server)
        elif action == "start-ai":
            result = await system_control.start_ai(server)
        elif action == "stop-ai":
            result = await system_control.stop_ai(server)
        elif action == "restart-ai":
            result = await system_control.restart_ai(server)
        elif action == "shutdown":
            # Optional force parameter check via query string if we want, but we'll accept it from json body
            # Wait, api_lifecycle is a POST, we could extract force flag. 
            # We'll default to forced here or assume the UI handles confirmation. 
            # To be safe, we'll pass force=True since the UI will have a giant warning modal.
            result = await system_control.shutdown_machine(server, force=True)
        elif action == "reboot":
            result = await system_control.reboot_machine(server, force=True)
        elif action == "idle":
            result = await system_control.sleep_mode(server)
        elif action == "deactivate":
            result = await system_control.deactivate_server(server, force=True)
        elif action == "activate":
            result = await system_control.activate_server(server)
        elif action == "health":
            result = await server_lifecycle.check_server_health(server)
        else:
            raise HTTPException(400, f"Unknown action: {action}")

        return result

    # ── Control Plane: Logs ──────────────────────────────────────────────────

    @app.get("/api/servers/{server_id}/logs/recent")
    async def api_logs_recent(server_id: int, limit: int = 100):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_get(
            server["host"], server["admin_token"], f"/logs/recent?limit={limit}"
        )
        return JSONResponse(content=data, status_code=code)

    @app.get("/api/servers/{server_id}/logs/errors")
    async def api_logs_errors(server_id: int, limit: int = 50):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        code, data = await _proxy_get(
            server["host"], server["admin_token"], f"/logs/errors?limit={limit}"
        )
        return JSONResponse(content=data, status_code=code)

    # ── Control Plane: Compatibility ─────────────────────────────────────────

    @app.get("/api/servers/{server_id}/compatibility")
    async def api_compatibility(server_id: int):
        server = db.get_server(server_id)
        if not server:
            raise HTTPException(404, "Server not found")
        report = compatibility.full_compatibility_report(server)
        return report

else:
    # ── Flask Fallback Implementation ──────────────────────────────────────────
    from flask import Flask, request, jsonify, make_response
    from flask_cors import CORS
    
    app = Flask(__name__)
    CORS(app)

    def _proxy_get(host: str, token: str, path: str):
        try:
            r = requests.get(f"{host}{path}", headers=_agent_headers(token), timeout=5)
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, {"warning": "Non-JSON response", "text": r.text}
        except requests.exceptions.RequestException:
            return 503, {"error": "Agent unreachable"}
        except Exception as e:
            return 500, {"error": str(e)}

    def _proxy_post(host: str, token: str, path: str, body: dict):
        try:
            r = requests.post(f"{host}{path}", json=body, headers=_agent_headers(token), timeout=5)
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, {"warning": "Non-JSON response", "text": r.text}
        except requests.exceptions.RequestException:
            return 503, {"error": "Agent unreachable"}
        except Exception as e:
            return 500, {"error": str(e)}

    def _proxy_delete(host: str, token: str, path: str, body: dict):
        try:
            r = requests.delete(f"{host}{path}", json=body, headers=_agent_headers(token), timeout=5)
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, {"warning": "Non-JSON response", "text": r.text}
        except requests.exceptions.RequestException:
            return 503, {"error": "Agent unreachable"}
        except Exception as e:
            return 500, {"error": str(e)}

    @app.route("/", methods=["GET"])
    def serve_ui():
        html_file = STATIC_DIR / "index.html"
        return html_file.read_text(), 200, {"Content-Type": "text/html"}

    @app.route("/api/servers", methods=["GET"])
    def api_list_servers():
        servers = db.list_servers()
        for s in servers:
            s["admin_token"] = "***"
        return jsonify(servers)

    @app.route("/api/servers", methods=["POST"])
    def api_add_server():
        body = request.get_json()
        server = db.add_server(body.get("name"), body.get("host"), body.get("admin_token"), body.get("notes", ""))
        return jsonify(server)

    @app.route("/api/servers/<int:server_id>", methods=["PUT"])
    def api_update_server(server_id):
        body = request.get_json()
        server = db.update_server(server_id, body.get("name"), body.get("host"), body.get("admin_token"), body.get("notes", ""))
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        return jsonify(server)

    @app.route("/api/servers/<int:server_id>", methods=["DELETE"])
    def api_remove_server(server_id):
        ok = db.remove_server(server_id)
        if not ok:
            return jsonify({"detail": "Server not found"}), 404
        return jsonify({"status": "removed"})

    @app.route("/api/servers/<int:server_id>/status", methods=["GET"])
    def api_server_status(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404

        status_code, health = _proxy_get(server["host"], server["admin_token"], "/v1/health")
        metrics_code, metrics_data = _proxy_get(server["host"], server["admin_token"], "/admin/metrics")
        models_code, models_data = _proxy_get(server["host"], server["admin_token"], "/admin/models")

        state = "offline"
        if status_code == 200:
            db.update_server_seen(server_id)
            if metrics_data and not metrics_data.get("ollama_running"):
                state = "idle"
            else:
                state = "online"

        return jsonify({
            "online": status_code == 200,
            "state": state,
            "health": health if status_code == 200 else None,
            "metrics": metrics_data if metrics_code == 200 else None,
            "models": models_data.get("models", []) if models_code == 200 else [],
        })

    @app.route("/api/servers/<int:server_id>/models/pull", methods=["POST"])
    def api_pull_model(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
            
        body = request.get_json()
        model_name = body.get("name")
        
        def do_pull():
            try:
                requests.post(
                    f"{server['host']}/admin/models/pull",
                    json={"name": model_name},
                    headers=_agent_headers(server["admin_token"]),
                    timeout=600
                )
            except Exception as e:
                log.error(f"Pull error: {e}")

        threading.Thread(target=do_pull, daemon=True).start()
        return jsonify({"status": "pulling", "model": model_name, "note": "Poll /status to track progress"})

    @app.route("/api/servers/<int:server_id>/models", methods=["DELETE"])
    def api_delete_model(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        
        body = request.get_json()
        code, data = _proxy_delete(server["host"], server["admin_token"], "/admin/models", {"name": body.get("name")})
        if code != 200:
            return jsonify({"detail": data.get("error", "Failed")}), code
        return jsonify(data)

    @app.route("/api/servers/<int:server_id>/models/<action>", methods=["POST"])
    def api_model_actions(server_id, action):
        if action not in ["select", "load", "unload"]:
            return jsonify({"detail": "Invalid action"}), 400
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        body = request.json or {}
        code, data = _proxy_post(server["host"], server["admin_token"], f"/admin/models/{action}", body)
        if code != 200:
            return jsonify({"detail": data.get("error", "Failed")}), code
        return jsonify(data)

    @app.route("/api/servers/<int:server_id>/keys/test", methods=["POST"])
    def api_test_api_key(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        body = request.json or {}
        code, data = _proxy_post(server["host"], server["admin_token"], "/admin/test-api-key", body)
        if code != 200:
            return jsonify({"detail": data.get("error", "Failed")}), code
        return jsonify(data)

    @app.route("/api/servers/<int:server_id>/keys", methods=["GET"])
    def api_list_keys(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
            
        code, data = _proxy_get(server["host"], server["admin_token"], "/admin/keys")
        if code == 200:
            return jsonify(data)
        
        return jsonify(db.list_cached_keys(server_id))

    @app.route("/api/servers/<int:server_id>/keys", methods=["POST"])
    def api_create_key(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
            
        body = request.get_json()
        code, data = _proxy_post(
            server["host"], server["admin_token"], "/admin/keys",
            {"label": body.get("label", ""), "limit_rpm": body.get("limit_rpm", 0)}
        )
        if code != 200:
            return jsonify({"detail": data.get("error", "Failed to create key")}), code
            
        db.cache_key(server_id, data.get("key"), data.get("label", ""), data.get("created_at", ""))
        return jsonify(data)

    @app.route("/api/servers/<int:server_id>/keys/revoke", methods=["POST"])
    def api_revoke_key(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
            
        body = request.get_json()
        code, data = _proxy_post(
            server["host"], server["admin_token"], "/admin/keys/revoke", {"key": body.get("key")}
        )
        if code != 200:
            return jsonify({"detail": data.get("error", "Failed")}), code
        return jsonify(data)

    @app.route("/api/servers/<int:server_id>/keys", methods=["DELETE"])
    def api_delete_key(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
            
        body = request.get_json()
        code, data = _proxy_delete(
            server["host"], server["admin_token"], "/admin/keys", {"key": body.get("key")}
        )
        if code != 200:
            return jsonify({"detail": data.get("error", "Failed")}), code
        db.remove_cached_key(server_id, body.get("key"))
        return jsonify(data)


    @app.route("/api/deploy/start", methods=["POST"])
    def api_deploy_start():
        body = request.get_json()
        host = body.get("host")
        port = int(body.get("port", 22))
        username = body.get("username")
        password = body.get("password")
        server_name = body.get("server_name", host)

        if not host or not username:
            return jsonify({"detail": "Host and username are required"}), 400

        deploy_id = deploy.start_deployment(
            host, port, username, password=password, server_name=server_name
        )
        return jsonify({"deploy_id": deploy_id})

    @app.route("/api/deploy/status/<deploy_id>", methods=["GET"])
    def api_deploy_status(deploy_id):
        st = deploy.active_deployments.get(deploy_id)
        if not st:
            return jsonify({"detail": "Deployment not found"}), 404
        return jsonify(st)

    # ── Control Plane: Model & API Testing (Flask) ─────────────────────────

    @app.route("/api/servers/<int:server_id>/test-model", methods=["POST"])
    def api_test_model(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        body = request.get_json()
        code, data = _proxy_post(
            server["host"], server["admin_token"],
            "/admin/test-model", {"name": body.get("name", ""), "prompt": body.get("prompt", "")}
        )
        return jsonify(data), code

    @app.route("/api/servers/<int:server_id>/models/health", methods=["GET"])
    def api_models_health(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        code, data = _proxy_get(server["host"], server["admin_token"], "/admin/models/health")
        return jsonify(data), code

    @app.route("/api/servers/<int:server_id>/test-api", methods=["POST"])
    def api_test_api(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        body = request.get_json()
        code, data = _proxy_post(
            server["host"], server["admin_token"],
            "/admin/test-api", {"model": body.get("model", "")}
        )
        return jsonify(data), code

    # ── Control Plane: Uptime (Flask) ──────────────────────────────────────

    @app.route("/api/servers/<int:server_id>/uptime", methods=["GET"])
    def api_server_uptime(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        code, data = _proxy_get(server["host"], server["admin_token"], "/metrics/uptime")
        return jsonify(data), code

    @app.route("/api/servers/<int:server_id>/monthly-runtime", methods=["GET"])
    def api_monthly_runtime(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        code, data = _proxy_get(server["host"], server["admin_token"], "/metrics/monthly-runtime")
        return jsonify(data), code

    # ── Control Plane: Lifecycle (Flask) ────────────────────────────────────

    @app.route("/api/servers/<int:server_id>/lifecycle/<action>", methods=["POST"])
    def api_lifecycle(server_id, action):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        # Lifecycle uses async functions — run via threading for Flask
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            if action == "restart-ollama":
                result = loop.run_until_complete(server_lifecycle.restart_ollama(server))
            elif action == "restart-agent":
                result = loop.run_until_complete(server_lifecycle.restart_agent(server))
            elif action == "stop-agent":
                result = loop.run_until_complete(server_lifecycle.stop_agent(server))
            elif action == "start-agent":
                result = loop.run_until_complete(server_lifecycle.start_agent(server))
            elif action == "start-ai":
                result = loop.run_until_complete(system_control.start_ai(server))
            elif action == "stop-ai":
                result = loop.run_until_complete(system_control.stop_ai(server))
            elif action == "restart-ai":
                result = loop.run_until_complete(system_control.restart_ai(server))
            elif action == "shutdown":
                result = loop.run_until_complete(system_control.shutdown_machine(server, force=True))
            elif action == "reboot":
                result = loop.run_until_complete(system_control.reboot_machine(server, force=True))
            elif action == "idle":
                result = loop.run_until_complete(system_control.sleep_mode(server))
            elif action == "deactivate":
                result = loop.run_until_complete(system_control.deactivate_server(server, force=True))
            elif action == "activate":
                result = loop.run_until_complete(system_control.activate_server(server))
            elif action == "health":
                result = loop.run_until_complete(server_lifecycle.check_server_health(server))
            else:
                return jsonify({"detail": f"Unknown action: {action}"}), 400
        finally:
            loop.close()
        return jsonify(result)

    # ── Control Plane: Logs (Flask) ────────────────────────────────────────

    @app.route("/api/servers/<int:server_id>/logs/recent", methods=["GET"])
    def api_logs_recent(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        limit = request.args.get("limit", 100, type=int)
        code, data = _proxy_get(server["host"], server["admin_token"], f"/logs/recent?limit={limit}")
        return jsonify(data), code

    @app.route("/api/servers/<int:server_id>/logs/errors", methods=["GET"])
    def api_logs_errors(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        limit = request.args.get("limit", 50, type=int)
        code, data = _proxy_get(server["host"], server["admin_token"], f"/logs/errors?limit={limit}")
        return jsonify(data), code

    # ── Control Plane: Compatibility (Flask) ───────────────────────────────

    @app.route("/api/servers/<int:server_id>/compatibility", methods=["GET"])
    def api_compatibility(server_id):
        server = db.get_server(server_id)
        if not server:
            return jsonify({"detail": "Server not found"}), 404
        report = compatibility.full_compatibility_report(server)
        return jsonify(report)


if __name__ == "__main__":
    print(f"\n  Shadow-Lab Control Plane running at: http://0.0.0.0:{DASHBOARD_PORT}\n")
    if USE_FASTAPI:
        uvicorn.run("main:app", host="0.0.0.0", port=DASHBOARD_PORT, reload=False, log_level="warning")
    else:
        app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)
