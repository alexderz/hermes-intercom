#!/bin/bash
# Loopback trial of the agent-peer proxy TLS and routing rules.
# Uses the installed caddy, a throwaway CA, and the shipped proxy-reload.
# Set TRIAL_KEEP=1 to leave the temp dir in place.

set -euo pipefail

if [ -z "${TMPDIR:-}" ]; then
  echo "trial: TMPDIR must be set" >&2
  exit 1
fi

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d "$TMPDIR/peer-proxy-trial.XXXXXX")
passes=0
fails=0
caddy_pid=
upstream_pid=
keepalive_pid=
resume_pid=

cleanup() {
  ec=$?
  if [ -n "${keepalive_pid:-}" ] && kill -0 "$keepalive_pid" 2>/dev/null; then
    kill "$keepalive_pid" 2>/dev/null || true
    wait "$keepalive_pid" 2>/dev/null || true
  fi
  if [ -n "${resume_pid:-}" ] && kill -0 "$resume_pid" 2>/dev/null; then
    kill "$resume_pid" 2>/dev/null || true
    wait "$resume_pid" 2>/dev/null || true
  fi
  if [ -n "${caddy_pid:-}" ] && kill -0 "$caddy_pid" 2>/dev/null; then
    kill "$caddy_pid" 2>/dev/null || true
    wait "$caddy_pid" 2>/dev/null || true
  fi
  if [ -n "${upstream_pid:-}" ] && kill -0 "$upstream_pid" 2>/dev/null; then
    kill "$upstream_pid" 2>/dev/null || true
    wait "$upstream_pid" 2>/dev/null || true
  fi
  if [ "${TRIAL_KEEP:-}" = "1" ]; then
    echo "trial: kept $work" >&2
  else
    rm -rf "$work"
  fi
  exit "$ec"
}
trap cleanup EXIT

export HOME="$work/home"
export XDG_CONFIG_HOME="$work/xdg/config"
export XDG_DATA_HOME="$work/xdg/data"
export XDG_CACHE_HOME="$work/xdg/cache"
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" \
  "$work/active" "$work/keys" "$work/certs" "$work/ext" "$work/csr"
cd "$work"

name=agent-peers.test
port=
upstream_port=
ca=$work/ca.pem
config=$work/caddy.json
sock=$work/admin.sock
access_log=$work/access.log
tls_log=$work/access.log.tls

ok() {
  printf 'PASS %s\n' "$1"
  passes=$((passes + 1))
}

bad() {
  printf 'FAIL %s\n' "$1"
  fails=$((fails + 1))
}

