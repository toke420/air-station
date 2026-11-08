"""Minimal client for the Liquidsoap telnet server (container network only)."""
import json
import socket

from . import config


class LiquidsoapError(Exception):
    pass


def command(cmd, host=None, port=None, timeout=4.0):
    host = host or config.LIQ_HOST
    port = port or config.LIQ_TELNET_PORT
    if "\n" in cmd or "\r" in cmd:
        raise ValueError("invalid command")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(cmd.encode() + b"\n")
            data = b""
            while not data.endswith(b"\nEND\r\n") and not data.endswith(b"\nEND\n") \
                    and data not in (b"END\r\n", b"END\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            try:
                sock.sendall(b"quit\n")
            except OSError:
                pass
    except OSError as error:
        raise LiquidsoapError(f"Liquidsoap is not reachable: {error}") from error
    text = data.decode("utf-8", errors="replace")
    lines = [l for l in text.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] in ("", "END"):
        lines.pop()
    return "\n".join(lines).strip()


def _metadata_dict(value):
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, list):
        result = {}
        for pair in value:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                result[str(pair[0])] = str(pair[1])
        return result
    return {}


def state(**kwargs):
    """Parsed output of the `air.state` command defined in radio.liq."""
    raw = command("air.state", **kwargs)
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise LiquidsoapError(f"Unexpected air.state reply: {raw[:200]!r}") from error
    for key in ("title", "artist", "album", "air_id", "source", "record", "live_switch"):
        data[key] = str(data.get(key) or "")
    data.setdefault("meta", {})
    data["meta"] = _metadata_dict(data.get("meta"))
    for key in ("live_connected", "obs_connected", "live_enabled", "live_waiting"):
        data[key] = bool(data.get(key))
    return data
