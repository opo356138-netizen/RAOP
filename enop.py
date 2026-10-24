# ==============================================================================
#   PROJECT: Httpsh Protocol Engine (v1.2 Sovereign Edition)
#   ARCHITECT: op
#   LICENSE: GNU General Public License v3.0 (GPLv3)
#   PURPOSE: Educational, Research & Sovereign Local-First Computing Only
# ==============================================================================
"""
STANDARD SPECIFICATION: Httpsh Protocol Core Engine (v1.2 - Sovereign Edition)
CREATED & ARCHITECTED BY: op
OWNERSHIP STAMP & MANIFESTO:
    This software and protocol standard ("Httpsh") is an independent,
    sovereign, local-first creation designed, architected, and owned by op.

    Security note:
    When a vault password is set, stored data is protected with AES-256-GCM using
    a key derived by scrypt. Without a password, data is stored as plain text.

This module has NO Tkinter dependency. It provides:
    * VaultCrypto            - AES-256-GCM helper (needs the 'cryptography' package)
    * HttpshClient           - loopback-only Httpsh networking (127.0.0.1:8000)
    * EnopSession            - guest session / node state / UI state, persisted in
                               the SAME SQLite file as the vault (encrypted when
                               the vault is encrypted)
    * TextExtractor          - book / document / image text import with formatting
    * HttpshStandardEngine   - the original resource-registry engine (legacy API)
"""

import base64
import colorsys
import hashlib
import html
import json
import os
import posixpath
import random
import re
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

try:
    AESGCM = __import__(
        "cryptography.hazmat.primitives.ciphers.aead", fromlist=["AESGCM"]
    ).AESGCM
    HAS_CRYPTO = True
except ImportError:
    AESGCM = None
    HAS_CRYPTO = False

# ------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------
UNSUPPORTED_MSG = "التخصيص الذي طلبتة غير مدعوم"

NODE_URL = "http://127.0.0.1:8000/"
HTTPSH_VERSION = "1.1"
MAX_RESPONSE_BYTES = 1_048_576

COLOR_TAG_NAMES = ("c_red", "c_orange", "c_yellow", "c_green", "c_blue", "c_purple")
STYLE_TAG_NAMES = ("bold", "italic") + COLOR_TAG_NAMES
_PALETTE_HUES = {"c_red": 0, "c_orange": 30, "c_yellow": 55,
                 "c_green": 120, "c_blue": 220, "c_purple": 280}

MAX_IMPORT_CHARS = 500_000
MAX_IMPORT_BYTES = 50 * 1024 * 1024

MEDIA_PATTERN = re.compile(r"<img\b|\[img\b|!\[[^\]]*\]\([^)]*\)|data:image/",
                           re.IGNORECASE)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------------------
# Errors
# ------------------------------------------------------------------------------
class UnsupportedCustomization(Exception):
    """Imported content uses a style / format / extraction rule we cannot represent.
    str(exc) is always exactly UNSUPPORTED_MSG; the technical reason is in .detail"""

    def __init__(self, detail: str = ""):
        super().__init__(UNSUPPORTED_MSG)
        self.detail = detail


class ExtractionUnavailable(Exception):
    """An optional dependency needed for this file type is not installed."""


# ------------------------------------------------------------------------------
# Encryption (AES-256-GCM, key derived with scrypt)
# ------------------------------------------------------------------------------
class VaultCrypto:
    CHECK_VALUE = "TOPFRA-VAULT-OK"

    def __init__(self, key: bytes):
        self._aes = AESGCM(key)

    @staticmethod
    def derive_key(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1,
                              maxmem=64 * 1024 * 1024, dklen=32)

    def encrypt(self, text: str, aad: str) -> str:
        nonce = os.urandom(12)
        ct = self._aes.encrypt(nonce, text.encode("utf-8"), aad.encode("utf-8"))
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str, aad: str) -> str:
        raw = base64.b64decode(token)
        return self._aes.decrypt(raw[:12], raw[12:], aad.encode("utf-8")).decode("utf-8")