free_port() {
  python3 -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

write_ext() {
  local path=$1
  shift
  printf '%s\n' "$@" >"$path"
}

make_ca() {
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$work/ca.key"
  openssl req -new -x509 -key "$work/ca.key" -out "$ca" -days 30 \
    -subj "/CN=Trial CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash"
}

sign_leaf() {
  local cn=$1 san=$2 eku=$3 out_cert=$4 out_key=$5
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$out_key"
  openssl req -new -key "$out_key" -out "$work/csr/req.csr" -subj "/CN=${cn}"
  write_ext "$work/ext/leaf.ext" \
    "basicConstraints=critical,CA:FALSE" \
    "keyUsage=critical,digitalSignature" \
    "extendedKeyUsage=${eku}" \
    "subjectKeyIdentifier=hash" \
    "authorityKeyIdentifier=keyid" \
    "subjectAltName=${san}"
  openssl x509 -req -in "$work/csr/req.csr" \
    -CA "$ca" -CAkey "$work/ca.key" -CAcreateserial \
    -out "$out_cert" -days 30 -extfile "$work/ext/leaf.ext"
}

make_client() {
  local peer=$1
  sign_leaf "$peer" "DNS:${peer}" clientAuth \
    "$work/certs/${peer}.pem" "$work/keys/${peer}.key"
}

render_initial() {
  python3 -I "$here/render.py" \
    --listen "127.0.0.1:${port}" \
    --server-name "$name" \
    --ca "$ca" \
    --active "$work/active" \
    --upstream "127.0.0.1:${upstream_port}" \
    --admin-socket "$sock" \
    --access-log "$access_log" \
    --cert "$work/certs/server.pem" \
    --key "$work/keys/server.key" \
    >"$config"
}

run_hook() {
  if ! "$here/proxy-reload" \
    --listen "127.0.0.1:${port}" \
    --server-name "$name" \
    --ca "$ca" \
    --active "$work/active" \
    --upstream "127.0.0.1:${upstream_port}" \
    --admin-socket "$sock" \
    --access-log "$access_log" \
    --cert "$work/certs/server.pem" \
    --key "$work/keys/server.key" \
    --config-out "$config" \
    "$@" \
    >>"$work/hook.log" 2>&1
  then
    echo "trial: proxy-reload failed; see $work/hook.log" >&2
    tail -n 40 "$work/hook.log" >&2 || true
    return 1
  fi
}

wait_port() {
  local want=$1
  python3 -I - "$want" <<'PY'
import socket, sys, time
port = int(sys.argv[1])
for _ in range(50):
    try:
        sock = socket.create_connection(("127.0.0.1", port), 0.2)
    except OSError:
        time.sleep(0.1)
        continue
    sock.close()
    sys.exit(0)
sys.exit(1)
PY
}

# HTTP status on stdout. Curl's exit status is the function status.
http_get() {
  local cert=$1 key=$2 path=$3 body=$4
  local args=(
    --http1.1 --silent --max-time 5 --path-as-is
    --resolve "${name}:${port}:127.0.0.1"
    --cacert "$ca"
    -o "$body" -w "%{http_code}"
    "https://${name}:${port}${path}"
  )
  if [ -n "$cert" ]; then
    args+=(--cert "$cert" --key "$key")
  fi
  curl "${args[@]}"
}

# POST the file as the body. Curl's exit status is the function status.
http_post() {
  local cert=$1 key=$2 path=$3 data=$4 body=$5
  curl \
    --http1.1 --silent --max-time 5 --path-as-is \
    --resolve "${name}:${port}:127.0.0.1" \
    --cacert "$ca" \
    --cert "$cert" --key "$key" \
    -H "Content-Type: application/octet-stream" \
    -H "Expect:" \
    --data-binary "@${data}" \
    -o "$body" -w "%{http_code}" \
    "https://${name}:${port}${path}"
}

make_body() {
  python3 -I - "$1" "$2" <<'PY'
import pathlib, sys
pathlib.Path(sys.argv[1]).write_bytes(b"x" * int(sys.argv[2]))
PY
}

expect_post() {
  local label=$1 cert=$2 key=$3 path=$4 data=$5 want=$6 want_body=${7:-}
  local body http rc
  body=$(mktemp "$work/body.XXXXXX")
  set +e
  http=$(http_post "$cert" "$key" "$path" "$data" "$body")
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    bad "$label (curl exit $rc, http ${http:-none})"
    rm -f "$body"
    return
  fi
  if [ "$http" != "$want" ]; then
    bad "$label (http $http, want $want, body $(cat "$body" 2>/dev/null || true))"
    rm -f "$body"
    return
  fi
  if [ -n "$want_body" ] && [ "$(cat "$body")" != "$want_body" ]; then
    bad "$label (upstream body $(cat "$body"), want $want_body)"
    rm -f "$body"
    return
  fi
  rm -f "$body"
  ok "$label"
}

# A completed upstream post is one line "<path> <length>". Poll so a body
# that shows up just after the client is answered is still noticed.
upstream_lacks() {
  local label=$1 needle=$2
  if python3 -I - "$work/upstream.seen" "$needle" <<'PY'
import pathlib, sys, time
path, needle = sys.argv[1], sys.argv[2]
deadline = time.time() + 1
while time.time() < deadline:
    file = pathlib.Path(path)
    text = file.read_text(encoding="ascii") if file.exists() else ""
    if needle in text.splitlines():
        sys.exit(1)
    time.sleep(0.05)
sys.exit(0)
PY
  then
    ok "$label"
  else
    bad "$label (upstream accepted ${needle})"
  fi
}

expect_status() {
  local label=$1 cert=$2 key=$3 path=$4 want=$5 want_body=${6:-}
  local body http rc
  body=$(mktemp "$work/body.XXXXXX")
  set +e
  http=$(http_get "$cert" "$key" "$path" "$body")
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    bad "$label (curl exit $rc, http ${http:-none})"
    rm -f "$body"
    return
  fi
  if [ "$http" != "$want" ]; then
    bad "$label (http $http, want $want)"
    rm -f "$body"
    return
  fi
  if [ -n "$want_body" ] && [ "$(cat "$body")" != "$want_body" ]; then
    bad "$label (upstream path $(cat "$body"), want $want_body)"
    rm -f "$body"
    return
  fi
  rm -f "$body"
  ok "$label"
}

# TLS 1.3 client-auth alerts arrive on the first application read.
# openssl s_client can print DONE and "Verify return code: 0 (ok)" before
# that alert, so the probe reads with Python and requires the alert.
probe_refused() {
  local cert=$1 key=$2 sni_mode=$3
  local out rc
  out=$work/probe.txt
  set +e
  python3 -I - "$port" "$name" "$sni_mode" "$cert" "$key" >"$out" 2>&1 <<'PY'
import socket
import ssl
import sys

port = int(sys.argv[1])
name = sys.argv[2]
mode = sys.argv[3]
cert = sys.argv[4]
key = sys.argv[5]
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
if cert:
    ctx.load_cert_chain(cert, key)
raw = socket.create_connection(("127.0.0.1", port), 5)
if mode == "none":
    hostname = None
elif mode == "wrong":
    hostname = "wrong.test"
elif mode == "own":
    hostname = name
else:
    sys.exit(f"bad sni mode {mode}")
try:
    tls = ctx.wrap_socket(raw, server_hostname=hostname)
except ssl.SSLError as exc:
    print(f"HANDSHAKE-FAIL {exc}")
    sys.exit(0)
try:
    host = hostname or name
    tls.sendall(
        (
            "GET /webhooks/peer-alpha HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
    )
    data = tls.recv(512)
except ssl.SSLError as exc:
    print(f"READ-FAIL {exc}")
    sys.exit(0)
if not data:
    print("READ-EMPTY")
    sys.exit(0)
status = data.split(b"\r\n", 1)[0].decode("ascii", "replace")
print(f"HTTP {status}")
PY
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    return 1
  fi
  grep -Ei '^(HANDSHAKE-FAIL|READ-FAIL) .*(alert|certificate|internal)' "$out" >/dev/null
}

expect_refused() {
  local label=$1
  if probe_refused "$2" "$3" "$4"; then
    ok "$label"
    return
  fi
  bad "$label ($(tr '\n' ' ' <"$work/probe.txt"))"
}

# Poll a JSON log until python says the lines match. Caddy writes the
# line as the request or handshake finishes; give it a moment.
wait_log() {
  local label=$1 file=$2
  shift 2
  if python3 -I - "$file" "$@" <<'PY'
import json
import sys
import time

def access_403(row):
    request = row.get("request") or {}
    uri = request.get("uri") or ""
    remote = request.get("remote_ip") or ""
    subject = row.get("client_subject") or ""
    serial = row.get("client_serial") or ""
    return (
        row.get("status") == 403
        and uri == "/webhooks/peer-beta"
        and remote == "127.0.0.1"
        and subject == "CN=alpha"
        and serial != ""
    )

def access_413(row):
    request = row.get("request") or {}
    uri = request.get("uri") or ""
    remote = request.get("remote_ip") or ""
    subject = row.get("client_subject") or ""
    serial = row.get("client_serial") or ""
    if row.get("status") != 413 or remote != "127.0.0.1" or serial == "":
        return False
    if want_uri and uri != want_uri:
        return False
    if want_subject and subject != want_subject:
        return False
    return True

def handshake(row):
    logger = row.get("logger") or ""
    msg = (row.get("msg") or "").lower()
    # A refused handshake is not an access line. Caddy records it as
    # "TLS handshake error" on http.stdlib, with detail on tls.handshake.
    if logger == "http.stdlib" and "tls handshake error" in msg:
        return True
    if logger == "tls.handshake" and any(
        word in msg for word in ("fail", "error", "reject", "not trusted")
    ):
        return True
    return False

path = sys.argv[1]
kind = sys.argv[2]
offset = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != "" else 0
want_uri = sys.argv[4] if len(sys.argv) > 4 else ""
want_subject = sys.argv[5] if len(sys.argv) > 5 else ""
deadline = time.time() + 5
while time.time() < deadline:
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if kind == "403" and access_403(row):
            sys.exit(0)
        if kind == "413" and access_413(row):
            sys.exit(0)
        if kind == "handshake" and handshake(row):
            sys.exit(0)
    time.sleep(0.1)
sys.exit(1)
PY
  then
    ok "$label"
    return
  fi
  bad "$label ($(tr '\n' ' ' <"$file" 2>/dev/null | tail -c 600))"
}

tcp_open() {
  python3 -I - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
try:
    sock = socket.create_connection(("127.0.0.1", port), 2)
except OSError:
    sys.exit(1)
sock.close()
PY
}

echo "trial: work dir $work" >&2

make_ca
sign_leaf "$name" "DNS:${name}" serverAuth \
  "$work/certs/server.pem" "$work/keys/server.key"
make_client alpha
make_client beta
make_client gamma
cp "$work/certs/alpha.pem" "$work/active/alpha.pem"
cp "$work/certs/beta.pem" "$work/active/beta.pem"
# gamma is signed by the same CA and is not pinned.

port=$(free_port)
upstream_port=$(free_port)

: >"$work/upstream.seen"
python3 -I - "$upstream_port" "$work/upstream.seen" <<'PY' &
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        data = self.path.encode("ascii", "replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        data = b""
        while len(data) < length:
            chunk = self.rfile.read(length - len(data))
            if not chunk:
                break
            data += chunk
        # A short read means the proxy closed the body early. Do not
        # answer 200: that post did not arrive whole.
        if len(data) != length:
            self.close_connection = True
            return
        with open(seen, "a", encoding="ascii") as handle:
            handle.write(f"{self.path} {len(data)}\n")
        payload = str(len(data)).encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        return

port = int(sys.argv[1])
seen = sys.argv[2]
server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
server.serve_forever()
PY
upstream_pid=$!
if ! wait_port "$upstream_port"; then
  echo "trial: upstream did not listen on 127.0.0.1:${upstream_port}" >&2
  bad "upstream started"
  echo "trial: ${passes} passed, ${fails} failed" >&2
  exit 1
fi

render_initial
caddy run --config "$config" >"$work/caddy.log" 2>&1 &
caddy_pid=$!

if ! wait_port "$port"; then
  echo "trial: caddy did not listen on 127.0.0.1:${port}" >&2
  tail -n 60 "$work/caddy.log" >&2 || true
  bad "caddy started"
  echo "trial: ${passes} passed, ${fails} failed" >&2
  exit 1
fi
if ! kill -0 "$caddy_pid" 2>/dev/null; then
  echo "trial: caddy exited" >&2
  tail -n 60 "$work/caddy.log" >&2 || true
  bad "caddy stayed up"
  echo "trial: ${passes} passed, ${fails} failed" >&2
  exit 1
fi

alpha_cert=$work/certs/alpha.pem
alpha_key=$work/keys/alpha.key
beta_cert=$work/certs/beta.pem
beta_key=$work/keys/beta.key
gamma_cert=$work/certs/gamma.pem
gamma_key=$work/keys/gamma.key

expect_status "own path 200" "$alpha_cert" "$alpha_key" \
  "/webhooks/peer-alpha" 200 "/webhooks/peer-alpha"
expect_status "other peer path 200 for that peer" "$beta_cert" "$beta_key" \
  "/webhooks/peer-beta" 200 "/webhooks/peer-beta"
expect_status "other peer's path 403" "$alpha_cert" "$alpha_key" \
  "/webhooks/peer-beta" 403
wait_log "other peer's path 403 logged" "$access_log" 403
expect_status "/p/x/... 403" "$alpha_cert" "$alpha_key" "/p/x/y" 403

expect_status "path trick case reaches own route" \
  "$alpha_cert" "$alpha_key" "/WEBHOOKS/PEER-ALPHA" 200 "/webhooks/peer-alpha"
expect_status "path trick .. reaches own route" \
  "$alpha_cert" "$alpha_key" "/webhooks/peer-beta/../peer-alpha" 200 \
  "/webhooks/peer-alpha"
expect_status "path trick %2D reaches own route" \
  "$alpha_cert" "$alpha_key" "/webhooks/peer%2Dalpha" 200 "/webhooks/peer-alpha"
expect_status "path trick case does not reach the other peer" \
  "$alpha_cert" "$alpha_key" "/WEBHOOKS/PEER-BETA" 403
expect_status "path trick .. does not reach the other peer" \
  "$alpha_cert" "$alpha_key" "/webhooks/peer-alpha/../peer-beta" 403

expect_refused "no client cert refused" "" "" own
tls_mark=0
if [ -f "$tls_log" ]; then
  tls_mark=$(wc -c <"$tls_log" | tr -d ' ')
fi
expect_refused "unpinned cert from the same CA refused" \
  "$gamma_cert" "$gamma_key" own
wait_log "unpinned cert handshake logged" "$tls_log" handshake "$tls_mark"
expect_refused "wrong SNI refused at the handshake" \
  "$alpha_cert" "$alpha_key" wrong
expect_refused "no SNI refused at the handshake" \
  "$alpha_cert" "$alpha_key" none

# Replacing the pin under the same name leaves the JSON unchanged.
# Only caddy reload --force picks up the new leaf.
cp "$config" "$work/before-replace.json"
sign_leaf alpha "DNS:alpha" clientAuth \
  "$work/certs/alpha-new.pem" "$work/keys/alpha-new.key"
cp "$work/certs/alpha-new.pem" "$work/active/alpha.pem"
if run_hook; then
  if ! cmp -s "$work/before-replace.json" "$config"; then
    bad "replaced pin accepts only the new cert (JSON changed, --force not proved)"
  else
    body=$(mktemp "$work/body.XXXXXX")
    set +e
    old_code=$(http_get "$alpha_cert" "$alpha_key" "/webhooks/peer-alpha" "$body")
    old_rc=$?
    new_code=$(http_get "$work/certs/alpha-new.pem" "$work/keys/alpha-new.key" \
      "/webhooks/peer-alpha" "$body")
    new_rc=$?
    set -e
    new_path=$(cat "$body" 2>/dev/null || true)
    rm -f "$body"
    if [ "$old_rc" -eq 0 ]; then
      bad "replaced pin accepts only the new cert (old cert still accepted, http ${old_code:-none})"
    elif [ "$new_rc" -ne 0 ] || [ "$new_code" != "200" ] || [ "$new_path" != "/webhooks/peer-alpha" ]; then
      bad "replaced pin accepts only the new cert (new rc $new_rc http ${new_code:-none} path ${new_path:-none})"
    else
      ok "replaced pin accepts only the new cert"
    fi
  fi
else
  bad "replaced pin accepts only the new cert (hook failed)"
fi

# Keep-alive opened before the hook, then the pin is removed.
# A second process saves a TLS session and, after the hook, opens a new
# connection with that session. Client auth disables session tickets, so
# the new handshake is full; it must still be refused. A missing session
# file is not a refusal.
alpha_cert=$work/certs/alpha-new.pem
alpha_key=$work/keys/alpha-new.key
mkfifo "$work/go"
mkfifo "$work/resume-go"
cat >"$work/keepalive.py" <<'PY'
import socket
import ssl
import sys

port = int(sys.argv[1])
server_name = sys.argv[2]
ca = sys.argv[3]
cert = sys.argv[4]
key = sys.argv[5]
gate = sys.argv[6]
path = "/webhooks/peer-alpha"

ctx = ssl.create_default_context(cafile=ca)
ctx.load_cert_chain(cert, key)
raw = socket.create_connection(("127.0.0.1", port), 5)
tls = ctx.wrap_socket(raw, server_hostname=server_name)
tls.settimeout(5)
request = (
    f"GET {path} HTTP/1.1\r\n"
    f"Host: {server_name}:{port}\r\n"
    "Connection: keep-alive\r\n"
    "\r\n"
).encode()

def read_response(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(rest) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    status = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
    return status, rest[:length]

try:
    tls.sendall(request)
    status, body = read_response(tls)
except Exception as exc:
    print(f"FIRST-FAIL {type(exc).__name__}: {exc}", flush=True)
    sys.exit(0)
print(f"FIRST {status} {body.decode('ascii', 'replace')}", flush=True)
print("READY", flush=True)
# Block until the shell has removed the pin and reloaded.
with open(gate, "r", encoding="ascii") as handle:
    handle.readline()
try:
    tls.sendall(request)
    status, body = read_response(tls)
    print(f"SECOND {status} {body.decode('ascii', 'replace')}", flush=True)
except Exception as exc:
    print(f"SECOND-FAIL {type(exc).__name__}: {exc}", flush=True)
PY
cat >"$work/resume.py" <<'PY'
import socket
import ssl
import sys
import time

port = int(sys.argv[1])
server_name = sys.argv[2]
ca = sys.argv[3]
cert = sys.argv[4]
key = sys.argv[5]
gate = sys.argv[6]
path = "/webhooks/peer-alpha"

ctx = ssl.create_default_context(cafile=ca)
ctx.load_cert_chain(cert, key)
raw = socket.create_connection(("127.0.0.1", port), 5)
tls = ctx.wrap_socket(raw, server_hostname=server_name)
tls.settimeout(5)
request = (
    f"GET {path} HTTP/1.1\r\n"
    f"Host: {server_name}:{port}\r\n"
    "Connection: close\r\n"
    "\r\n"
).encode()
tls.sendall(request)
try:
    tls.recv(4096)
except OSError:
    pass
time.sleep(0.3)
session = tls.session
has_ticket = bool(session is not None and getattr(session, "has_ticket", False))
print(f"TICKET {int(has_ticket)}", flush=True)
print("READY", flush=True)
try:
    tls.close()
except OSError:
    pass
with open(gate, "r", encoding="ascii") as handle:
    handle.readline()
raw2 = socket.create_connection(("127.0.0.1", port), 5)
try:
    tls2 = ctx.wrap_socket(raw2, server_hostname=server_name, session=session)
    tls2.settimeout(5)
    tls2.sendall(request)
    data = tls2.recv(512)
except ssl.SSLError as exc:
    print(f"RESUME-FAIL {exc}", flush=True)
    sys.exit(0)
if not data:
    print("RESUME-EMPTY", flush=True)
    sys.exit(0)
status = data.split(b"\r\n", 1)[0].decode("ascii", "replace")
print(f"RESUME-HTTP {status}", flush=True)
PY
python3 -I "$work/keepalive.py" "$port" "$name" "$ca" "$alpha_cert" "$alpha_key" \
  "$work/go" >"$work/keepalive.out" 2>"$work/keepalive.err" &
keepalive_pid=$!
python3 -I "$work/resume.py" "$port" "$name" "$ca" "$alpha_cert" "$alpha_key" \
  "$work/resume-go" >"$work/resume.out" 2>"$work/resume.err" &
resume_pid=$!

wait_ready() {
  local pid=$1 file=$2
  local _
  for _ in $(seq 1 50); do
    if grep -q '^READY$' "$file" 2>/dev/null; then
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
    sleep 0.1
  done
  return 1
}

poke() {
  timeout 5 sh -c 'echo GO > "$1"' sh "$1" || true
}

keep_ready=no
resume_ready=no
if wait_ready "$keepalive_pid" "$work/keepalive.out"; then
  keep_ready=yes
fi
if wait_ready "$resume_pid" "$work/resume.out"; then
  resume_ready=yes
fi

rm -f "$work/active/alpha.pem"
if ! run_hook; then
  bad "removed pin refused on a new connection (resumption attempted) (hook failed)"
  bad "removed pin keep-alive connection dropped (hook failed)"
  poke "$work/go"
  poke "$work/resume-go"
else
  poke "$work/go"
  poke "$work/resume-go"
  wait "$keepalive_pid" 2>/dev/null || true
  keepalive_pid=
  wait "$resume_pid" 2>/dev/null || true
  resume_pid=
  if [ "$keep_ready" != yes ]; then
    bad "removed pin keep-alive connection dropped (connection never became ready)"
    cat "$work/keepalive.out" "$work/keepalive.err" >&2 || true
  elif ! grep -q '^FIRST HTTP/1.1 200 ' "$work/keepalive.out"; then
    bad "removed pin keep-alive connection dropped (first request was not 200: $(tr '\n' ' ' <"$work/keepalive.out"))"
  elif grep -Eq '^SECOND(-FAIL)? .*HTTP/1\.[01] 200' "$work/keepalive.out"; then
    bad "removed pin keep-alive connection dropped (second request succeeded: $(tr '\n' ' ' <"$work/keepalive.out"))"
  elif grep -q '^SECOND' "$work/keepalive.out"; then
    ok "removed pin keep-alive connection dropped"
  else
    bad "removed pin keep-alive connection dropped (no second result: $(tr '\n' ' ' <"$work/keepalive.out"))"
  fi
  if [ "$resume_ready" != yes ]; then
    bad "removed pin refused on a new connection (resumption attempted) (session was not saved)"
    cat "$work/resume.out" "$work/resume.err" >&2 || true
  elif grep -q '^RESUME-HTTP ' "$work/resume.out"; then
    bad "removed pin refused on a new connection (resumption attempted) ($(tr '\n' ' ' <"$work/resume.out"))"
  elif grep -Ei '^RESUME-FAIL .*(alert|certificate|internal)' "$work/resume.out" >/dev/null; then
    ok "removed pin refused on a new connection (resumption attempted)"
  else
    bad "removed pin refused on a new connection (resumption attempted) ($(tr '\n' ' ' <"$work/resume.out"))"
  fi
fi

# Empty folder: every handshake is refused, and the listener stays up.
rm -f "$work/active/"*.pem
if ! run_hook; then
  bad "empty active folder refuses every handshake (hook failed)"
elif ! tcp_open; then
  bad "empty active folder refuses every handshake (listener is down)"
else
  empty_fail=
  if ! probe_refused "$work/certs/alpha-new.pem" "$work/keys/alpha-new.key" own; then
    empty_fail="alpha=$(tr '\n' ' ' <"$work/probe.txt")"
  fi
  if ! probe_refused "$beta_cert" "$beta_key" own; then
    empty_fail="${empty_fail} beta=$(tr '\n' ' ' <"$work/probe.txt")"
  fi
  if ! probe_refused "$gamma_cert" "$gamma_key" own; then
    empty_fail="${empty_fail} gamma=$(tr '\n' ' ' <"$work/probe.txt")"
  fi
  if [ -n "$empty_fail" ]; then
    bad "empty active folder refuses every handshake (${empty_fail})"
  else
    ok "empty active folder refuses every handshake"
  fi
fi

# Body limit. This trial uses a small default so the posts stay short.
# Unit tests cover the 65536 default. beta's override is higher, and
# only beta's route uses it.
cp "$work/certs/alpha-new.pem" "$work/active/alpha.pem"
cp "$beta_cert" "$work/active/beta.pem"
body_limit=64
beta_limit=128
make_body "$work/body-under" $((body_limit - 1))
make_body "$work/body-at" "$body_limit"
make_body "$work/body-over" $((body_limit + 1))
make_body "$work/body-beta-at" "$beta_limit"
make_body "$work/body-beta-over" $((beta_limit + 1))
if ! run_hook --max-body "$body_limit" --max-body-peer "beta=${beta_limit}"; then
  bad "body just under the limit gets through (hook failed)"
  bad "body just over the limit is 413 (hook failed)"
  bad "per-peer override takes effect for that peer only (hook failed)"
elif ! python3 -I - "$config" "$body_limit" "$beta_limit" <<'PY'
import json, sys
path, default, beta = sys.argv[1:]
default, beta = int(default), int(beta)
routes = json.load(open(path, encoding="utf-8"))["apps"]["http"]["servers"]["agent-peers"]["routes"]
sizes = {}
for route in routes:
    match = route.get("match")
    if not match:
        continue
    for handle in route["handle"]:
        if handle.get("handler") == "request_body":
            sizes[match[0]["path"][0]] = handle["max_size"]
ok = (
    sizes.get("/webhooks/peer-alpha") == default
    and sizes.get("/webhooks/peer-beta") == beta
)
sys.exit(0 if ok else 1)
PY
then
  bad "body just under the limit gets through (rendered limits mismatch)"
  bad "body just over the limit is 413 (rendered limits mismatch)"
  bad "per-peer override takes effect for that peer only (rendered limits mismatch)"
else
  expect_post "body just under the limit gets through" \
    "$alpha_cert" "$alpha_key" "/webhooks/peer-alpha" \
    "$work/body-under" 200 "$((body_limit - 1))"
  expect_post "body at the limit gets through" \
    "$alpha_cert" "$alpha_key" "/webhooks/peer-alpha" \
    "$work/body-at" 200 "$body_limit"
  access_mark=0
  if [ -f "$access_log" ]; then
    access_mark=$(wc -c <"$access_log" | tr -d ' ')
  fi
  expect_post "body just over the limit is 413" \
    "$alpha_cert" "$alpha_key" "/webhooks/peer-alpha" \
    "$work/body-over" 413
  wait_log "body just over the limit is logged" "$access_log" 413 "$access_mark" \
    "/webhooks/peer-alpha" "CN=alpha"
  upstream_lacks "body just over the limit does not reach upstream" \
    "/webhooks/peer-alpha $((body_limit + 1))"
  expect_post "per-peer override allows that peer a larger body" \
    "$beta_cert" "$beta_key" "/webhooks/peer-beta" \
    "$work/body-over" 200 "$((body_limit + 1))"
  expect_post "per-peer override at its own limit gets through" \
    "$beta_cert" "$beta_key" "/webhooks/peer-beta" \
    "$work/body-beta-at" 200 "$beta_limit"
  expect_post "per-peer override does not raise the other peer" \
    "$alpha_cert" "$alpha_key" "/webhooks/peer-alpha" \
    "$work/body-beta-at" 413
  upstream_lacks "other peer's larger body does not reach upstream" \
    "/webhooks/peer-alpha ${beta_limit}"
  expect_post "per-peer override just over is 413" \
    "$beta_cert" "$beta_key" "/webhooks/peer-beta" \
    "$work/body-beta-over" 413
fi

echo "trial: ${passes} passed, ${fails} failed"
if [ "$fails" -ne 0 ]; then
  echo "trial: caddy log:" >&2
  tail -n 40 "$work/caddy.log" >&2 || true
  exit 1
fi
