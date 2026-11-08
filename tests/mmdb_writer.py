"""Tiny MaxMind DB writer, only for tests of airstation.geoip.Reader.

Builds an IPv6 tree (IPv4 networks live under ::/96, as in real GeoIP2/DB-IP files),
any record size (24/28/32), and a data section that uses pointers for repeated
values, like real databases do.
"""
import ipaddress
import struct

MARKER = b"\xab\xcd\xefMaxMind.com"


def _ctrl(kind, size):
    out = bytearray()
    if size < 29:
        sz, extra = size, b""
    elif size < 285:
        sz, extra = 29, bytes([size - 29])
    elif size < 65821:
        sz, extra = 30, (size - 285).to_bytes(2, "big")
    else:
        sz, extra = 31, (size - 65821).to_bytes(3, "big")
    if kind <= 7:
        out.append((kind << 5) | sz)
    else:
        out.append(sz)
        out.append(kind - 7)
    return bytes(out) + extra


def _uint(value):
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    return raw


class Encoder:
    def __init__(self):
        self.data = bytearray()
        self.cache = {}

    def pointer(self, offset):
        if offset < 2048:
            return bytes([0x20 | (offset >> 8), offset & 0xFF])
        if offset < 526336:
            v = offset - 2048
            return bytes([0x28 | (v >> 16)]) + (v & 0xFFFF).to_bytes(2, "big")
        v = offset - 526336
        return bytes([0x30 | (v >> 24)]) + (v & 0xFFFFFF).to_bytes(3, "big")

    def encode(self, value, top=True):
        if isinstance(value, str):
            key = ("s", value)
            if key in self.cache and not top:
                return self.pointer(self.cache[key])
            raw = value.encode()
            return _ctrl(2, len(raw)) + raw
        if isinstance(value, bool):
            return _ctrl(14, int(value))
        if isinstance(value, float):
            return _ctrl(3, 8) + struct.pack(">d", value)
        if isinstance(value, int):
            raw = _uint(value)
            return _ctrl(6 if value < 2 ** 32 else 9, len(raw)) + raw
        if isinstance(value, list):
            return _ctrl(11, len(value)) + b"".join(self.encode(v, False) for v in value)
        if isinstance(value, dict):
            return _ctrl(7, len(value)) + b"".join(
                self.encode(k, False) + self.encode(v, False) for k, v in value.items())
        raise TypeError(value)

    def add(self, value):
        """Append a top-level record and remember its strings for later pointers."""
        offset = len(self.data)
        self.data += self.encode(value)
        if isinstance(value, dict):          # make later records point at this one's keys
            self._remember(value, offset)
        return offset

    def _remember(self, value, offset):
        # Re-walk the encoding to learn where each string sits (good enough for tests).
        start = len(self.data)
        for text in _strings(value):
            raw = _ctrl(2, len(text.encode())) + text.encode()
            pos = self.data.find(raw, offset, start)
            if pos >= 0 and ("s", text) not in self.cache:
                self.cache[("s", text)] = pos


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def write(path, networks, record_size=28):
    """networks: [(cidr string, record dict)]"""
    enc = Encoder()
    nodes = [[None, None]]
    for cidr, record in networks:
        net = ipaddress.ip_network(cidr)
        if net.version == 4:
            bits = 96 + net.prefixlen
            number = int(net.network_address)
        else:
            bits = net.prefixlen
            number = int(net.network_address)
        offset = enc.add(record)
        node = 0
        for i in range(bits):
            bit = (number >> (127 - i)) & 1
            if i == bits - 1:
                nodes[node][bit] = ("data", offset)
                break
            nxt = nodes[node][bit]
            if nxt is None:
                nodes.append([None, None])
                nxt = ("node", len(nodes) - 1)
                nodes[node][bit] = nxt
            node = nxt[1]
    count = len(nodes)

    def value(rec):
        if rec is None:
            return count
        if rec[0] == "node":
            return rec[1]
        return count + 16 + rec[1]

    tree = bytearray()
    for left, right in nodes:
        lv, rv = value(left), value(right)
        if record_size == 24:
            tree += lv.to_bytes(3, "big") + rv.to_bytes(3, "big")
        elif record_size == 28:
            tree += (lv & 0xFFFFFF).to_bytes(3, "big")
            tree.append(((lv >> 24) << 4) | (rv >> 24))
            tree += (rv & 0xFFFFFF).to_bytes(3, "big")
        else:
            tree += lv.to_bytes(4, "big") + rv.to_bytes(4, "big")
    meta = Encoder().encode({"node_count": count, "record_size": record_size, "ip_version": 6,
                             "database_type": "Test-City", "build_epoch": 1790000000,
                             "binary_format_major_version": 2, "binary_format_minor_version": 0,
                             "languages": ["en"], "description": {"en": "test"}})
    with open(path, "wb") as f:
        f.write(bytes(tree) + bytes(16) + bytes(enc.data) + MARKER + meta)
