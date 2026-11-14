"""A stand-in for Liquidsoap's telnet server, used by the tests.

It implements the same `air.*` commands as config/radio.liq and, like the real
script, asks the API for the next AutoDJ track over HTTP.
"""
import json
import socketserver
import threading
import urllib.request


class FakeLiquidsoap:
    def __init__(self, api_url=None, token=""):
        self.api_url = api_url
        self.token = token
        self.state = {"source": "autodj", "title": "", "artist": "", "album": "",
                      "air_id": "", "live_connected": False, "obs_connected": False,
                      "live_enabled": True, "live_waiting": False, "live_switch": "track",
                      "record": "off", "mic_connected": False, "music_volume": 100,
                      "pause": False, "music_stopped": False, "meta": []}
        self.mic_token = ""
        self.commands = []
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for raw in self.rfile:
                    line = raw.decode().strip()
                    if line == "quit":
                        return
                    fake.commands.append(line)
                    reply = fake.reply(line)
                    self.wfile.write((reply + "\r\nEND\r\n").encode())

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, line):
        cmd, _, arg = line.partition(" ")
        if cmd == "air.state":
            return json.dumps(self.state)
        if cmd == "air.skip":
            self.advance()
            return "Skipped"
        if cmd == "air.live":
            self.state["live_enabled"] = arg == "on"
            return "OK"
        if cmd == "air.record":
            self.state["record"] = arg
            return "OK"
        if cmd == "air.volume":
            self.state["music_volume"] = int(arg)
            return "OK"
        if cmd == "air.pause":
            self.state["pause"] = arg == "on"
            self.state["music_stopped"] = False if arg != "on" else self.state["music_stopped"]
            return "OK"
        if cmd == "air.mic_token":
            self.mic_token = arg
            return "OK"
        return "ERROR: unknown command"

    def advance(self):
        """Behave like request.dynamic: fetch the next URI from the API."""
        request = urllib.request.Request(self.api_url + "/internal/next",
                                         headers={"X-Air-Token": self.token})
        with urllib.request.urlopen(request, timeout=5) as response:
            uri = response.read().decode()
        if not uri:
            self.state.update(air_id="", title="Emergency Tune", artist="Local")
            return uri
        air_id = uri.split('air_id="')[1].split('"')[0]
        self.state.update(air_id=air_id, title="", artist="", source="autodj")
        return uri

    def go_live(self, title="DJ Set", artist="DJ Example"):
        self.state.update(source="live", live_connected=True, air_id="",
                          title=title, artist=artist)

    def stop(self):
        self.server.shutdown()