# ------------------------------------------------------------------------------
# Httpsh networking (loopback only)
# ------------------------------------------------------------------------------
class HttpshClient:
    ALLOWED_TYPES = ("text", "code")

    def __init__(self, url: str = NODE_URL):
        self.url = url
        # empty ProxyHandler: loopback traffic must never go through a system proxy
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def sanitize(text: str) -> str:
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"https?://\S+|www\.\S+", "[LINK_REMOVED]", text)
        text = re.sub(r"<[^>]+>", "", text)
        return text

    @classmethod
    def validate_payload(cls, payload_type: str, content: str):
        if payload_type.strip().lower() not in cls.ALLOWED_TYPES:
            return False, f"Payload type '{payload_type}' is not allowed (text/code only)."
        if MEDIA_PATTERN.search(content):
            return False, ("Httpsh forbids images/media references. "
                           "Remove <img ...>, [img ...] or image links first.")
        if payload_type == "text" and "<script" in content.lower():
            return False, "Text payload contains forbidden executable <script> content."
        return True, ""

    def _request(self, method: str, body=None) -> dict:
        headers = {
            "User-Agent": f"Httpsh-Client/{HTTPSH_VERSION}",
            "X-Httpsh-Version": HTTPSH_VERSION,
            "X-Httpsh-Core": "TOPFRA",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(self.url, data=data, headers=headers, method=method)
        with self._opener.open(req, timeout=3) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Node response is larger than 1 MB - rejected.")
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Node response is not a JSON object.")
        return parsed

    @staticmethod
    def is_verified(resp: dict) -> bool:
        return resp.get("core") == "TOPFRA" and resp.get("protocol", "Httpsh") == "Httpsh"

    def get(self) -> dict:
        return self._request("GET")

    def post(self, resource_id: str, payload_type: str, payload: str) -> dict:
        return self._request("POST", {
            "protocol": "Httpsh", "version": HTTPSH_VERSION, "core": "TOPFRA",
            "resource_id": resource_id, "type": payload_type, "payload": payload,
        })


# ------------------------------------------------------------------------------
# Session / node / UI state persisted next to the vault
# ------------------------------------------------------------------------------
class EnopSession:
    """Guest session + networking state + UI state, stored in the vault database.

    Must be used from the thread that owns the SQLite connection (the UI thread)."""

    def __init__(self, conn: sqlite3.Connection, cipher=None):
        self.conn = conn
        self.cipher = cipher
        self.restored = False
        self.profile = {}
        self.node = {}
        self.ui = {"last_tab": 0}
        with self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS enop_state "
                              "(key TEXT PRIMARY KEY, blob TEXT NOT NULL, updated TEXT)")

    @staticmethod
    def new_guest_profile() -> dict:
        now = _now()
        return {"identity": f"guest_{int(time.time())}_{random.randint(1000, 9999)}",
                "mode": "guest", "created_at": now, "last_active": now, "boots": 1}

    def _dump(self, key: str, obj: dict) -> str:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        return self.cipher.encrypt(text, f"enop:{key}") if self.cipher else text

    def _load(self, key: str):
        row = self.conn.execute("SELECT blob FROM enop_state WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            raw = self.cipher.decrypt(row[0], f"enop:{key}") if self.cipher else row[0]
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def restore_or_create(self) -> dict:
        prof = self._load("session")
        valid = (isinstance(prof, dict) and isinstance(prof.get("identity"), str)
                 and prof["identity"].startswith("guest_"))
        if valid:
            self.restored = True
            try:
                prof["boots"] = int(prof.get("boots", 1)) + 1
            except (TypeError, ValueError):
                prof["boots"] = 2
            prof.setdefault("created_at", _now())
            prof.setdefault("mode", "guest")
        else:
            self.restored = False
            prof = self.new_guest_profile()
        prof["last_active"] = _now()
        self.profile = prof

        defaults = {"node_url": NODE_URL, "last_action": None, "last_status": "never contacted",
                    "last_seen": None, "last_error": "", "sent": 0, "fetched": 0, "failures": 0}
        stored = self._load("node") or {}
        for k in defaults:
            if k in stored and k != "node_url":      # never trust a stored URL
                defaults[k] = stored[k]
        self.node = defaults

        ui = self._load("ui") or {}
        last_tab = ui.get("last_tab", 0)
        self.ui = {"last_tab": last_tab if isinstance(last_tab, int) else 0}
        self.save()
        return self.profile

    def save(self):
        self.profile["last_active"] = _now()
        stamp = _now()
        with self.conn:
            for key, obj in (("session", self.profile), ("node", self.node), ("ui", self.ui)):
                self.conn.execute("INSERT OR REPLACE INTO enop_state(key, blob, updated) "
                                  "VALUES (?, ?, ?)", (key, self._dump(key, obj), stamp))

    def set_last_tab(self, idx: int):
        self.ui["last_tab"] = int(idx)

    def record_node_event(self, action: str, ok: bool, verified: bool = False, detail: str = ""):
        self.node["last_action"] = action
        if ok:
            self.node["last_status"] = "verified" if verified else "responded (unverified)"
            self.node["last_seen"] = _now()
            self.node["last_error"] = ""
            key = "fetched" if action == "fetch" else "sent"
            self.node[key] = int(self.node.get(key, 0)) + 1
        else:
            self.node["last_status"] = "unreachable"
            self.node["last_error"] = str(detail)[:200]
            self.node["failures"] = int(self.node.get("failures", 0)) + 1
        self.save()

    def export_portable_account(self, target_device_id: str) -> str:
        p = self.profile
        return (f"HTTPSH_PORTABLE_ACCOUNT::{p['identity']}::TARGET:{target_device_id}"
                f"::TIME:{p['created_at']}")

    def summary(self) -> str:
        p, n = self.profile, self.node
        lines = [
            f"Session: {p['identity']}",
            f"Mode: {p['mode']} (stored locally inside the vault)",
            f"Created: {p['created_at']}",
            f"Last active: {p['last_active']}",
            f"App starts: {p['boots']}",
            "Restored from a previous run: " + ("yes" if self.restored else "no (new session)"),
            "",
            f"Node: {n['node_url']}",
            f"Last status: {n['last_status']}",
            f"Last contact: {n['last_seen'] or 'never'}",
            f"Fetched: {n['fetched']}   Sent: {n['sent']}   Failures: {n['failures']}",
        ]
        if n["last_error"]:
            lines.append(f"Last error: {n['last_error']}")
        return "\n".join(lines)


# ------------------------------------------------------------------------------
# Colour / CSS helpers for imported formatting
# ------------------------------------------------------------------------------
_NAMED = {
    "black": (0, 0, 0), "white": (255, 255, 255), "gray": (128, 128, 128),
    "grey": (128, 128, 128), "silver": (192, 192, 192), "red": (255, 0, 0),
    "maroon": (128, 0, 0), "orange": (255, 165, 0), "yellow": (255, 255, 0),
    "olive": (128, 128, 0), "lime": (0, 255, 0), "green": (0, 128, 0),
    "aqua": (0, 255, 255), "cyan": (0, 255, 255), "teal": (0, 128, 128),
    "blue": (0, 0, 255), "navy": (0, 0, 128), "fuchsia": (255, 0, 255),
    "magenta": (255, 0, 255), "purple": (128, 0, 128), "pink": (255, 192, 203),
    "brown": (165, 42, 42), "gold": (255, 215, 0), "violet": (238, 130, 238),
    "indigo": (75, 0, 130), "crimson": (220, 20, 60),
}
_IGNORED_COLOR_WORDS = {"", "inherit", "initial", "unset", "currentcolor", "transparent",
                        "auto", "windowtext", "default", "revert"}


def rgb_to_tag(r: int, g: int, b: int):
    """Map an RGB colour to the nearest supported palette tag (None = plain/neutral)."""
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if s < 0.25 or v < 0.15:
        return None
    deg = h * 360

    def dist(name):
        d = abs(deg - _PALETTE_HUES[name])
        return min(d, 360 - d)
    return min(_PALETTE_HUES, key=dist)


def parse_css_color(value: str):
    v = value.strip().lower()
    if v in _IGNORED_COLOR_WORDS:
        return None
    if v in _NAMED:
        return rgb_to_tag(*_NAMED[v])
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return rgb_to_tag(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})"
                     r"\s*(?:[,/]\s*[\d.]+%?\s*)?\)", v)
    if m:
        return rgb_to_tag(*(min(255, int(x)) for x in m.groups()))
    raise UnsupportedCustomization(f"color {value!r}")


