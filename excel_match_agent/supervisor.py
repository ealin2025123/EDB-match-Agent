"""监督Agent工作进程；整体时限可选，各API和解析进程仍有自己的超时。"""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def time_budget(job):
    budget = job.get("total_timeout")
    if budget is None:
        return None
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not math.isfinite(budget) or budget <= 0:
        raise ValueError("total_timeout须为正数秒数，或null表示不设Agent整体时限")
    return budget


def run_supervised(job, job_base, docker=None, output_root=None, execution_backend=None):
    from .app import ROOT, dump, load, terminate_tree
    started = time.monotonic()
    budget = time_budget(job)
    base = Path(job_base).resolve()
    output = Path(output_root or (base / job.get("output_dir", "runs"))).resolve()
    folder = output / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    payload = {"job": job, "job_base": str(base), "run_dir": str(folder), "docker": docker,
               "execution_backend": execution_backend, "started": started,
               "deadline": started + budget if budget is not None else None}
    payload_path = folder / "job-request.json"
    dump(payload_path, payload)
    result_path = folder / "run-result.json"
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with (folder / "supervisor.stdout").open("wb") as stream:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "excel_match_agent.supervisor", str(payload_path)],
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream, **kwargs)
        timed_out = False
        while process.poll() is None:
            if budget is not None and time.monotonic() >= started + budget:
                timed_out = True
                terminate_tree(process, kill_timeout=1)
                process.wait(timeout=2)
                break
            time.sleep(0.05)
    try:
        result = load(result_path) if result_path.exists() else {}
    except (OSError, ValueError):
        result = {}
    elapsed = time.monotonic() - started
    if timed_out or (budget is not None and elapsed >= budget):
        for name in ("node-output.json", "node-output.txt"):
            (folder / name).unlink(missing_ok=True)
        for key in ("OUTDATA", "unitArray", "codeOutputResult", "name_review", "metrics"):
            result.pop(key, None)
        result.update(status="needs_review", failure_kind="TOTAL_TIMEOUT", production_ready=False)
        result.setdefault("notifications", []).append({"code": "TOTAL_TIMEOUT", "severity": "error",
            "message": f"Agent整体执行超过配置的{budget}秒预算，已停止，未返回成功节点值"})
    elif process.returncode or not result:
        result = {"status": "needs_review", "failure_kind": "WORKER_FAILED", "production_ready": False,
                  "notifications": [{"code": "WORKER_FAILED", "severity": "error", "message": "Agent工作进程退出异常，请查看运行记录"}]}
    result.update(run_id=folder.name, output_dir=str(folder), elapsed_seconds=round(elapsed, 3),
                  total_timeout_seconds=budget, overall_deadline_enabled=budget is not None)
    result["rebuilds"] = max(0, result.get("attempts", 0) - 1)
    result["max_rebuilds"] = 2
    result["requires_human_review"] = result.get("status") != "candidate_pass"
    if result["requires_human_review"]:
        result["human_review_file"] = str(folder / "human-review.json")
        # 工作进程超时/异常时也必须留下明确的人工处理入口。
        dump(folder / "human-review.json", {
            "status": "failed", "requires_human_review": True,
            "failure_kind": result.get("failure_kind"), "attempts": result.get("attempts", 0),
            "rebuilds": result["rebuilds"], "max_rebuilds": 2,
            "run_id": folder.name, "diagnostics": str(result_path),
            "notifications": result.get("notifications", []),
            "last_evaluation": result.get("last_evaluation"), "history": result.get("history", [])})
    dump(result_path, result)
    return result


if __name__ == "__main__":
    from .app import _run_job, load
    payload = load(Path(sys.argv[1]))
    _run_job(payload["job"], Path(payload["job_base"]), docker=payload["docker"],
             execution_backend=payload["execution_backend"], _run_dir=Path(payload["run_dir"]),
             _started=payload["started"], _deadline=payload["deadline"])
