"""轻量网络诊断：IP、网关、DNS、延迟、丢包和按需下载测速。"""

import json
import re
import socket
import subprocess
import time

import requests


class NetworkDiagnostics:
    def __init__(self, timeout=8):
        self.timeout = max(2, int(timeout))

    def local_configuration(self):
        result = {"hostname": socket.gethostname(), "interfaces": [], "local_ips": [],
                  "gateways": [], "dns_servers": []}
        script = (
            "Get-NetIPConfiguration | Select-Object InterfaceAlias,InterfaceDescription," 
            "@{N='IPv4';E={$_.IPv4Address.IPAddress}},@{N='Gateway';E={$_.IPv4DefaultGateway.NextHop}}," 
            "@{N='DNS';E={$_.DNSServer.ServerAddresses}} | ConvertTo-Json -Depth 4 -Compress"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout + 10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            data = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else []
            for item in data if isinstance(data, list) else [data]:
                ips = item.get("IPv4") or []
                dns = item.get("DNS") or []
                if isinstance(dns, dict):
                    dns = dns.get("value") or dns.get("Value") or []
                ips = ips if isinstance(ips, list) else [ips]
                dns = dns if isinstance(dns, list) else [dns]
                interface = {"name": item.get("InterfaceAlias", ""),
                             "description": item.get("InterfaceDescription", ""),
                             "ipv4": [x for x in ips if x], "gateway": item.get("Gateway") or "",
                             "dns": [x for x in dns if x]}
                # 169.254/16 是无 DHCP 地址；没有网关时不作为主要活动接口展示。
                interface["ipv4"] = [x for x in interface["ipv4"] if not str(x).startswith("169.254.")]
                if interface["ipv4"]:
                    result["interfaces"].append(interface)
                    result["local_ips"].extend(interface["ipv4"])
                    result["dns_servers"].extend(interface["dns"])
                if interface["gateway"]:
                    result["gateways"].append(interface["gateway"])
        except Exception:
            try:
                result["local_ips"] = sorted({x[4][0] for x in socket.getaddrinfo(socket.gethostname(), None)
                                               if "." in x[4][0] and not x[4][0].startswith(("127.", "169.254."))})
            except OSError:
                pass
        for key in ("local_ips", "gateways", "dns_servers"):
            result[key] = list(dict.fromkeys(result[key]))
        return result

    def public_ip(self):
        try:
            response = requests.get("https://api.ipify.org", params={"format": "json"}, timeout=self.timeout)
            response.raise_for_status()
            return {"ok": True, "ip": response.json().get("ip", "")}
        except Exception as exc:
            return {"ok": False, "ip": "", "error": str(exc)[:160]}

    def dns_test(self, hostname="api.dingtalk.com"):
        started = time.perf_counter()
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(hostname, 443)})
            return {"ok": True, "hostname": hostname, "addresses": addresses,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
        except OSError as exc:
            return {"ok": False, "hostname": hostname, "addresses": [], "error": str(exc)}

    def ping(self, host="223.5.5.5", count=4):
        try:
            proc = subprocess.run(
                ["ping", "-n", str(max(1, min(int(count), 10))), "-w", "2000", host],
                capture_output=True, text=True, encoding="gbk", errors="replace",
                timeout=self.timeout + 4, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            text = proc.stdout
            latencies = [int(x) for x in re.findall(r"(?:time|时间)[=<]\s*(\d+)ms", text, re.I)]
            sent_match = re.search(r"(?:Sent|已发送)\s*=\s*(\d+)", text, re.I)
            lost_match = re.search(r"(?:Lost|丢失)\s*=\s*(\d+)", text, re.I)
            sent = int(sent_match.group(1)) if sent_match else max(1, int(count))
            lost = int(lost_match.group(1)) if lost_match else max(0, sent - len(latencies))
            return {"ok": proc.returncode == 0, "host": host, "sent": sent, "lost": lost,
                    "loss_percent": round(lost * 100 / sent, 1),
                    "average_ms": round(sum(latencies) / len(latencies), 1) if latencies else None}
        except Exception as exc:
            return {"ok": False, "host": host, "error": str(exc)[:160]}

    def speed_test(self, bytes_to_download=2_000_000):
        size = max(200_000, min(int(bytes_to_download), 10_000_000))
        started = time.perf_counter()
        received = 0
        try:
            with requests.get("https://speed.cloudflare.com/__down", params={"bytes": size},
                              stream=True, timeout=(5, self.timeout + 12)) as response:
                response.raise_for_status()
                for chunk in response.iter_content(65536):
                    received += len(chunk)
            seconds = max(time.perf_counter() - started, 0.001)
            return {"ok": True, "download_mbps": round(received * 8 / seconds / 1_000_000, 2),
                    "bytes": received, "seconds": round(seconds, 2),
                    "note": "小文件单线程结果，仅用于快速诊断"}
        except Exception as exc:
            return {"ok": False, "download_mbps": None, "bytes": received, "error": str(exc)[:160]}

    def diagnose(self, include_speed=False):
        result = {"local": self.local_configuration(), "public": self.public_ip(),
                  "dns": self.dns_test(), "ping": self.ping()}
        if include_speed:
            result["speed"] = self.speed_test()
        result["ok"] = bool(result["dns"].get("ok") and result["ping"].get("ok"))
        return result