def parse_css_declarations(style: str) -> dict:
    out = {}
    for part in style.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.replace("!important", "").strip().lower()
    return out


def styles_from_css(props: dict) -> set:
    st = set()
    w = props.get("font-weight")
    if w and (w in ("bold", "bolder") or (w.isdigit() and int(w) >= 600)):
        st.add("bold")
    if props.get("font-style") in ("italic", "oblique"):
        st.add("italic")
    if "color" in props:
        tag = parse_css_color(props["color"])
        if tag:
            st.add(tag)
    for key in ("text-decoration", "text-decoration-line"):
        if any(x in props.get(key, "") for x in ("underline", "line-through", "overline")):
            raise UnsupportedCustomization(f"{key}: {props[key]}")
    for key in ("background", "background-color"):
        v = props.get(key)
        if v is not None and v not in ("none", "transparent", "white", "#fff", "#ffffff",
                                       "inherit", "initial", "unset"):
            raise UnsupportedCustomization(f"{key}: {v}")
    if props.get("vertical-align", "") in ("sub", "super"):
        raise UnsupportedCustomization("vertical-align")
    if "small-caps" in props.get("font-variant", ""):
        raise UnsupportedCustomization("font-variant")
    return st


def merge_spans(spans, length: int):
    by = {}
    for s, e, t in spans:
        s, e = max(0, s), min(length, e)
        if e > s and t in STYLE_TAG_NAMES:
            by.setdefault(t, []).append((s, e))
    out = []
    for t, rs in by.items():
        rs.sort()
        cs, ce = rs[0]
        for s, e in rs[1:]:
            if s <= ce:
                ce = max(ce, e)
            else:
                out.append((cs, ce, t))
                cs, ce = s, e
        out.append((cs, ce, t))
    return out


@dataclass
class Extracted:
    text: str
    spans: list = field(default_factory=list)      # (start, end, tag)
    notes: list = field(default_factory=list)      # human-readable remarks


class _TextBuilder:
    def __init__(self, cap: int):
        self.parts, self.length, self.spans = [], 0, []
        self.cap, self.full = cap, False

    def add(self, text: str, styles=()):
        if not text or self.full:
            return
        room = self.cap - self.length
        if len(text) > room:
            text, self.full = text[:room], True
        start = self.length
        self.parts.append(text)
        self.length += len(text)
        for st in styles:
            self.spans.append((start, self.length, st))

    def last_char(self) -> str:
        return self.parts[-1][-1:] if self.parts else ""

    def ensure_newline(self):
        if self.length and self.last_char() != "\n":
            self.add("\n")


