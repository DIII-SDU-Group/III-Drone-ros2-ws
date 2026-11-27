#!/usr/bin/env python3
"""Bind the HIL ground-control proxy to the Pi selected by this invocation."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def request_json(base_url: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        base_url.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urlopen(request, timeout=5.0) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError(f"GC proxy returned a non-object for {path}")
    return result


def same_runtime_url(selected_url: str, requested_url: str) -> bool:
    if selected_url.rstrip("/") == requested_url.rstrip("/"):
        return True
    selected, requested = urlsplit(selected_url), urlsplit(requested_url)
    if (
        selected.scheme != requested.scheme
        or selected.port != requested.port
        or selected.path.rstrip("/") != requested.path.rstrip("/")
        or not selected.hostname
        or not requested.hostname
    ):
        return False
    try:
        selected_addresses = {
            item[4][0] for item in socket.getaddrinfo(selected.hostname, None, socket.AF_INET)
        }
        requested_addresses = {
            item[4][0] for item in socket.getaddrinfo(requested.hostname, None, socket.AF_INET)
        }
    except socket.gaierror:
        return False
    return bool(selected_addresses & requested_addresses)


def proxy_reachable_runtime_url(runtime_url: str) -> str:
    """Give the Docker proxy a concrete IPv4 peer; mDNS is host-only here."""
    parsed = urlsplit(runtime_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise ValueError("HIL runtime URL must contain an http(s) host and port")
    try:
        addresses = dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(
                parsed.hostname, parsed.port, socket.AF_INET, socket.SOCK_STREAM
            )
        )
    except socket.gaierror as exc:
        raise ValueError(f"Unable to resolve HIL runtime {parsed.hostname}: {exc}") from exc
    for address in addresses:
        candidate = f"{parsed.scheme}://{address}:{parsed.port}"
        try:
            request_json(candidate, "/identity")
        except (HTTPError, URLError, OSError, ValueError):
            continue
        return candidate
    raise ValueError(f"No reachable IPv4 Runtime API for {parsed.hostname}:{parsed.port}")


def bind(proxy_url: str, runtime_url: str) -> str:
    runtime_url = runtime_url.rstrip("/")
    current = request_json(proxy_url, "/runtime/target")
    selected = current.get("selected")
    if isinstance(selected, dict):
        selected_url = str(selected.get("base_url", ""))
        if selected_url.rstrip("/") == runtime_url:
            return str(selected.get("endpoint_id", "already-selected"))
        if same_runtime_url(selected_url, runtime_url):
            # DNS aliasing alone does not prove the proxy's selected address is
            # still live. In particular, mDNS can retain a stale direct-link
            # IPv4 beside a reachable LAN address. Compare fresh identities.
            try:
                selected_identity = request_json(selected_url, "/identity")
                requested_identity = request_json(runtime_url, "/identity")
            except (HTTPError, URLError, OSError, ValueError):
                # A disconnected proxy may replace a stale selection. An
                # active browser may not switch until the operator disconnects.
                if current.get("browser_connected") is True:
                    raise ValueError("GC browser is connected to an unreachable runtime")
            else:
                identity_keys = ("runtime_id", "host_label", "profile")
                if (
                    selected_identity.get("runtime_id")
                    and all(
                        selected_identity.get(key) == requested_identity.get(key)
                        for key in identity_keys
                    )
                    and _literal_ipv4_url(selected_url)
                ):
                    return str(selected.get("endpoint_id", "already-selected"))
        if current.get("browser_connected") is True:
            raise ValueError("GC browser is connected to a different runtime")
    endpoint = request_json(
        proxy_url, "/runtime/discovery/manual", {"base_url": runtime_url}
    )
    endpoint_id = endpoint.get("endpoint_id")
    if not isinstance(endpoint_id, str) or endpoint.get("base_url") != runtime_url:
        raise ValueError("GC proxy did not register the selected HIL runtime URL")
    state = request_json(
        proxy_url, "/runtime/target/select", {"endpoint_id": endpoint_id}
    )
    selected = state.get("selected")
    if not isinstance(selected, dict) or selected.get("base_url") != runtime_url:
        raise ValueError("GC proxy selected a different runtime than the HIL command target")
    return endpoint_id


def _literal_ipv4_url(url: str) -> bool:
    try:
        ipaddress.IPv4Address(urlsplit(url).hostname or "")
    except ipaddress.AddressValueError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--runtime-url", required=True)
    args = parser.parse_args()
    try:
        proxy_runtime_url = proxy_reachable_runtime_url(args.runtime_url)
        endpoint_id = bind(args.proxy_url, proxy_runtime_url)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        parser.exit(1, f"GC proxy target binding failed: {exc}\n")
    print(f"GC proxy selected HIL runtime {proxy_runtime_url} ({endpoint_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
