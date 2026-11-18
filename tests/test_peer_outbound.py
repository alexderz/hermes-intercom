"""Outbound peers: Bot Chat posts to peer:<name>, and a reply is the block only (peers.md P2)."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import ipaddress
import json
import logging
import os
import shutil
import socket
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import traceback
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import peer_inbound
import peers
import profiles
import record
import replies
import route

PACKAGE_DIR = Path(__file__).resolve().parents[1]
BOT = "local-bot"
TIP = "local-bot-tip"
OTHER = "local-other"
PEER = "remote-bot"
HMAC_SECRET = "hmac-secret-77ab"
URL_SECRET = "s3cret-token-9f3a"
BEARER_KEY = "bearer-key-6c1e8a40"
ONLY_BOT_CHAT = "only Bot Chat talks to remote agents; message Bot Chat"
WAITING = "a remote agent cannot hold your turn; use requested"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self._take("POST")

    def do_GET(self):
        self._take("GET")

    def _take(self, method):
        server = self.server
        with server.lock:
            server.inflight += 1
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else b""
            item = {"method": method, "path": self.path, "body": body,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "row": _row_at_post(server.home, body)}
            with server.lock:
                server.seen.append(item)
            if server.delay:
                time.sleep(server.delay)
            # hmac-v2 posts the bare hook. A bad or missing signature is 401; url-auth
            # posts the secret in the path and carries no signature. bearer posts
            # somewhere other than /hook and carries Authorization, not a signature.
            if method == "POST" and self.path == "/hook" and not self._hmac_ok(body):
                self._respond(401)
            else:
                self._respond()
        finally:
            with server.lock:
                server.inflight -= 1

    def _hmac_ok(self, body):
        stamp = self.headers.get("X-Webhook-Timestamp")
        signature = self.headers.get("X-Webhook-Signature-V2")
        if not stamp or not signature:
            return False
        expected = hmac.new(HMAC_SECRET.encode("utf-8"), stamp.encode("utf-8") + b"." + body,
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def _respond(self, status=None):
        if status is None and self.server.raw_response is not None:
            self.close_connection = True
            try:
                self.wfile.write(self.server.raw_response)
                self.wfile.flush()
            except OSError:
                return
            return
        status = self.server.status if status is None else status
        try:
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", self.server.location)
                self.send_header("Content-Length", "0")
            else:
                body = b"ok"
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if not (300 <= status < 400):
                self.wfile.write(b"ok")
        except OSError:
            return

    def log_message(self, fmt, *args):
        return


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, certfile, keyfile):
        super().__init__(("127.0.0.1", 0), _Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        self.ssl_context = context
        self.delay = 0
        self.status = 200
        self.raw_response = None
        self.location = ""
        self.seen = []
        self.inflight = 0
        self.lock = threading.Lock()
        self.home = None

    def get_request(self):
        sock, addr = super().get_request()
        try:
            return self.ssl_context.wrap_socket(sock, server_side=True), addr
        except OSError:
            sock.close()
            raise

    def handle_error(self, request, client_address):
        return


def _row_at_post(home, body):
    """The record row as the POST arrives: delivery_path is set, result is still empty."""
    if home is None:
        return None
    try:
        message_id = json.loads(body).get("message_id")
    except (ValueError, AttributeError, TypeError):
        return None
    path = Path(home) / "plugin-data" / "intercom" / "record.db"
    if not message_id or not path.is_file():
        return None
    conn = sqlite3.connect(path, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def _name(common):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _leaf(ca_key, ca_name, ski, subject, eku, san, path):
    """An end-entity cert Python 3.14's VERIFY_X509_STRICT will accept."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .add_extension(san, critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
                       critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_pem(path, cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


def _write_pem(path, data):
    Path(path).write_bytes(data)


def _make_certs(directory):
    """A throwaway CA and leaves. Trust is only the CA file the test points SSL_CERT_FILE at."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = _name("outbound-test-ca")
    ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    now = datetime.now(timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    directory = Path(directory)
    ca_file = directory / "ca.pem"
    cert_file = directory / "server.pem"
    key_file = directory / "server.key"
    client_file = directory / "client.pem"
    _write_pem(ca_file, ca_cert.public_bytes(serialization.Encoding.PEM))
    server_key, _server = _leaf(
        ca_key, ca_name, ski, _name("127.0.0.1"), ExtendedKeyUsageOID.SERVER_AUTH,
        x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
        cert_file)
    _write_pem(key_file, server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    _leaf(ca_key, ca_name, ski, _name("peer-client"), ExtendedKeyUsageOID.CLIENT_AUTH,
          x509.SubjectAlternativeName([x509.DNSName("peer-client.invalid")]), client_file)
    other = directory / "other-ca.pem"
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_name = _name("other-test-ca")
    other_ski = x509.SubjectKeyIdentifier.from_public_key(other_key.public_key())
    other_cert = (
        x509.CertificateBuilder()
        .subject_name(other_name)
        .issuer_name(other_name)
        .public_key(other_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(other_ski, critical=False)
        .sign(other_key, hashes.SHA256())
    )
    _write_pem(other, other_cert.public_bytes(serialization.Encoding.PEM))
    return {"ca": ca_file, "cert": cert_file, "key": key_file, "client": client_file, "other": other}


def _load_plugin(name):
    spec = importlib.util.spec_from_file_location(
        name, PACKAGE_DIR / "__init__.py", submodule_search_locations=[str(PACKAGE_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Log(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class FakeCtx:
    def __init__(self, **settings):
        self.settings = settings
        self.tools = []
        self.hooks = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_hook(self, *args, **kwargs):
        self.hooks.append(args)

    def register_cli_command(self, *args, **kwargs):
        return None


class FakeOwner:
    def __init__(self):
        self.calls = []

    def __call__(self, target, text, delivery, submission_id):
        self.calls.append({"target": target, "text": text, "delivery": delivery})
        return {"ok": True, "applied": "steer", "applied_detail": None, "admission_id": None}


class OutboundTestBase(unittest.TestCase):
    certs = None
    server = None

    @classmethod
    def setUpClass(cls):
        cls.cert_dir = Path(tempfile.mkdtemp(prefix="intercom-peer-certs-"))
        cls.certs = _make_certs(cls.cert_dir)
        cls.server = _Server(cls.certs["cert"], cls.certs["key"])
        cls.server.location = f"https://127.0.0.1:{cls.server.server_address[1]}/stolen"
        cls.thread = threading.Thread(target=cls.server.serve_forever, name="peer-fake-https",
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        server = cls.server
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = getattr(cls, "thread", None)
        if thread is not None:
            thread.join(5)
        shutil.rmtree(cls.cert_dir, ignore_errors=True)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="intercom-peer-out-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HERMES_HOME": str(self.home),
            "SSL_CERT_FILE": str(self.certs["ca"]),
        })
        env.start()
        self.addCleanup(env.stop)
        (self.home / "config.yaml").write_text(
            "plugins:\n  enabled: [intercom]\n  entries:\n    intercom:\n"
            "      allow_gateway_injection: true\n      settings: {user_name: Sam}\n",
            encoding="utf-8")
        self.port = self.server.server_address[1]
        self.hook_url = f"https://127.0.0.1:{self.port}/hook"
        self.secret_url = f"{self.hook_url}/{URL_SECRET}"
        self.hmac_file = self.home / "hmac.secret"
        self.url_file = self.home / "url.secret"
        self.bearer_file = self.home / "bearer.secret"
        self.bearer_url = f"https://127.0.0.1:{self.port}/routine"
        self.hmac_file.write_text(HMAC_SECRET + "\n", encoding="utf-8")
        self.url_file.write_text(self.secret_url + "\n", encoding="utf-8")
        self.bearer_file.write_text(BEARER_KEY + "\n", encoding="utf-8")
        self.write_peers([self.peer(PEER)])
        self.ctx = FakeCtx(user_name="Sam")
        self.log = _Log()
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.log)
        self.addCleanup(root.setLevel, previous_level)
        self.addCleanup(root.removeHandler, self.log)
        self.server.home = self.home
        self.server.delay = 0
        self.server.status = 200
        self.server.raw_response = None
        self.server.seen.clear()
        ctx_patch = mock.patch.dict(replies._ctx, {"ctx": self.ctx})
        ctx_patch.start()
        self.addCleanup(ctx_patch.stop)

        from hermes_state import SessionDB

        self.db = SessionDB()
        self.addCleanup(self.db.close)
        self.add_session(BOT, "Bot Chat")
        self.add_session(OTHER, "Other")
        self.assertEqual(profiles.current(), "default")

    def tearDown(self):
        self.server.delay = 0
        deadline = time.time() + 5
        while self.server.inflight and time.time() < deadline:
            time.sleep(0.02)

    def add_session(self, session_id, title, *, source="tui", parent=None):
        kwargs = {"parent_session_id": parent} if parent else {}
        self.db.create_session(session_id, source=source, **kwargs)
        if title:
            self.db.set_session_title(session_id, title)

    def peer(self, name, *, auth="hmac-v2", url=None, secret_file=None):
        if secret_file is None:
            secret_file = {"hmac-v2": self.hmac_file, "bearer": self.bearer_file}.get(auth, self.url_file)
        if url is None:
            url = {"url": f"{self.hook_url}/\u2026", "bearer": self.bearer_url}.get(auth, self.hook_url)
        return {"name": name, "url": url, "auth": auth, "secret_file": str(secret_file)}

    def write_peers(self, data):
        folder = self.home / "plugin-data" / "intercom"
        folder.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data)
        (folder / "peers.json").write_text(text, encoding="utf-8")

    def rows(self, home=None):
        path = Path(home or self.home) / "plugin-data" / "intercom" / "record.db"
        if not path.is_file():
            return []
        conn = sqlite3.connect(path, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM messages ORDER BY created_at, id")]
        finally:
            conn.close()

    def send(self, message="hello from bot chat", *, sender=BOT, to=None, reply_mode="none", **kwargs):
        return route.send(sender, to if to is not None else f"peer:{PEER}", message,
                          ctx=self.ctx, reply_mode=reply_mode, **kwargs)

    def assert_clean(self, *blobs):
        text = "\n".join(self.log.lines)
        for blob in blobs:
            text += "\n" + json.dumps(blob, default=str)
        for row in self.rows():
            text += "\n" + json.dumps(row, default=str)
        for secret in (HMAC_SECRET, URL_SECRET, BEARER_KEY):
            self.assertNotIn(secret, text)

    @staticmethod
    def _drop_module(name):
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)

    def assert_refused(self, receipt, error, message=ONLY_BOT_CHAT):
        self.assertFalse(receipt["ok"], receipt)
        self.assertEqual(receipt["error"], error)
        self.assertEqual(receipt["message"], message)
        self.assertEqual(receipt["receipt"], "refused")
        self.assertIsNone(receipt["message_id"])
        self.assertEqual(receipt.get("exit"), 2)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.server.seen, [])
        self.assert_clean(receipt)


class CertTests(OutboundTestBase):
    def test_throwaway_certs_carry_strict_extensions(self):
        ca = x509.load_pem_x509_certificate(self.certs["ca"].read_bytes())
        basic = ca.extensions.get_extension_for_class(x509.BasicConstraints)
        self.assertTrue(basic.critical)
        self.assertTrue(basic.value.ca)
        usage = ca.extensions.get_extension_for_class(x509.KeyUsage)
        self.assertTrue(usage.critical)
        self.assertTrue(usage.value.key_cert_sign)
        self.assertTrue(usage.value.crl_sign)
        ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)

        leaf = x509.load_pem_x509_certificate(self.certs["cert"].read_bytes())
        aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        self.assertEqual(aki.value.key_identifier, ski.value.digest)
        self.assertIsNone(aki.value.authority_cert_issuer)
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        self.assertIn(ipaddress.IPv4Address("127.0.0.1"), san.value.get_values_for_type(x509.IPAddress))
        eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH, eku.value)

        client = x509.load_pem_x509_certificate(self.certs["client"].read_bytes())
        client_eku = client.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        self.assertIn(ExtendedKeyUsageOID.CLIENT_AUTH, client_eku.value)
        client_aki = client.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        self.assertEqual(client_aki.value.key_identifier, ski.value.digest)
        client_san = client.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        self.assertEqual(client_san.value.get_values_for_type(x509.DNSName), ["peer-client.invalid"])

    def test_timeout_is_thirty_seconds(self):
        self.assertEqual(peers.TIMEOUT_SECONDS, 30)


class RefusalTests(OutboundTestBase):
    def test_each_gate_refuses_with_nothing_recorded(self):
        cases = [
            ("profile", {"profile": "default"}),
            ("profile-named", {"profile": "spike"}),
            ("script", {"script": True}),
            ("ask", {"kind": "ask", "reply_mode": "waiting"}),
            ("passive", {"delivery": "passive"}),
            ("key", {"key": "build-state"}),
            ("within", {"within": 5}),
            ("not-bot-chat", {"sender": OTHER}),
            ("not-a-session", {"sender": "local-missing"}),
            ("blank-sender", {"sender": "  "}),
        ]
        expected = {
            "profile": "profile", "profile-named": "profile", "script": "script", "ask": "ask",
            "passive": "passive", "key": "key", "within": "within", "not-bot-chat": "not-bot-chat",
            "not-a-session": "not-bot-chat", "blank-sender": "not-bot-chat",
        }
        for name, kwargs in cases:
            with self.subTest(name=name):
                self.server.seen.clear()
                receipt = self.send(**kwargs)
                self.assert_refused(receipt, expected[name])

    def test_gate_order_beats_a_later_reason(self):
        # not-default is first, then profile, then script, then ask, then passive, then key.
        spike = self._spike()
        with profiles.in_profile("spike"):
            receipt = self.send(profile="spike", script=True, kind="ask", reply_mode="waiting",
                                delivery="passive", key="build-state", within=5)
        self.assert_refused(receipt, "not-default")
        self.assertEqual(self.rows(spike), [])

        receipt = self.send(profile="default", script=True, kind="ask", reply_mode="waiting")
        self.assert_refused(receipt, "profile")
        receipt = self.send(script=True, kind="ask", reply_mode="waiting", delivery="passive")
        self.assert_refused(receipt, "script")
        receipt = self.send(kind="ask", reply_mode="waiting", delivery="passive", key="build-state")
        self.assert_refused(receipt, "ask")
        receipt = self.send(delivery="passive", key="build-state", within=5)
        self.assert_refused(receipt, "passive")
        receipt = self.send(key="build-state", within=5)
        self.assert_refused(receipt, "key")

    def test_a_named_profile_is_not_default_even_for_its_bot_chat(self):
        spike = self._spike()
        from hermes_state import SessionDB

        db = SessionDB(db_path=spike / "state.db")
        self.addCleanup(db.close)
        db.create_session(BOT, source="tui")
        db.set_session_title(BOT, "Bot Chat")
        (spike / "plugin-data" / "intercom").mkdir(parents=True)
        (spike / "plugin-data" / "intercom" / "peers.json").write_text(
            json.dumps([self.peer(PEER)]), encoding="utf-8")
        with profiles.in_profile("spike"):
            receipt = self.send()
        self.assert_refused(receipt, "not-default")
        self.assertEqual(self.rows(spike), [])

    def test_only_the_compression_tip_of_bot_chat_may_send(self):
        self.db.end_session(BOT, "compression")
        self.add_session(TIP, None, parent=BOT)
        parent = self.send(sender=BOT)
        self.assert_refused(parent, "not-bot-chat")
        receipt = self.send(sender=TIP, message="from the tip")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(json.loads(self.server.seen[-1]["body"])["text"], "from the tip")
        self.assert_clean(receipt)

    def test_unknown_peer_uses_the_bot_chat_sentence(self):
        for target in ("peer:nope", "peer:Bad", "peer:", "peer:" + "a" * 64, "peer:has_underscore"):
            with self.subTest(target=target):
                self.server.seen.clear()
                receipt = self.send(to=target)
                self.assert_refused(receipt, "unknown-peer")
        self.write_peers([])
        self.assert_refused(self.send(), "unknown-peer")

    def test_an_unknown_peer_beats_waiting(self):
        receipt = self.send(to="peer:nope", reply_mode="waiting")
        self.assert_refused(receipt, "unknown-peer")

    def test_waiting_is_refused_for_a_known_peer(self):
        receipt = self.send(reply_mode="waiting")
        self.assertFalse(receipt["ok"], receipt)
        self.assertEqual(receipt["error"], "waiting")
        self.assertEqual(receipt["message"], WAITING)
        self.assertIsNone(receipt["message_id"])
        self.assertEqual(receipt.get("exit"), 2)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.server.seen, [])
        self.assert_clean(receipt)

    def test_reply_missing_is_still_route_and_records_nothing(self):
        receipt = route.send(BOT, f"peer:{PEER}", "hi", ctx=self.ctx, reply_mode=None)
        self.assertEqual(receipt["error"], "reply-missing")
        self.assertIsNone(receipt["message_id"])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.server.seen, [])
        receipt = route.send(BOT, f"peer:{PEER}", "hi", ctx=self.ctx, reply_mode="optional")
        self.assertEqual(receipt["error"], "reply-missing")
        self.assertEqual(self.rows(), [])

    def test_a_non_https_url_is_not_a_peer(self):
        self.write_peers([self.peer(PEER, url=f"http://127.0.0.1:{self.port}/hook")])
        self.assertEqual(peers.load(), {})
        self.assert_refused(self.send(), "unknown-peer")
        good = self.peer("other-agent")
        bad = self.peer(PEER, url=f"http://127.0.0.1:{self.port}/hook")
        self.write_peers([good, bad])
        self.assertEqual(peers.load(), {})
        self.assert_refused(self.send(to="peer:other-agent"), "unknown-peer")

    def test_a_non_https_secret_url_is_not_a_peer(self):
        self.write_peers([self.peer(PEER, auth="url")])
        self.url_file.write_text(f"http://127.0.0.1:{self.port}/hook/{URL_SECRET}\n", encoding="utf-8")
        receipt = self.send()
        self.assert_refused(receipt, "unknown-peer")

    def test_an_unreadable_secret_records_nothing_and_names_no_path(self):
        missing = self.home / "missing-secret"
        for auth, path in (
                ("hmac-v2", missing),
                ("url", self.home / "also-missing"),
                ("bearer", self.home / "bearer-missing")):
            with self.subTest(auth=auth):
                self.write_peers([self.peer(PEER, auth=auth, secret_file=path)])
                receipt = self.send()
                self.assertFalse(receipt["ok"], receipt)
                self.assertEqual(receipt["error"], "secret-unreadable")
                self.assertEqual(receipt["message"], peers.SECRET_UNREADABLE)
                self.assertNotIn("exit", receipt)
                self.assertIsNone(receipt["message_id"])
                self.assertNotIn("missing-secret", json.dumps(receipt))
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.server.seen, [])
                self.assert_clean(receipt)
        self.hmac_file.write_text("  \n", encoding="utf-8")
        self.write_peers([self.peer(PEER)])
        receipt = self.send()
        self.assertEqual(receipt["error"], "secret-unreadable")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.server.seen, [])

    def test_a_bearer_key_with_whitespace_or_a_control_is_unreadable(self):
        cases = {
            "empty": "",
            "blank": " \n\t",
            "space": f"ab {BEARER_KEY}",
            "tab": "ab\tcd",
            "newline": f"{BEARER_KEY}\nmore",
            "cr": "ab\rcd",
            "nul": "ab\x00cd",
            "del": "ab\x7fcd",
            "prefixed": f"Bearer {BEARER_KEY}",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.server.seen.clear()
                self.bearer_file.write_text(text, encoding="utf-8")
                self.write_peers([self.peer(PEER, auth="bearer")])
                receipt = self.send()
                self.assertFalse(receipt["ok"], receipt)
                self.assertEqual(receipt["error"], "secret-unreadable")
                self.assertEqual(receipt["message"], peers.SECRET_UNREADABLE)
                self.assertNotIn("exit", receipt)
                self.assertIsNone(receipt["message_id"])
                self.assertNotIn(BEARER_KEY, json.dumps(receipt))
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.server.seen, [])
                self.assert_clean(receipt)

    def test_empty_body_is_refused_after_the_gates(self):
        receipt = self.send("   ")
        self.assertFalse(receipt["ok"], receipt)
        self.assertEqual(receipt["error"], "empty")
        self.assertEqual(receipt["message"], "Nothing was sent. The message is empty.")
        [row] = self.rows()
        self.assertEqual(row["delivery_path"], "refused")
        self.assertEqual(row["result"], "refused")
        self.assertEqual(row["detail"], "empty")
        self.assertEqual(row["address"], f"peer:{PEER}")
        self.assertEqual(self.server.seen, [])
        self.assert_clean(receipt)
        # A sender that is not Bot Chat never reaches the empty-row path.
        before = len(self.rows())
        receipt = self.send("  ", sender=OTHER)
        self.assertEqual(receipt["error"], "not-bot-chat")
        self.assertEqual(receipt["message"], ONLY_BOT_CHAT)
        self.assertIsNone(receipt["message_id"])
        self.assertEqual(len(self.rows()), before)
        self.assertEqual(self.server.seen, [])

    def _spike(self):
        spike = self.home / "profiles" / "spike"
        spike.mkdir(parents=True)
        (spike / "config.yaml").write_text(
            "plugins:\n  enabled: [intercom]\n", encoding="utf-8")
        return spike


class PostTests(OutboundTestBase):
    def test_hmac_post_body_and_signature(self):
        receipt = self.send("café from bot chat", reply_mode="requested")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["receipt"], "delivered")
        self.assertEqual(receipt["message"], "delivered (HTTP 200)")
        self.assertEqual(receipt["target_title"], PEER)
        self.assertEqual(receipt["url"], self.hook_url)
        self.assertEqual(receipt["reply"], "requested")
        self.assertEqual(receipt["requested"], "steer")
        [item] = self.server.seen
        self.assertEqual(item["method"], "POST")
        self.assertEqual(item["path"], "/hook")
        body = item["body"]
        payload = json.loads(body)
        self.assertEqual(list(payload), ["from", "message_id", "wants_reply", "text"])
        self.assertEqual(payload["from"], "hermes")
        self.assertEqual(payload["message_id"], receipt["message_id"])
        self.assertIs(payload["wants_reply"], True)
        self.assertEqual(payload["text"], "café from bot chat")
        self.assertEqual(body, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        stamp = item["headers"]["x-webhook-timestamp"]
        signature = item["headers"]["x-webhook-signature-v2"]
        expected = hmac.new(HMAC_SECRET.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(expected, signature))
        self.assertEqual(item["headers"]["content-type"], "application/json")
        during = item["row"]
        self.assertEqual(during["address"], f"peer:{PEER}")
        self.assertEqual(during["delivery_path"], "peer")
        self.assertIsNone(during["result"])
        row = record.get(receipt["message_id"])
        self.assertEqual(row["result"], "delivered")
        self.assertEqual(row["detail"], "HTTP 200")
        self.assertEqual(row["target_title"], PEER)
        self.assertEqual(row["sender_session_id"], BOT)
        self.assert_clean(receipt)

    def test_a_gist_is_stored_and_not_posted(self):
        receipt = self.send("café from bot chat", gist="A note for the remote agent.")
        self.assertTrue(receipt["ok"], receipt)
        payload = json.loads(self.server.seen[-1]["body"])
        self.assertEqual(list(payload), ["from", "message_id", "wants_reply", "text"])
        self.assertNotIn("gist", payload)
        self.assertEqual(payload["text"], "café from bot chat")
        self.assertEqual(record.get(receipt["message_id"])["gist"], "A note for the remote agent.")
        self.assert_clean(receipt)

    def test_none_posts_wants_reply_false_and_omits_reply_to(self):
        receipt = self.send("a note", reply_mode="none")
        self.assertTrue(receipt["ok"], receipt)
        payload = json.loads(self.server.seen[-1]["body"])
        self.assertIs(payload["wants_reply"], False)
        self.assertNotIn("reply_to", payload)
        self.assert_clean(receipt)

    def test_url_auth_posts_the_secret_url_and_the_receipt_shows_the_redacted_one(self):
        shown = f"{self.hook_url}/\u2026"
        self.write_peers([self.peer(PEER, auth="url", url=shown)])
        receipt = self.send("via the url")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["message"], "delivered (HTTP 200)")
        self.assertEqual(receipt["url"], shown)
        [item] = self.server.seen
        self.assertEqual(item["path"], f"/hook/{URL_SECRET}")
        self.assertNotIn("x-webhook-signature-v2", item["headers"])
        self.assertNotIn("x-webhook-timestamp", item["headers"])
        payload = json.loads(item["body"])
        self.assertEqual(payload["text"], "via the url")
        self.assertIs(payload["wants_reply"], False)
        self.assertNotIn(URL_SECRET, json.dumps(receipt))
        self.assert_clean(receipt)

    def test_optional_reply_posts_wants_reply_false_and_reply_to(self):
        receipt = self.send("only the block", reply_mode="optional", reply_to="abcd1234")
        self.assertTrue(receipt["ok"], receipt)
        payload = json.loads(self.server.seen[-1]["body"])
        self.assertEqual(list(payload), ["from", "message_id", "reply_to", "wants_reply", "text"])
        self.assertEqual(payload["reply_to"], "abcd1234")
        self.assertIs(payload["wants_reply"], False)
        self.assertEqual(payload["text"], "only the block")
        row = record.get(receipt["message_id"])
        self.assertEqual(row["in_reply_to"], "abcd1234")
        self.assertEqual(row["reply_mode"], "optional")
        self.assert_clean(receipt)

    def test_interrupt_and_queue_still_post(self):
        for delivery in ("interrupt", "queue"):
            with self.subTest(delivery=delivery):
                self.server.seen.clear()
                receipt = self.send(f"by {delivery}", delivery=delivery)
                self.assertTrue(receipt["ok"], receipt)
                self.assertEqual(receipt["requested"], delivery)
                self.assertEqual(record.get(receipt["message_id"])["requested"], delivery)
                self.assertEqual(json.loads(self.server.seen[-1]["body"])["text"], f"by {delivery}")
                self.assert_clean(receipt)

    def test_http_error_names_the_code_and_does_not_follow_a_redirect(self):
        self.server.status = 500
        receipt = self.send("nope")
        self.assertFalse(receipt["ok"], receipt)
        self.assertEqual(receipt["message"], "not delivered (HTTP 500)")
        self.assertEqual(receipt["error"], "HTTP 500")
        self.assertEqual(receipt["url"], self.hook_url)
        row = record.get(receipt["message_id"])
        self.assertEqual(row["result"], "error")
        self.assertEqual(row["detail"], "HTTP 500")
        self.assertEqual(len(self.server.seen), 1)
        self.assert_clean(receipt)

        self.write_peers([self.peer(PEER, auth="url")])
        self.server.seen.clear()
        self.log.lines.clear()
        receipt = self.send("secret path")
        self.assertEqual(receipt["message"], "not delivered (HTTP 500)")
        self.assertEqual(receipt["url"], f"{self.hook_url}/\u2026")
        self.assert_clean(receipt)
        self.assertNotIn(URL_SECRET, "\n".join(self.log.lines))

    def test_redirect_is_an_error(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.server.seen.clear()
                self.server.status = code
                receipt = self.send("go away")
                self.assertEqual(receipt["message"], f"not delivered (HTTP {code})")
                self.assertEqual(receipt["error"], f"HTTP {code}")
                self.assertEqual([item["method"] for item in self.server.seen], ["POST"])
                self.assertNotIn(URL_SECRET, json.dumps(receipt))
        self.server.status = 200

    def test_a_non_success_2xx_is_delivered(self):
        self.server.status = 204
        receipt = self.send("accepted")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["message"], "delivered (HTTP 204)")
        self.assertEqual(record.get(receipt["message_id"])["detail"], "HTTP 204")

    def test_timeout_is_delivery_unknown_and_does_not_retry(self):
        self.server.delay = 1.5
        with mock.patch.object(peers, "TIMEOUT_SECONDS", 0.4):
            receipt = self.send("slow")
        self.assertFalse(receipt["ok"], receipt)
        self.assertEqual(receipt["error"], "delivery-unknown")
        self.assertEqual(receipt["message"], "delivery-unknown")
        self.assertEqual(len(self.server.seen), 1)
        during = self.server.seen[0]["row"]
        self.assertEqual(during["delivery_path"], "peer")
        self.assertIsNone(during["result"])
        row = record.get(receipt["message_id"])
        self.assertEqual(row["result"], "error")
        self.assertEqual(row["detail"], "delivery-unknown")
        self.assert_clean(receipt)
        self.assertEqual(peers.TIMEOUT_SECONDS, 30)

    def test_tls_is_verified(self):
        self.write_peers([self.peer(PEER, auth="url")])
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(self.certs["other"])}):
            receipt = self.send("must fail the handshake")
        self.assertFalse(receipt["ok"], receipt)
        self.assertIsNotNone(receipt["message_id"])
        self.assertNotEqual(receipt["error"], "delivery-unknown")
        self.assertNotIn(URL_SECRET, receipt["error"])
        self.assertNotIn("http", receipt["error"].lower())
        self.assertEqual(self.server.seen, [])
        row = record.get(receipt["message_id"])
        self.assertEqual(row["result"], "error")
        self.assertEqual(row["detail"], receipt["error"])
        self.assertNotIn(URL_SECRET, row["detail"])
        self.assert_clean(receipt)

    def test_a_spaced_secret_url_is_an_error_and_stays_out_of_tool_output(self):
        token = "s3cret tok"
        self.url_file.write_text(f"{self.hook_url}/{token}\n", encoding="utf-8")
        shown = f"{self.hook_url}/\u2026"
        self.write_peers([self.peer(PEER, auth="url", url=shown)])
        name = f"hermes_intercom_out_{self._testMethodName}"
        plugin = _load_plugin(name)
        self.addCleanup(self._drop_module, name)
        ctx = FakeCtx(user_name="Sam")
        with mock.patch.object(plugin.inbox, "start", side_effect=AssertionError("inbox")):
            plugin.register(ctx)
        raw = plugin.handle(
            {"action": "send", "to": f"peer:{PEER}", "message": "hi", "reply": "none"},
            session_id=BOT)
        self.assertNotIn(token, raw)
        receipt = json.loads(raw)
        self.assertFalse(receipt["ok"], receipt)
        self.assertEqual(receipt["error"], "InvalidURL")
        self.assertEqual(receipt["message"], "not delivered (InvalidURL)")
        row = record.get(receipt["message_id"])
        self.assertEqual(row["result"], "error")
        self.assertEqual(row["detail"], "InvalidURL")
        self.assertNotIn(token, row["detail"])
        self.assertEqual(self.server.seen, [])
        self.assertNotIn(token, "\n".join(self.log.lines))
        self.assert_clean(receipt, raw)

    def test_a_malformed_status_line_records_the_error_type(self):
        self.server.raw_response = b"NOT A STATUS LINE\r\n"
        receipt = self.send("weird")
        self.assertFalse(receipt["ok"], receipt)
        self.assertEqual(receipt["error"], "BadStatusLine")
        self.assertEqual(receipt["message"], "not delivered (BadStatusLine)")
        self.assertNotIn("NOT A STATUS", json.dumps(receipt))
        row = record.get(receipt["message_id"])
        self.assertEqual(row["result"], "error")
        self.assertEqual(row["detail"], "BadStatusLine")
        self.assertEqual(len(self.server.seen), 1)
        self.assert_clean(receipt)

    def test_the_fake_server_rejects_a_bad_hmac(self):
        wrong = "not-the-server-secret"
        self.hmac_file.write_text(wrong + "\n", encoding="utf-8")
        receipt = self.send("bad sig")
        self.assertEqual(receipt["error"], "HTTP 401")
        self.assertEqual(receipt["message"], "not delivered (HTTP 401)")
        self.assertEqual([item["path"] for item in self.server.seen], ["/hook"])
        self.assertNotIn(wrong, json.dumps(receipt))
        self.assertNotIn(wrong, "\n".join(self.log.lines))
        row = record.get(receipt["message_id"])
        self.assertEqual(row["result"], "error")
        self.assertEqual(row["detail"], "HTTP 401")
        self.assert_clean(receipt)

    def test_a_bearer_peer_loads(self):
        self.write_peers([self.peer(PEER, auth="bearer")])
        loaded = peers.load()
        self.assertEqual(list(loaded), [PEER])
        peer = loaded[PEER]
        self.assertEqual(peer.auth, "bearer")
        self.assertEqual(peer.url, self.bearer_url)
        self.assertIn("bearer", peers.AUTH_MODES)
        shown = peers.for_bot_chat(BOT)
        self.assertEqual(shown, [{"to": f"peer:{PEER}", "url": self.bearer_url}])
        self.assertNotIn(BEARER_KEY, json.dumps(shown))
        self.write_peers([self.peer(PEER, auth="bearer", url=f"http://127.0.0.1:{self.port}/routine")])
        self.assertEqual(peers.load(), {})
        self.assert_refused(self.send(), "unknown-peer")

    def test_bearer_posts_the_authorization_header_and_no_hmac(self):
        self.bearer_file.write_text(f"\n  {BEARER_KEY}\t\n", encoding="utf-8")
        self.write_peers([self.peer(PEER, auth="bearer")])
        receipt = self.send("via bearer", reply_mode="requested")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["receipt"], "delivered")
        self.assertEqual(receipt["message"], "delivered (HTTP 200)")
        self.assertEqual(receipt["url"], self.bearer_url)
        [item] = self.server.seen
        self.assertEqual(item["method"], "POST")
        self.assertEqual(item["path"], "/routine")
        self.assertEqual(item["headers"].get("authorization"), f"Bearer {BEARER_KEY}")
        self.assertNotIn("x-webhook-signature-v2", item["headers"])
        self.assertNotIn("x-webhook-timestamp", item["headers"])
        payload = json.loads(item["body"])
        self.assertEqual(payload["from"], "hermes")
        self.assertEqual(payload["text"], "via bearer")
        self.assertIs(payload["wants_reply"], True)
        self.assertNotIn(BEARER_KEY, json.dumps(receipt))
        row = record.get(receipt["message_id"])
        self.assertEqual(row["result"], "delivered")
        self.assertEqual(row["detail"], "HTTP 200")
        self.assertEqual(row["address"], f"peer:{PEER}")
        self.assertNotIn(BEARER_KEY, json.dumps(row))
        self.assert_clean(receipt)

        self.server.seen.clear()
        self.server.status = 204
        accepted = self.send("accepted")
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["message"], "delivered (HTTP 204)")
        self.assertEqual(record.get(accepted["message_id"])["detail"], "HTTP 204")
        self.assertEqual(self.server.seen[-1]["headers"].get("authorization"), f"Bearer {BEARER_KEY}")
        self.assert_clean(accepted)
        self.server.status = 200

    def test_the_bearer_key_is_absent_from_an_http_error_and_a_connection_error(self):
        seen = []
        real_open = urllib.request.OpenerDirector.open

        def open_and_keep(opener, fullurl, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
            header = None
            if isinstance(fullurl, urllib.request.Request):
                header = fullurl.get_header("Authorization")
            try:
                return real_open(opener, fullurl, data, timeout)
            except Exception as exc:
                seen.append((header, exc))
                raise

        self.bearer_file.write_text(BEARER_KEY + "\n", encoding="utf-8")
        self.write_peers([self.peer(PEER, auth="bearer")])
        self.server.status = 500
        with mock.patch.object(urllib.request.OpenerDirector, "open", open_and_keep):
            http_receipt = self.send("http fails")
        self.assertEqual(http_receipt["message"], "not delivered (HTTP 500)")
        self.assertEqual(http_receipt["error"], "HTTP 500")
        self.assertEqual(len(self.server.seen), 1)
        self.assertEqual(self.server.seen[0]["headers"].get("authorization"), f"Bearer {BEARER_KEY}")
        self.assertNotIn("x-webhook-signature-v2", self.server.seen[0]["headers"])
        self._assert_bearer_hidden(http_receipt, seen)
        self.server.status = 200

        seen.clear()
        self.log.lines.clear()
        self.server.seen.clear()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed = sock.getsockname()[1]
        self.write_peers([self.peer(
            PEER, auth="bearer", url=f"https://127.0.0.1:{closed}/routine")])
        with mock.patch.object(urllib.request.OpenerDirector, "open", open_and_keep):
            conn_receipt = self.send("connect fails")
        self.assertFalse(conn_receipt["ok"], conn_receipt)
        self.assertIsNotNone(conn_receipt["message_id"])
        self.assertNotEqual(conn_receipt["error"], "delivery-unknown")
        self.assertNotIn("HTTP", conn_receipt["error"])
        self.assertEqual(self.server.seen, [])
        self._assert_bearer_hidden(conn_receipt, seen)

    def _assert_bearer_hidden(self, receipt, seen):
        self.assertTrue(seen)
        text = json.dumps(receipt)
        text += "\n" + "\n".join(self.log.lines)
        for row in self.rows():
            text += "\n" + json.dumps(row, default=str)
        for header, exc in seen:
            self.assertEqual(header, "Bearer " + BEARER_KEY)
            text += "\n" + str(exc)
            text += "\n" + repr(exc)
            text += "\n" + "".join(traceback.format_exception(exc))
        self.assertNotIn(BEARER_KEY, text)
        self.assert_clean(receipt)

    def test_the_post_runs_on_the_calling_thread(self):
        box = {}
        real = peers._exchange

        def wrapped(url, data, headers):
            box["thread"] = threading.current_thread()
            return real(url, data, headers)

        def run():
            box["worker"] = threading.current_thread()
            box["receipt"] = self.send("from a worker")

        with mock.patch.object(peers, "_exchange", wrapped):
            worker = threading.Thread(target=run, name="peer-caller")
            worker.start()
            worker.join(10)
        self.assertTrue(box["receipt"]["ok"], box["receipt"])
        self.assertIs(box["thread"], box["worker"])
        self.assertIsNot(box["thread"], threading.main_thread())
        self.assert_clean(box["receipt"])


class ReplyTests(OutboundTestBase):
    def setUp(self):
        super().setUp()
        self.owner = FakeOwner()
        deliver = mock.patch.object(route.client, "deliver_sync", self.owner)
        listing = mock.patch.object(
            route.client, "list_sessions", lambda scope="live", picker=False: {"ok": True, "sessions": []})
        deliver.start()
        listing.start()
        self.addCleanup(deliver.stop)
        self.addCleanup(listing.stop)

    def post_inbound(self, text="hello from afar", *, wants_reply="true"):
        content = f"reply_to={{reply_to}}\nwants_reply={wants_reply}\n{text}"
        ok, reason = peer_inbound._deliver_inbound(PEER, content)
        self.assertEqual((ok, reason), (True, ""))
        return record_rows_latest(self)

    def turn(self, answer, *, prompt="", steers=(), session_id=BOT):
        history = [{"role": "user", "content": prompt}]
        for steer in steers:
            history.append({"role": "assistant", "content": "", "tool_calls": []})
            history.append({"role": "user", "content": f"[steer]\n{steer}\n[/steer]",
                            "display_kind": "steer"})
        history.append({"role": "assistant", "content": answer})
        replies.on_post_llm_call(session_id=session_id, user_message=prompt,
                                 assistant_response=answer, conversation_history=history,
                                 model="m", platform="tui")
        for thread in threading.enumerate():
            if thread.name.startswith("intercom-reply-"):
                thread.join(10)

    def test_a_block_is_posted_to_the_peer_and_nothing_else(self):
        inbound = self.post_inbound()
        framed = self.owner.calls[-1]["text"]
        seen_threads = []
        real = peers._exchange

        def wrapped(url, data, headers):
            seen_threads.append(threading.current_thread().name)
            return real(url, data, headers)

        answer = f"Report for Sam.\n[REPLY {inbound['id']}]\nYes, 22:00.\n[/REPLY]\nthanks"
        with mock.patch.object(peers, "_exchange", wrapped):
            self.turn(answer, steers=[framed])
        self.assertEqual(seen_threads, [f"intercom-reply-{inbound['id']}"])
        [item] = self.server.seen
        payload = json.loads(item["body"])
        self.assertEqual(payload["text"], "Yes, 22:00.")
        self.assertNotIn("Report for Sam", payload["text"])
        self.assertNotIn("thanks", payload["text"])
        self.assertEqual(payload["reply_to"], inbound["id"])
        self.assertIs(payload["wants_reply"], False)
        self.assertEqual(list(payload), ["from", "message_id", "reply_to", "wants_reply", "text"])
        during = item["row"]
        self.assertEqual(during["address"], f"peer:{PEER}")
        self.assertEqual(during["delivery_path"], "peer")
        self.assertIsNone(during["result"])
        row = record.get(inbound["id"])
        self.assertEqual(row["reply_state"], "sent")
        self.assertEqual(row["reply"], "Yes, 22:00.")
        self.assertEqual(row["reply_message_id"], payload["message_id"])
        posted = record.get(payload["message_id"])
        self.assertEqual(posted["result"], "delivered")
        self.assertEqual(posted["reply_mode"], "optional")
        self.assertEqual(posted["in_reply_to"], inbound["id"])
        self.assert_clean({"inbound": row, "posted": posted})

    def test_a_turn_without_a_block_sends_nothing(self):
        inbound = self.post_inbound()
        framed = self.owner.calls[-1]["text"]
        self.turn("The whole answer is for Sam, with no block.", steers=[framed])
        self.assertEqual(self.server.seen, [])
        row = record.get(inbound["id"])
        self.assertEqual(row["reply_state"], "no-block")
        self.assertIsNone(row["reply"])
        self.assertIsNone(row["reply_message_id"])
        self.assertEqual(len(self.rows()), 1)

    def test_a_silent_block_sends_nothing(self):
        inbound = self.post_inbound()
        framed = self.owner.calls[-1]["text"]
        self.turn(f"For Sam.\n[REPLY {inbound['id']}][SILENT][/REPLY]", steers=[framed])
        self.assertEqual(self.server.seen, [])
        self.assertEqual(record.get(inbound["id"])["reply_state"], "silent")
        self.assertEqual(len(self.rows()), 1)

    def test_requested_peer_does_not_fall_through_to_the_whole_answer(self):
        inbound = self.post_inbound()
        self.assertEqual(inbound["reply_mode"], "requested")
        self.assertEqual(inbound["sender_source"], "peer")
        framed = self.owner.calls[-1]["text"]
        state, text, detail, gist = replies.reply_for(inbound, "Yes, 22:00 still holds.")
        self.assertEqual((state, text, detail, gist), ("no-block", None, None, None))
        self.turn("Yes, 22:00 still holds.", prompt=framed)
        self.assertEqual(self.server.seen, [])
        self.assertEqual(record.get(inbound["id"])["reply_state"], "no-block")


class SchemaListTests(OutboundTestBase):
    def load_plugin(self):
        name = f"hermes_intercom_out_{self._testMethodName}"
        plugin = _load_plugin(name)
        self.addCleanup(self._drop_module, name)
        return plugin

    def test_the_peer_clause_is_fixed_and_names_no_peer(self):
        plugin = self.load_plugin()
        ctx = FakeCtx(user_name="Sam")
        with mock.patch.object(plugin.inbox, "start", side_effect=AssertionError("inbox")):
            plugin.register(ctx)
        schema = ctx.tools[0]["schema"]
        description = schema["description"]
        self.assertIn(plugin._PEER_CLAUSE, description)
        self.assertEqual(description.count(plugin._PEER_CLAUSE), 1)
        self.assertNotIn("@", json.dumps(schema))
        self.assertNotIn(PEER, description)
        self.assertNotIn("@", plugin._PEER_CLAUSE)
        self.assertEqual(plugin._TO_OFF, "a session id, an exact title, or Home")
        self.assertEqual(
            plugin._TO_ON,
            "a session id, an exact title, Home, or @<project> (the project's owner session)")
        self.assertEqual(schema["parameters"]["properties"]["to"]["description"], plugin._TO_OFF)
        on = plugin._tool_description(True)
        off = plugin._tool_description(False)
        self.assertIn(plugin._PEER_CLAUSE, on)
        self.assertIn(plugin._PEER_CLAUSE, off)
        self.assertEqual(on.replace(plugin._TO_PARAGRAPH_ON, plugin._TO_PARAGRAPH_OFF), off)

    def test_list_shows_peers_to_bot_chat_only(self):
        plugin = self.load_plugin()
        ctx = FakeCtx(user_name="Sam")
        with mock.patch.object(plugin.inbox, "start", side_effect=AssertionError("inbox")):
            plugin.register(ctx)
        other = self.peer("other-agent", url=f"{self.hook_url}/other")
        self.write_peers([other, self.peer(PEER)])

        def list_sessions(scope="live", picker=False):
            return {"ok": True, "sessions": []}

        with mock.patch.object(plugin.client, "list_sessions", list_sessions):
            shown = json.loads(plugin.handle({"action": "list"}, session_id=BOT))
            queried = json.loads(plugin.handle(
                {"action": "list", "query": "nomatch", "limit": 1}, session_id=BOT))
            other_sender = json.loads(plugin.handle({"action": "list"}, session_id=OTHER))
            self.write_peers([])
            empty = json.loads(plugin.handle({"action": "list"}, session_id=BOT))
        self.assertEqual(shown["peers"], [
            {"to": "peer:other-agent", "url": f"{self.hook_url}/other"},
            {"to": f"peer:{PEER}", "url": self.hook_url},
        ])
        self.assertEqual(queried["peers"], shown["peers"])
        self.assertNotIn("peers", other_sender)
        self.assertEqual(empty["peers"], [])
        blob = json.dumps(shown)
        self.assertNotIn("secret_file", blob)
        self.assertNotIn(HMAC_SECRET, blob)
        self.assertIsNone(peers.for_bot_chat(OTHER))
        self.assertIsNone(peers.for_bot_chat(None))

    def test_list_in_another_profile_omits_peers(self):
        plugin = self.load_plugin()
        ctx = FakeCtx(user_name="Sam")
        with mock.patch.object(plugin.inbox, "start", side_effect=AssertionError("inbox")):
            plugin.register(ctx)
        spike = self.home / "profiles" / "spike"
        spike.mkdir(parents=True)
        (spike / "config.yaml").write_text("plugins:\n  enabled: [intercom]\n", encoding="utf-8")
        with mock.patch.object(plugin.client, "list_sessions",
                               lambda scope="live", picker=False: {"ok": True, "sessions": []}):
            raw = json.loads(plugin.handle(
                {"action": "list", "profile": "spike"}, session_id=BOT))
        self.assertNotIn("peers", raw)
        with profiles.in_profile("spike"):
            self.assertIsNone(peers.for_bot_chat(BOT))

    def test_the_tool_send_reaches_the_peer(self):
        plugin = self.load_plugin()
        ctx = FakeCtx(user_name="Sam")
        with mock.patch.object(plugin.inbox, "start", side_effect=AssertionError("inbox")):
            plugin.register(ctx)
        raw = json.loads(plugin.handle(
            {"action": "send", "to": f"peer:{PEER}", "message": "from the tool", "reply": "none"},
            session_id=BOT))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(raw["message"], "delivered (HTTP 200)")
        self.assertEqual(json.loads(self.server.seen[-1]["body"])["text"], "from the tool")
        self.assert_clean(raw)
        before = len(self.server.seen)
        asked = json.loads(plugin.handle(
            {"action": "ask", "to": f"peer:{PEER}", "message": "wait", "timeout": 10},
            session_id=BOT))
        self.assertEqual(asked["error"], "ask")
        self.assertEqual(asked["message"], ONLY_BOT_CHAT)
        self.assertEqual(len(self.server.seen), before)
        self.assert_clean(asked)


def record_rows_latest(case):
    rows = case.rows()
    case.assertEqual(len(rows), 1)
    return rows[0]