def _finish(b: _TextBuilder, stats: dict) -> Extracted:
    text = "".join(b.parts)
    lead = len(text) - len(text.lstrip())
    text = text[lead:].rstrip()
    spans = merge_spans([(s - lead, e - lead, t) for s, e, t in b.spans], len(text))
    notes = []
    if stats.get("media"):
        notes.append(f"{stats['media']} image/media item(s) skipped (Httpsh is text-only)")
    if b.full:
        notes.append(f"import limited to the first {MAX_IMPORT_CHARS:,} characters")
    return Extracted(text, spans, notes)


# ------------------------------------------------------------------------------
# HTML / XHTML (also used for EPUB chapters)
# ------------------------------------------------------------------------------
class _RichHTMLParser(HTMLParser):
    VOID = {"br", "hr", "img", "input", "meta", "link", "source", "embed", "col",
            "wbr", "area", "base", "track", "param"}
    SKIP = {"script", "style", "head", "title", "noscript", "template", "svg", "video",
            "audio", "object", "iframe", "canvas", "picture", "math"}
    MEDIA_CONTAINERS = {"svg", "video", "audio", "object", "iframe", "canvas", "picture"}
    BLOCK = {"p", "div", "section", "article", "header", "footer", "blockquote", "pre",
             "ul", "ol", "table", "tr", "li", "dd", "dt", "figure", "figcaption", "body",
             "h1", "h2", "h3", "h4", "h5", "h6", "thead", "tbody", "tfoot", "caption"}
    BOLD = {"b", "strong", "th", "h1", "h2", "h3", "h4", "h5", "h6"}
    ITALIC = {"i", "em", "cite", "dfn"}
    UNSUPPORTED = {"u", "ins", "s", "strike", "del", "sub", "sup", "mark"}

    def __init__(self, builder: _TextBuilder, keep_format: bool, stats: dict):
        super().__init__(convert_charrefs=True)
        self.b, self.keep, self.stats = builder, keep_format, stats
        self.stack = []            # (tag, styles frozenset, is_skip)
        self.row_cells = False

    def _skipping(self) -> bool:
        return any(e[2] for e in self.stack)

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, True)

    def _start(self, tag, attrs, selfclosing):
        tag = tag.lower()
        skipping = self._skipping()
        if tag in self.VOID or selfclosing:
            if not skipping:
                if tag == "br":
                    self.b.add("\n")
                elif tag == "hr":
                    self.b.ensure_newline()
                elif tag in ("img", "embed"):
                    self.stats["media"] = self.stats.get("media", 0) + 1
            return
        if skipping or tag in self.SKIP:
            if tag in self.MEDIA_CONTAINERS and not skipping:
                self.stats["media"] = self.stats.get("media", 0) + 1
            self.stack.append((tag, frozenset(), True))
            return
        styles = self._styles_for(tag, dict(attrs))
        self.stack.append((tag, styles, False))
        if tag in self.BLOCK:
            self.b.ensure_newline()
        if tag == "li":
            self.b.add("• ")
        elif tag == "tr":
            self.row_cells = False
        elif tag in ("td", "th"):
            if self.row_cells:
                self.b.add("\t")
            self.row_cells = True

    def _styles_for(self, tag, attrs):
        if not self.keep:
            return frozenset()
        if tag in self.UNSUPPORTED:
            raise UnsupportedCustomization(f"<{tag}>")
        st = set()
        if tag in self.BOLD:
            st.add("bold")
        if tag in self.ITALIC:
            st.add("italic")
        if tag == "font" and "color" in attrs:
            c = parse_css_color(attrs["color"])
            if c:
                st.add(c)
        if attrs.get("style"):
            st |= styles_from_css(parse_css_declarations(attrs["style"]))
        return frozenset(st)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        else:
            return
        if tag in self.BLOCK and not self._skipping():
            self.b.ensure_newline()

    def handle_data(self, data):
        if not data or self._skipping():
            return
        if any(e[0] == "pre" for e in self.stack):
            text = data
        else:
            text = re.sub(r"\s+", " ", data)
            if self.b.last_char() in ("", "\n", "\t", " "):
                text = text.lstrip(" ")
        if not text:
            return
        styles = set()
        for e in self.stack:
            styles |= e[1]
        self.b.add(text, styles)


# ------------------------------------------------------------------------------
# DOCX / EPUB helpers
# ------------------------------------------------------------------------------
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _w(tag: str) -> str:
    return _W + tag


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _check_xml(data: bytes):
    if re.search(rb"<!(doctype|entity)", data, re.IGNORECASE):
        raise ValueError("Unsafe XML (DOCTYPE/ENTITY) rejected.")


def _onoff(el) -> bool:
    return (el.get(_w("val")) or "true").lower() not in ("0", "false", "off", "none")


