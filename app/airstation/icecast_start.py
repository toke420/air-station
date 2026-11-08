"""Render icecast.xml with secrets, then exec Icecast (container entry point)."""
import os
import xml.etree.ElementTree as ET
from pathlib import Path


def render(template, secret_dir, output, hostname=None):
    tree = ET.parse(template)
    for name in ("source", "relay", "admin"):
        password = (Path(secret_dir) / f"{name}_password").read_text().strip()
        if not password:
            raise ValueError(f"Empty {name} password")
        tree.find(f"authentication/{name}-password").text = password
    if hostname:
        tree.find("hostname").text = hostname
    tree.write(output, encoding="utf-8", xml_declaration=True)
    os.chmod(output, 0o600)


if __name__ == "__main__":
    render("/app/config/icecast.xml", "/run/secrets", "/tmp/icecast.xml",
           os.environ.get("AIR_DOMAIN"))
    os.execvp("icecast2", ["icecast2", "-c", "/tmp/icecast.xml"])
