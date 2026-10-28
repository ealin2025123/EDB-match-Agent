import json
import socket
import time
import urllib.error
import urllib.request

HOST = "YOUR_AGENT_HOST"
PORT = 8767


def main(params):
    started = time.monotonic()
    stage = "tcp_connect"
    status = "failed"
    code = ""
    message = ""
    try:
        with socket.create_connection((HOST, PORT), timeout=10):
            pass
        stage = "http_health"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://" + HOST + ":" + str(PORT) + "/health", timeout=10) as response:
            data = json.loads(response.read(8192).decode("utf-8"))
            if not isinstance(data, dict) or data.get("service") != "excel-match-agent" or data.get("status") != "ok":
                raise ValueError("Unexpected health response")
        status = "success"
        message = "TCP and Agent HTTP health check passed."
    except Exception as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(exc, urllib.error.HTTPError):
            code = "HEALTH_HTTP_" + str(exc.code)
            message = "HTTP server responded with an error. Check URL and gateway."
        elif isinstance(reason, TimeoutError):
            code = "TCP_CONNECT_TIMEOUT" if stage == "tcp_connect" else "HTTP_HEALTH_TIMEOUT"
            message = "Check DC-to-Agent route, ACL and firewall." if stage == "tcp_connect" else "TCP connected, but HTTP health response timed out."
        elif isinstance(reason, ConnectionRefusedError):
            code = "CONNECTION_REFUSED"
            message = "Check Agent service, target IP and listening port."
        elif isinstance(reason, socket.gaierror):
            code = "DNS_FAILED"
            message = "DC could not resolve the Agent hostname."
        elif isinstance(exc, (ValueError, UnicodeError)):
            code = "HEALTH_RESPONSE_INVALID"
            message = "Response is not the expected Agent health JSON."
        else:
            code = "PROBE_FAILED_" + type(reason).__name__
            message = "Network probe failed. Check deployment-side logs."
    message += " elapsed=" + str(round(time.monotonic() - started, 3)) + "s; target=" + HOST + ":" + str(PORT)
    return {"agent": [{"status": status, "stage": stage, "error_code": code, "message": message}]}