class _DocxReader:
    def __init__(self, builder, keep, stats):
        self.b, self.keep, self.stats = builder, keep, stats

    def block(self, el):
        if el.tag == _w("p"):
            self.paragraph(el, True)
        elif el.tag == _w("tbl"):
            self.table(el)
        elif el.tag == _w("sdt"):
            content = el.find(_w("sdtContent"))
            if content is not None:
                for c in content:
                    self.block(c)

    def paragraph(self, p, newline):
        base = set()
        ppr = p.find(_w("pPr"))
        if ppr is not None:
            ps = ppr.find(_w("pStyle"))
            if ps is not None and self.keep:
                if (ps.get(_w("val")) or "").lower().startswith(("heading", "title")):
                    base.add("bold")
            if ppr.find(_w("numPr")) is not None:
                self.b.add("• ")
        for child in p:
            self.inline(child, base)
        if newline:
            self.b.add("\n")

    def inline(self, el, base):
        if el.tag == _w("r"):
            self.run(el, base)
        elif _local(el.tag) in ("hyperlink", "ins", "smartTag", "fldSimple"):
            for sub in el:
                self.inline(sub, base)

    def run(self, r, base):
        styles = set(base) if self.keep else set()
        rpr = r.find(_w("rPr"))
        if rpr is not None and self.keep:
            el = rpr.find(_w("b"))
            if el is not None and _onoff(el):
                styles.add("bold")
            el = rpr.find(_w("i"))
            if el is not None and _onoff(el):
                styles.add("italic")
            el = rpr.find(_w("color"))
            if el is not None:
                v = (el.get(_w("val")) or "auto").lower()
                if v != "auto":
                    if not re.fullmatch(r"[0-9a-f]{6}", v):
                        raise UnsupportedCustomization(f"color {v!r}")
                    tag = rgb_to_tag(int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
                    if tag:
                        styles.add(tag)
            el = rpr.find(_w("u"))
            if el is not None and (el.get(_w("val")) or "single").lower() != "none":
                raise UnsupportedCustomization("underline")
            for name in ("strike", "dstrike", "smallCaps", "caps"):
                el = rpr.find(_w(name))
                if el is not None and _onoff(el):
                    raise UnsupportedCustomization(name)
            el = rpr.find(_w("highlight"))
            if el is not None and (el.get(_w("val")) or "none").lower() != "none":
                raise UnsupportedCustomization("highlight")
            el = rpr.find(_w("vertAlign"))
            if el is not None and (el.get(_w("val")) or "") in ("superscript", "subscript"):
                raise UnsupportedCustomization("vertAlign")
        for node in r:
            t = node.tag
            if t == _w("t"):
                self.b.add(node.text or "", styles)
            elif t == _w("tab"):
                self.b.add("\t")
            elif t in (_w("br"), _w("cr")):
                self.b.add("\n")
            elif t == _w("noBreakHyphen"):
                self.b.add("-", styles)
            elif t in (_w("drawing"), _w("pict"), _w("object")):
                self.stats["media"] = self.stats.get("media", 0) + 1

    def table(self, tbl):
        for tr in tbl.findall(_w("tr")):
            first = True
            for tc in tr.findall(_w("tc")):
                if not first:
                    self.b.add("\t")
                first = False
                for i, p in enumerate(tc.findall(_w("p"))):
                    if i:
                        self.b.add(" ")
                    self.paragraph(p, False)
            self.b.ensure_newline()


def _decode_bytes(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1256")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _read_capped(path: Path) -> bytes:
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("The file is larger than 50 MB.")
    return path.read_bytes()


# ------------------------------------------------------------------------------
# Text extraction (books, documents, images)
# ------------------------------------------------------------------------------
class TextExtractor:
    """Returns Extracted(text, spans, notes).

    keep_format=True  : bold / italic / palette colours are preserved. Any style we
                        cannot represent (underline, strike-through, highlight,
                        sub/superscript, small caps, unparsable colours) raises
                        UnsupportedCustomization -> the UI shows UNSUPPORTED_MSG.
    keep_format=False : all styling is ignored; never raises UnsupportedCustomization
                        for styles (unknown file types still do).
    Fonts, font sizes and alignment are normalised by the editor and never an error.
    """

    PLAIN_EXT = {".txt", ".md", ".csv", ".tsv", ".log", ".json"}
    HTML_EXT = {".html", ".htm", ".xhtml"}
    IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}

    @classmethod
    def dialog_patterns(cls):
        exts = sorted(cls.PLAIN_EXT | cls.HTML_EXT | cls.IMAGE_EXT | {".docx", ".epub", ".pdf"})
        return " ".join("*" + e for e in exts)

    @classmethod
    def extract(cls, path, keep_format: bool = True) -> Extracted:
        path = Path(path)
        ext = path.suffix.lower()
        try:
            if ext in cls.PLAIN_EXT:
                return cls._plain(path)
            if ext in cls.HTML_EXT:
                return cls._html(path, keep_format)
            if ext == ".docx":
                return cls._docx(path, keep_format)
            if ext == ".epub":
                return cls._epub(path, keep_format)
            if ext == ".pdf":
                return cls._pdf(path)
            if ext in cls.IMAGE_EXT:
                return cls._image(path)
        except (zipfile.BadZipFile, ET.ParseError, KeyError) as e:
            raise ValueError(f"The file is damaged or is not a valid '{ext}' file.") from e
        raise UnsupportedCustomization(f"extraction rule for '{ext}' files")

    @staticmethod
    def _plain(path: Path) -> Extracted:
        text = _decode_bytes(_read_capped(path))
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        notes = []
        if len(text) > MAX_IMPORT_CHARS:
            text = text[:MAX_IMPORT_CHARS]
            notes.append(f"import limited to the first {MAX_IMPORT_CHARS:,} characters")
        return Extracted(text.rstrip(), [], notes)

    @staticmethod
    def _html(path: Path, keep: bool) -> Extracted:
        b, stats = _TextBuilder(MAX_IMPORT_CHARS), {"media": 0}
        p = _RichHTMLParser(b, keep, stats)
        p.feed(_decode_bytes(_read_capped(path)))
        p.close()
        return _finish(b, stats)

    @staticmethod
    def _docx(path: Path, keep: bool) -> Extracted:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_IMPORT_BYTES:
                raise ValueError("The document is too large.")
            data = z.read("word/document.xml")
        _check_xml(data)
        root = ET.fromstring(data)
        body = root.find(_w("body"))
        if body is None:
            raise ValueError("The .docx file has no body.")
        b, stats = _TextBuilder(MAX_IMPORT_CHARS), {"media": 0}
        reader = _DocxReader(b, keep, stats)
        for el in body:
            if b.full:
                break
            reader.block(el)
        return _finish(b, stats)

    @staticmethod
    def _epub(path: Path, keep: bool) -> Extracted:
        b, stats = _TextBuilder(MAX_IMPORT_CHARS), {"media": 0}
        with zipfile.ZipFile(path) as z:
            container = z.read("META-INF/container.xml")
            _check_xml(container)
            rootfile = next((e.get("full-path") for e in ET.fromstring(container).iter()
                             if _local(e.tag) == "rootfile"), None)
            if not rootfile:
                raise ValueError("The EPUB container has no package file.")
            opf = z.read(rootfile)
            _check_xml(opf)
            manifest, spine = {}, []
            for e in ET.fromstring(opf).iter():
                name = _local(e.tag)
                if name == "item":
                    manifest[e.get("id")] = (e.get("href") or "", e.get("media-type") or "")
                elif name == "itemref":
                    spine.append(e.get("idref"))
            base = posixpath.dirname(rootfile)
            for ref in spine:
                if b.full:
                    break
                href, mtype = manifest.get(ref, ("", ""))
                if not href or mtype not in ("application/xhtml+xml", "text/html"):
                    continue
                name = posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0])))
                try:
                    info = z.getinfo(name)
                except KeyError:
                    continue
                if info.file_size > MAX_IMPORT_BYTES:
                    continue
                parser = _RichHTMLParser(b, keep, stats)
                parser.feed(z.read(name).decode("utf-8", errors="replace"))
                parser.close()
                b.ensure_newline()
                b.add("\n")
        return _finish(b, stats)

    @staticmethod
    def _pdf(path: Path) -> Extracted:
        try:
            from importlib import import_module
            PdfReader = import_module("pypdf").PdfReader
        except ImportError:
            raise ExtractionUnavailable("PDF import needs the 'pypdf' package:\n\n"
                                        "  pip install pypdf")
        reader = PdfReader(str(path))
        if getattr(reader, "is_encrypted", False):
            raise ValueError("The PDF is password protected.")
        b = _TextBuilder(MAX_IMPORT_CHARS)
        for page in reader.pages:
            if b.full:
                break
            b.add(page.extract_text() or "")
            b.ensure_newline()
            b.add("\n")
        return _finish(b, {})

    @staticmethod
    def _image(path: Path) -> Extracted:
        try:
            from importlib import import_module
            pytesseract = import_module("pytesseract")
            Image = import_module("PIL").Image
        except ImportError:
            raise ExtractionUnavailable("Image OCR needs extra packages:\n\n"
                                        "  pip install pytesseract pillow\n\n"
                                        "and the Tesseract OCR engine installed on this computer.")
        with Image.open(path) as img:
            try:
                text = pytesseract.image_to_string(img, lang="ara+eng")
            except Exception:
                text = pytesseract.image_to_string(img, lang="eng")
        text = text.replace("\r\n", "\n").replace("\x00", "").strip()
        return Extracted(text, [], ["OCR text: formatting is not preserved for images"])


