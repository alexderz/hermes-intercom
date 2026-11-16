"""Caddy JSON rendered for the agent-peer proxy."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


def load_render():
    path = Path(__file__).resolve().parents[1] / "deploy" / "peer-proxy" / "render.py"
    spec = importlib.util.spec_from_file_location("peer_proxy_render", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = load_render()


class RenderTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="peer-proxy-render-")
        self.root = Path(self._dir.name)
        self.active = self.root / "active"
        self.active.mkdir()
        self.ca = self.root / "ca.pem"
        self.ca.write_text("ca\n", encoding="utf-8")
        self.cert = self.root / "server.pem"
        self.key = self.root / "server.key"
        self.cert.write_text("cert\n", encoding="utf-8")
        self.key.write_text("key\n", encoding="utf-8")
        self.sock = str(self.root / "admin.sock")

    def tearDown(self):
        self._dir.cleanup()

    def write_pin(self, name, body="pem\n"):
        path = self.active / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def config(self, **overrides):
        args = dict(
            listen="127.0.0.1:8645",
            server_name="agent-peers.example",
            ca=str(self.ca),
            active=str(self.active),
            upstream="127.0.0.1:8644",
            admin_socket=self.sock,
            access_log=str(self.root / "access.log"),
            cert=str(self.cert),
            key=str(self.key),
        )
        args.update(overrides)
        return render.render_config(**args)

    def server(self, config=None):
        config = self.config() if config is None else config
        servers = config["apps"]["http"]["servers"]
        self.assertEqual(list(servers), ["agent-peers"])
        return servers["agent-peers"]

    def test_one_policy_has_no_match_and_uses_the_leaf_folder(self):
        self.write_pin("alpha.pem")
        server = self.server()
        policies = server["tls_connection_policies"]
        self.assertEqual(len(policies), 1)
        policy = policies[0]
        self.assertNotIn("match", policy)
        auth = policy["client_authentication"]
        self.assertEqual(auth["mode"], "require_and_verify")
        self.assertEqual(
            auth["ca"],
            {"pem_files": [str(self.ca)], "provider": "file"},
        )
        self.assertEqual(
            auth["verifiers"],
            [
                {
                    "leaf_certs_loaders": [
                        {"folders": [str(self.active)], "loader": "folder"}
                    ],
                    "verifier": "leaf",
                }
            ],
        )
        self.assertEqual(server["listen"], ["127.0.0.1:8645"])
        self.assertEqual(server["automatic_https"], {"disable": True})
        self.assertNotIn("h3", server["protocols"])

    def test_listen_is_the_given_address(self):
        server = self.server(self.config(listen="192.0.2.10:8645"))
        self.assertEqual(server["listen"], ["192.0.2.10:8645"])

    def test_routes_expression_and_skip_invalid_names(self):
        self.write_pin("beta.pem")
        self.write_pin("alpha.pem")
        self.write_pin("a-b.pem")
        self.write_pin("Alpha.pem")
        self.write_pin("has_under.pem")
        self.write_pin(("n" * 64) + ".pem")
        self.write_pin("notes.txt", "ignore\n")
        self.write_pin("nested/gamma.pem")
        err = io.StringIO()
        with redirect_stderr(err):
            config = self.config()
        server = self.server(config)
        routes = server["routes"]
        names = ["a-b", "alpha", "beta"]
        self.assertEqual(len(routes), 4)
        for route, name in zip(routes, names):
            path = f"/webhooks/peer-{name}"
            self.assertEqual(
                route["match"],
                [
                    {
                        "expression": (
                            '{http.request.tls.client.subject} == "CN=%s"' % name
                        ),
                        "path": [path],
                    }
                ],
            )
            self.assertEqual(
                route["handle"],
                [
                    {
                        "handler": "log_append",
                        "key": "client_subject",
                        "value": render.SUBJECT,
                    },
                    {
                        "handler": "log_append",
                        "key": "client_serial",
                        "value": render.CLIENT_SERIAL,
                    },
                    {"handler": "rewrite", "uri": path},
                    {"handler": "request_body", "max_size": 65536},
                    {
                        "handler": "reverse_proxy",
                        "upstreams": [{"dial": "127.0.0.1:8644"}],
                    },
                ],
            )
        self.assertEqual(
            routes[-1],
            {
                "handle": [
                    {
                        "handler": "log_append",
                        "key": "client_subject",
                        "value": render.SUBJECT,
                    },
                    {
                        "handler": "log_append",
                        "key": "client_serial",
                        "value": render.CLIENT_SERIAL,
                    },
                    {"handler": "static_response", "status_code": 403},
                ]
            },
        )
        self.assertNotIn("match", routes[-1])
        warning = err.getvalue()
        self.assertIn("skipping Alpha.pem", warning)
        self.assertIn("skipping has_under.pem", warning)
        self.assertIn("skipping " + ("n" * 64) + ".pem", warning)
        self.assertNotIn("gamma.pem", warning)
        self.assertNotIn("notes.txt", warning)
        text = render.dump_config(config)
        self.assertIn("{http.request.tls.client.subject}", text)
        self.assertNotIn("{tls_client_subject}", text)

    def test_empty_folder_is_fail_closed(self):
        err = io.StringIO()
        with redirect_stderr(err):
            server = self.server()
        self.assertEqual(err.getvalue(), "")
        policies = server["tls_connection_policies"]
        self.assertEqual(len(policies), 1)
        self.assertNotIn("match", policies[0])
        self.assertEqual(
            policies[0]["client_authentication"]["verifiers"][0]["leaf_certs_loaders"][0][
                "folders"
            ],
            [str(self.active)],
        )
        self.assertEqual(
            server["routes"],
            [
                {
                    "handle": [
                        {
                            "handler": "log_append",
                            "key": "client_subject",
                            "value": render.SUBJECT,
                        },
                        {
                            "handler": "log_append",
                            "key": "client_serial",
                            "value": render.CLIENT_SERIAL,
                        },
                        {"handler": "static_response", "status_code": 403},
                    ]
                }
            ],
        )

    def test_admin_socket_and_persist_off(self):
        config = self.config()
        self.assertEqual(config["admin"]["listen"], "unix/" + self.sock)
        self.assertEqual(config["admin"]["config"]["persist"], False)

    def test_output_is_deterministic_and_sorted(self):
        self.write_pin("zeta.pem")
        self.write_pin("alpha.pem")
        out = io.StringIO()
        err = io.StringIO()
        argv = [
            "--listen",
            "127.0.0.1:8645",
            "--server-name",
            "agent-peers.example",
            "--ca",
            str(self.ca),
            "--active",
            str(self.active),
            "--upstream",
            "127.0.0.1:8644",
            "--admin-socket",
            self.sock,
            "--access-log",
            str(self.root / "access.log"),
            "--cert",
            str(self.cert),
            "--key",
            str(self.key),
        ]
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(render.main(argv), 0)
        first = out.getvalue()
        out2 = io.StringIO()
        with redirect_stdout(out2), redirect_stderr(io.StringIO()):
            self.assertEqual(render.main(argv), 0)
        self.assertEqual(first, out2.getvalue())
        parsed = json.loads(first)
        self.assertEqual(first, json.dumps(parsed, indent=2, sort_keys=True) + "\n")
        routes = parsed["apps"]["http"]["servers"]["agent-peers"]["routes"]
        self.assertEqual(
            routes[0]["match"][0]["path"],
            ["/webhooks/peer-alpha"],
        )
        self.assertEqual(
            routes[1]["match"][0]["path"],
            ["/webhooks/peer-zeta"],
        )

    def test_acme_dns_json_shape(self):
        provider = {
            "name": "example",
            "api_token": "not-a-secret",
            "zones": ["example"],
            "options": {"region": "example"},
        }
        path = self.root / "dns.json"
        path.write_text(json.dumps(provider), encoding="utf-8")
        config = self.config(cert=None, key=None, acme_dns=str(path))
        tls = config["apps"]["tls"]
        self.assertEqual(tls["certificates"]["automate"], ["agent-peers.example"])
        self.assertNotIn("load_files", tls["certificates"])
        policy = tls["automation"]["policies"][0]
        self.assertEqual(policy["subjects"], ["agent-peers.example"])
        issuer = policy["issuers"][0]
        self.assertEqual(issuer["module"], "acme")
        self.assertEqual(issuer["challenges"]["http"], {"disabled": True})
        self.assertEqual(issuer["challenges"]["tls-alpn"], {"disabled": True})
        self.assertEqual(issuer["challenges"]["dns"]["provider"], provider)
        text = render.dump_config(config)
        self.assertNotIn("{tls_client_subject}", text)
        # The file-certificate mode is not mixed in.
        self.assertNotIn("load_files", text)

    def test_certificate_modes_are_exclusive(self):
        with self.assertRaises(SystemExit) as ctx:
            self.config(acme_dns=str(self.root / "missing.json"))
        self.assertIn("not both", str(ctx.exception))
        with self.assertRaises(SystemExit) as no_source:
            self.config(cert=None, key=None)
        self.assertIn("--cert/--key or --acme-dns", str(no_source.exception))
        with self.assertRaises(SystemExit):
            self.config(key=None)

    def test_access_log_is_required_and_names_the_tls_sibling(self):
        with self.assertRaises(SystemExit) as missing:
            self.config(access_log="")
        self.assertIn("--access-log", str(missing.exception))
        config = self.config(access_log="peer-access.log")
        logs = config["logging"]["logs"]
        access = logs["access"]
        self.assertEqual(
            access["writer"],
            {"filename": "peer-access.log", "output": "file", "roll": True},
        )
        self.assertEqual(access["encoder"], {"format": "json"})
        self.assertEqual(access["include"], ["http.log.access.agent-peers"])
        self.assertNotIn("level", access)
        tls = logs["tls"]
        self.assertEqual(
            tls["writer"],
            {"filename": "peer-access.log.tls", "output": "file", "roll": True},
        )
        self.assertEqual(tls["encoder"], {"format": "json"})
        self.assertEqual(tls["level"], "DEBUG")
        self.assertEqual(tls["include"], ["http.stdlib", "tls.handshake"])
        self.assertEqual(
            logs["default"]["exclude"],
            ["http.log.access.agent-peers"],
        )
        server = self.server(config)
        self.assertEqual(server["logs"], {"default_logger_name": "agent-peers"})
        text = render.dump_config(config)
        self.assertIn('"roll": true', text)
        self.assertIn("peer-access.log.tls", text)

    def main_argv(self, *extra):
        return [
            "--listen",
            "127.0.0.1:8645",
            "--server-name",
            "agent-peers.example",
            "--ca",
            str(self.ca),
            "--active",
            str(self.active),
            "--upstream",
            "127.0.0.1:8644",
            "--admin-socket",
            self.sock,
            "--access-log",
            str(self.root / "access.log"),
            "--cert",
            str(self.cert),
            "--key",
            str(self.key),
            *extra,
        ]

    def body_limits(self, text):
        config = json.loads(text)
        routes = config["apps"]["http"]["servers"]["agent-peers"]["routes"]
        limits = {}
        for route in routes:
            found = [
                handle["max_size"]
                for handle in route["handle"]
                if handle.get("handler") == "request_body"
            ]
            handlers = [handle["handler"] for handle in route["handle"]]
            if "match" not in route:
                self.assertEqual(found, [])
                self.assertNotIn("request_body", handlers)
                continue
            self.assertEqual(len(found), 1)
            self.assertLess(
                handlers.index("request_body"),
                handlers.index("reverse_proxy"),
            )
            limits[route["match"][0]["path"][0]] = found[0]
        return limits

    def test_max_body_and_per_peer_override(self):
        self.write_pin("beta.pem")
        self.write_pin("alpha.pem")
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(render.main(self.main_argv()), 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(
            self.body_limits(out.getvalue()),
            {
                "/webhooks/peer-alpha": 65536,
                "/webhooks/peer-beta": 65536,
            },
        )
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(
                render.main(
                    self.main_argv(
                        "--max-body",
                        "100",
                        "--max-body-peer",
                        "beta=250",
                        "--max-body-peer",
                        "alpha=40",
                    )
                ),
                0,
            )
        self.assertEqual(
            self.body_limits(out.getvalue()),
            {
                "/webhooks/peer-alpha": 40,
                "/webhooks/peer-beta": 250,
            },
        )
        # A later value for the same peer replaces the earlier one.
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(
                render.main(
                    self.main_argv(
                        "--max-body",
                        "100",
                        "--max-body-peer",
                        "beta=10",
                        "--max-body-peer",
                        "beta=80",
                    )
                ),
                0,
            )
        limits = self.body_limits(out.getvalue())
        self.assertEqual(limits["/webhooks/peer-alpha"], 100)
        self.assertEqual(limits["/webhooks/peer-beta"], 80)

    def assert_render_error(self, extra, message):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                render.main(self.main_argv(*extra))
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn(message, str(ctx.exception))
        self.assertEqual(out.getvalue(), "")

    def test_body_limit_rejects_bad_values(self):
        self.write_pin("alpha.pem")
        self.assert_render_error(["--max-body", "0"], "positive integer")
        self.assert_render_error(["--max-body", "-5"], "positive integer")
        self.assert_render_error(["--max-body", "12kb"], "positive integer")
        self.assert_render_error(["--max-body", ""], "positive integer")
        self.assert_render_error(
            ["--max-body", "9223372036854775808"],
            "positive integer",
        )
        self.assert_render_error(
            ["--max-body-peer", "alpha=0"],
            "positive integer",
        )
        self.assert_render_error(
            ["--max-body-peer", "alpha=-1"],
            "positive integer",
        )
        self.assert_render_error(
            ["--max-body-peer", "alpha=nope"],
            "positive integer",
        )
        self.assert_render_error(
            ["--max-body-peer", "alpha"],
            "<name>=<bytes>",
        )
        self.assert_render_error(
            ["--max-body-peer", "=10"],
            "<name>=<bytes>",
        )
        self.assert_render_error(
            ["--max-body-peer", "beta=10"],
            "unknown peer 'beta'",
        )
        # The largest signed 64-bit byte count is accepted.
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(
                render.main(self.main_argv("--max-body", "9223372036854775807")),
                0,
            )
        self.assertEqual(
            self.body_limits(out.getvalue())["/webhooks/peer-alpha"],
            9223372036854775807,
        )

    def test_acme_dns_must_be_an_object(self):
        path = self.root / "dns.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            self.config(cert=None, key=None, acme_dns=str(path))
        self.assertIn("JSON object", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
