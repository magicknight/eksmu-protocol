"""Demonstrate real HTTP -> file -> separate process -> signed result, without a browser.

Uses temporary local test data and keys, deletes them on exit, and sends no external
requests. Not a substitute for browser acceptance and not a production deployment.
"""
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eksmu.server import make_server


def main():
    summary = {"schema": "eksmu.worker-demo.v1", "status": "FAIL", "browser_used": False,
               "transport": "loopback HTTP plus local files and real subprocesses", "checks": []}
    with tempfile.TemporaryDirectory(prefix="eksmu-worker-demo-") as directory:
        root = Path(directory)
        server = make_server(root / "community.sqlite", 0, verification=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            def request(path, data=None, token=None):
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                headers = {"Content-Type": "application/json", "X-Eksmu-Client": "local-prototype"}
                if token: headers["Authorization"] = "Bearer " + token
                try:
                    client.request("GET" if data is None else "POST", path,
                                   json.dumps(data) if data is not None else None, headers)
                    response = client.getresponse(); body = json.loads(response.read())
                    if response.status >= 400: raise RuntimeError(body)
                    return body
                finally: client.close()

            def worker(*args):
                result = subprocess.run([sys.executable, "-m", "eksmu_bridge.worker", *args],
                                        cwd=ROOT, capture_output=True, text=True, timeout=15)
                if result.returncode: raise RuntimeError(result.stderr)
                return result.stdout.strip()

            actor = request("/api/actors", {"alias": "演示参与者（测试数据）"})
            revision = request("/api/explorations", {"body": "演示：没有评审许可也能发表。"}, actor["token"])["revision"]
            initial = request("/api/export")
            key = root / "worker.key"; public_key = worker("keygen", "--key", str(key))
            rational = lambda n: {"op": "rational", "numerator": str(n), "denominator": "1"}
            for case, right, cancel in (("equal", 1, False), ("unequal_cancelled", 2, True)):
                task = request("/api/verification/prepare", {"revision_id": revision["id"], "scope": "只检查显式输入",
                    "problem": {"left": rational(1), "right": rational(right)}}, actor["token"])["task"]["id"]
                dispatch = request("/api/verification/dispatch", {"task_id": task, "operation_id": str(uuid.uuid4()),
                    "expected_public_key": public_key, "consent": True, "ttl_seconds": 3600}, actor["token"])
                did = dispatch["dispatch"]["id"]
                capsule = request("/api/verification/capsule?dispatch_id=" + did, token=actor["token"])
                job, output = root / (case + "-job.json"), root / (case + "-result.json")
                job.write_text(json.dumps(capsule))
                worker("run", "--job", str(job), "--key", str(key), "--out", str(output), "--allow-local-compute")
                verified = json.loads(worker("check", "--job", str(job), "--result", str(output), "--public-key", public_key))
                if cancel: request("/api/verification/cancel", {"task_id": task}, actor["token"])
                record = request("/api/verification/collect", {"dispatch_id": did, "submission": json.loads(output.read_text())}, actor["token"])
                if not verified["replay_matches"]: raise RuntimeError("Replay failed")
                expected = "late_cancelled" if cancel else "recorded"
                if record["disposition"] != expected: raise RuntimeError("Unexpected disposition")
                if verified["status"] != ("failed" if cancel else "passed"): raise RuntimeError("Unexpected arithmetic")
                summary["checks"].append({"case": case, "status": verified["status"], "disposition": expected})
            if request("/api/export") != initial: raise RuntimeError("Verification changed publication records")
            request("/api/comments", {"revision_id": revision["id"], "body": "继续提出问题"}, actor["token"])
            request("/api/explorations", {"body": "检查失败后继续自由发表"}, actor["token"])
            summary["checks"].append({"case": "publication_independence", "status": "PASS"})
            summary["status"] = "PASS"
        finally:
            server.shutdown(); server.server_close(); thread.join()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