# ------------------------------------------------------------------------------
# The original Httpsh resource-registry engine (legacy API, now vault-aware)
# ------------------------------------------------------------------------------
class HttpshStandardEngine:
    """Reference engine for the Httpsh Standard (v1.2).

    Pass `conn` (and `cipher`) to share the app's vault database; otherwise a
    standalone SQLite file is used. Encryption uses AES-256-GCM, never XOR."""

    def __init__(self, vault_filename="httpsh_sovereign_vault.db", hardware_ram_gb: float = 2.0,
                 conn=None, cipher=None):
        self.vault_filename = vault_filename
        self.ram_gb = hardware_ram_gb
        self.cipher = cipher
        self._owns_conn = conn is None
        self.conn = conn if conn is not None else sqlite3.connect(vault_filename)
        self._enforce_resource_profile()
        self._init_sovereign_vault()

        self.themes = {
            "white": {"bg": "\033[47m", "fg": "\033[30m", "name": "Default Clean White"},
            "pink": {"bg": "\033[105m", "fg": "\033[30m", "name": "Soft Pink"},
            "blue": {"bg": "\033[44m", "fg": "\033[97m", "name": "Deep Blue"},
            "green": {"bg": "\033[42m", "fg": "\033[30m", "name": "Terminal Green"},
            "yellow": {"bg": "\033[43m", "fg": "\033[30m", "name": "Warm Yellow"},
            "red": {"bg": "\033[41m", "fg": "\033[97m", "name": "Alert Red"},
            "purple": {"bg": "\033[45m", "fg": "\033[97m", "name": "Cyber Purple"},
            "black": {"bg": "\033[40m", "fg": "\033[97m", "name": "Dark Mode Black"},
        }
        self.reset_code = "\033[0m"

    def close(self):
        if self._owns_conn:
            self.conn.close()

    def _enforce_resource_profile(self):
        mode = "Ultra-Lean Local Mode" if self.ram_gb <= 2.0 else "Standard Performance Mode"
        print(f"[Httpsh Protocol by op] Hardware Profile: {self.ram_gb}GB RAM -> [{mode} Active].")

    def _init_sovereign_vault(self):
        with self.conn:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS standard_registry (
                    resource_id TEXT PRIMARY KEY,
                    uri TEXT,
                    category_header TEXT,
                    compliance_status TEXT,
                    encrypted_payload TEXT
                )''')

    # -- sealing -------------------------------------------------------------
    def _seal(self, text: str, user_secret, resource_id: str) -> str:
        aad = f"registry:{resource_id}"
        if user_secret:
            if not HAS_CRYPTO:
                raise RuntimeError("The 'cryptography' package is required for encryption.")
            salt = os.urandom(16)
            c = VaultCrypto(VaultCrypto.derive_key(user_secret, salt))
            return "S1:" + base64.b64encode(salt).decode("ascii") + ":" + c.encrypt(text, aad)
        if self.cipher:
            return "V1:" + self.cipher.encrypt(text, aad)
        raise RuntimeError("No encryption available: give a secret (needs 'cryptography') "
                           "or construct the engine with an unlocked vault cipher.")

    def open_resource(self, resource_id: str, user_secret=None) -> str:
        row = self.conn.execute("SELECT encrypted_payload FROM standard_registry "
                                "WHERE resource_id = ?", (resource_id,)).fetchone()
        if not row:
            raise KeyError(resource_id)
        aad = f"registry:{resource_id}"
        token = row[0]
        if token.startswith("S1:"):
            _, salt_b64, body = token.split(":", 2)
            c = VaultCrypto(VaultCrypto.derive_key(user_secret or "", base64.b64decode(salt_b64)))
            return c.decrypt(body, aad)
        if token.startswith("V1:") and self.cipher:
            return self.cipher.decrypt(token[3:], aad)
        raise RuntimeError("This resource cannot be opened with the supplied credentials.")

    def sanitize_raw_text(self, text: str) -> str:
        return HttpshClient.sanitize(text)

    def process_and_validate_resource(self, resource_id: str, uri: str, headers: dict,
                                      payload: str, user_secret=None) -> bool:
        if self.conn.execute("SELECT 1 FROM standard_registry WHERE resource_id = ?",
                             (resource_id,)).fetchone():
            return self._collision(resource_id)
        if MEDIA_PATTERN.search(payload):
            print("\n❌ [Httpsh Strict Violation]: Payload contains images or media references!")
            print("🚨 [Media Rejected]: Protocol forbids images completely. Processing aborted.")
            return False

        category = headers.get("Category", "UNCATEGORIZED").strip()
        print(f"\n[Httpsh Compliance OK]: Resource verified under category: [{category}]")
        raw_data = f"URI:{uri} | Cat:{category} | RawPayload:{self.sanitize_raw_text(payload)}"
        try:
            sealed = self._seal(raw_data, user_secret, resource_id)
        except RuntimeError as e:
            print(f"❌ [Encryption Error]: {e}")
            return False
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO standard_registry (resource_id, uri, category_header, "
                    "compliance_status, encrypted_payload) VALUES (?, ?, ?, ?, ?)",
                    (resource_id, uri, category, "COMPLIANT_SECURE", sealed))
        except sqlite3.IntegrityError:
            return self._collision(resource_id)
        print(f"✅ [Success]: Resource '{resource_id}' securely registered in the sovereign vault.")
        return True

    @staticmethod
    def _collision(resource_id: str) -> bool:
        print(f"\n❌ [Namespace Collision]: Resource ID '{resource_id}' is already taken!")
        print("🚨 [Sovereign Rule]: Uniqueness enforced in this vault. Choose another identifier.")
        return False

    def export_to_text_file(self, resource_id: str, output_filepath: str = None) -> str:
        row = self.conn.execute("SELECT uri, category_header, encrypted_payload "
                                "FROM standard_registry WHERE resource_id = ?",
                                (resource_id,)).fetchone()
        if not row:
            print(f"[Export Error]: Resource ID '{resource_id}' not found in vault.")
            return ""
        uri, category, enc_payload = row
        output_filepath = output_filepath or f"export_{resource_id}.httpsh"
        content = ("--- Httpsh Sovereign Text Standard Export (by op) ---\n"
                   f"URI: {uri}\nCategory: {category}\n"
                   f"Export Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   "---------------------------------------------------\n\n"
                   f"[ENCRYPTED_VAULT_PAYLOAD]: {enc_payload}\n")
        with open(output_filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"\n📄 [Exporter by op]: Successfully exported file to -> '{output_filepath}'")
        return output_filepath

    def render_windows_stream(self, title: str, lines: list, theme_mode: str = "white",
                              custom_pool: list = None):
        key = "white"
        if theme_mode == "dynamic_default":
            key = random.choice(list(self.themes))
        elif theme_mode == "dynamic_custom" and custom_pool:
            key = random.choice(custom_pool[:5])
        elif theme_mode in self.themes:
            key = theme_mode
        th = self.themes.get(key, self.themes["white"])
        paint = f"{th['bg']}{th['fg']}"
        print(f"\n{paint}=== [Httpsh Standard Engine v1.2 | op: {title}] ==={self.reset_code}")
        print(f"{paint}[Rules]: Zero-Media | Non-Interactive Raw Text | Theme -> {th['name']}{self.reset_code}")
        for idx, line in enumerate(lines, start=1):
            print(f"{paint}  [Raw Line #{idx}]: {self.sanitize_raw_text(line)}{self.reset_code}")
        print(f"{paint}===================================================={self.reset_code}")

    def enforce_strict_payload_type(self, payload_type: str, content: str) -> bool:
        ok, reason = HttpshClient.validate_payload(payload_type, content)
        if not ok:
            print(f"\n❌ [Httpsh Security Violation]: {reason}")
            return False
        print(f"\n✅ [Security Approved]: Payload type [{payload_type.strip().upper()}] accepted by op's standard.")
        return True

    def ingest_from_image_scanner(self, image_path: str, payload_type: str, user_secret,
                                  resource_id: str, uri: str, category: str) -> bool:
        print(f"\n📸 [Local OCR Scanner by op]: Scanning image source -> '{image_path}'...")
        try:
            text = TextExtractor.extract(image_path, keep_format=False).text
        except ExtractionUnavailable as e:
            print(f"❌ [OCR Unavailable]: {e}")
            return False
        except Exception as e:
            print(f"❌ [OCR Error]: Failed to process image payload -> {e}")
            return False
        if not text.strip():
            print("❌ [OCR Error]: No text found in the image.")
            return False
        print("✨ [OCR Success]: Image converted to raw text. Image data purged.")
        if not self.enforce_strict_payload_type(payload_type, text):
            return False
        return self.process_and_validate_resource(resource_id, uri, {"Category": category},
                                                  text, user_secret)

    def create_guest_session(self) -> dict:
        profile = EnopSession.new_guest_profile()
        print(f"\n👤 [Httpsh Guest Mode by op]: Active guest session initialized -> [{profile['identity']}]")
        print("🛡 [Privacy Shield]: Session data stays on this device (inside the vault).")
        return profile

    def export_portable_account(self, guest_profile: dict, target_device_id: str) -> str:
        block = (f"HTTPSH_PORTABLE_ACCOUNT::{guest_profile['identity']}"
                 f"::TARGET:{target_device_id}::TIME:{guest_profile['created_at']}")
        print("\n📦 [Portable Account by op]: Profile serialized for cross-device migration.")
        return block

    def render_sovereign_watermark(self):
        text = ("TOPFRA & Httpsh Standard: Designed & Architected by op. "
                "Special thanks for eliminating intrusive ads and securing pure text flow.")
        black = self.themes["black"]
        paint = f"{black['bg']}{black['fg']}"
        print(f"\n{paint}--- [SOVEREIGN SIGNATURE] ---{self.reset_code}")
        print(f"{paint}  {text}{self.reset_code}")
        print(f"{paint}----------------------------{self.reset_code}")
        return text


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("--- Httpsh Protocol Engine Initialized Successfully ---")
    engine = HttpshStandardEngine()
    engine.render_sovereign_watermark()
    engine.render_windows_stream("Test Node 01", [
        "Welcome to the sovereign local-first revolution.",
        "Zero tracking, zero media, absolute text sovereignty.",
        "Engineered by op for low-resource secure environments."], theme_mode="green")
    engine.close()