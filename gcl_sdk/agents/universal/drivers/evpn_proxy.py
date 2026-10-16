#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Host-local netboot/metadata proxy for the ovs_evpn overlay.

One process per hypervisor, one listener per VNI inside that VNI's
namespace: br-int punts guest HTTP for 169.254.169.254 into ``nf<vni>``
and the kernel TCP stack terminates it there, so overlapping CIDRs never
share a socket. Serves the boot script itself and relays an allowlist of
upstream prefixes; everything else is 404 (never an open relay).
"""

import base64
import errno
import http.server
import json
import os
import posixpath
import re
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from gcl_sdk.agents.universal.api import boot_identity
from gcl_sdk.agents.universal.drivers import evpn_records

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Finds candidate machine uuids; the gate compares them to the caller's.
_UUID_RE = re.compile(_UUID)

METADATA_IP = "169.254.169.254"
NODE_UUID_HEADER = "X-Genesis-Node-UUID"
METADATA_BASE_URL = "http://%s" % METADATA_IP
# The diskless guest's chain script; iPXE fills `${uuid}` (SMBIOS), so it
# holds nothing per-guest.
BOOT_SCRIPT_PATH = "/boot/boot.ipxe"
BOOT_SCRIPT = "#!ipxe\nchain %s/v1/boots/${uuid}\n" % METADATA_BASE_URL
# Everything a guest calls on the platform's APIs; anything else is refused.
# (method, path, whether the request must claim to be the caller). `{me}`
# must be the caller; a resource is named by a uuid of its own.
PLATFORM_CALLS = tuple(
    (
        method,
        re.compile(
            "^%s$" % path.replace("{me}", "(?P<me>%s)" % _UUID).replace("{uuid}", _UUID)
        ),
        claims,
    )
    for method, path, claims in (
        ("GET", "/v1/boots/{me}", False),  # iPXE
        ("POST", "/v1/agents/", True),
        ("PUT", "/v1/agents/{me}", False),
        ("GET", "/v1/agents/{me}/actions/get_payload", False),
        ("GET", "/v1/node_verifiers/{me}/actions/verify", False),
        # Seed OS takes the node's key after flashing the disk.
        ("POST", "/v1/nodes/{me}/actions/refresh_secret/invoke", False),
        ("POST", "/v1/kind/[^/]+/resources/", True),
        ("GET", "/v1/kind/[^/]+/resources/{uuid}", True),
        ("PUT", "/v1/kind/[^/]+/resources/{uuid}", True),
        ("DELETE", "/v1/kind/[^/]+/resources/{uuid}", True),
    )
)


_CACHE = evpn_records.DirCache()


def _prefix_grants(specs):
    grants = {}
    for spec in specs or []:
        prefix, _, upstream = str(spec).partition("=")
        if prefix and upstream:
            grants[prefix] = upstream
    return grants


def _port_grants(specs):
    grants = {}
    for port, upstream in (specs or {}).items():
        try:
            grants[int(port)] = str(upstream)
        except (TypeError, ValueError):
            continue
    return grants


def _load_proxy_config(records_dir):
    config = evpn_records.nf_config(records_dir, "proxy")
    return {
        "forwards": _prefix_grants(config.get("forwards")),
        "apis": _prefix_grants(config.get("apis")),
        "ports": _port_grants(config.get("ports")),
        "services": _port_grants(config.get("services")),
    }


def _load_callers(records_dir):
    """Which guest holds which address, from this VNI's records."""
    return {
        str(rec["ipv4"]): str(rec["node"])
        for rec in evpn_records.guest_records(records_dir)
        if rec.get("ipv4") and rec.get("node")
    }


def _proxy_config(records_dir):
    """What this VNI's `proxy` function grants, by kind of grant.

    Per network, never a host-wide union (that would relay one tenant's
    upstreams for another's guest).

    * ``forwards`` — published static subtrees (prefix -> upstream).
    * ``ports`` — the platform's unauthenticated APIs (port -> upstream);
      the caller is held to its own business here.
    * ``apis`` — upstreams that check credentials themselves; relayed whole.
    * ``services`` — doors onto installation services; relayed like an
      `api` but stamped with `NODE_UUID_HEADER` (a second factor only).

    Safe because only the installation (or a destination the caller owns)
    can name a grant.
    """
    return _CACHE.get(
        records_dir,
        "proxy",
        _load_proxy_config,
        watch=(evpn_records.nf_config_file(records_dir, "proxy"),),
    )


def wanted_ports(records_dir):
    """Every port this VNI's proxy should be answering on.

    Must match the ports br-int claims, or a listener gets nothing / a
    claimed port is a black hole.
    """
    ports = {METADATA_PORT}
    ports.update(PLATFORM_PORTS)
    ports.update(_proxy_config(records_dir)["services"])
    return ports


def caller_uuid(records_dir, address):
    """Which guest of this VNI is asking, from its address.

    Trusted: the address is CP-allocated and port-security flows pin it.
    """
    return _CACHE.get(records_dir, "callers", _load_callers).get(address)


# A hand-built opener: the default one would fetch `file://`/`ftp://`
# grants (reading the hypervisor), honour proxy env vars, and follow
# redirects out of a grant. Without the redirect handler a 3xx arrives as
# `HTTPError` and is relayed verbatim to the guest.
_RELAYABLE_SCHEMES = frozenset(("http", "https"))


def _build_opener():
    opener = urllib.request.OpenerDirector()
    handlers = [urllib.request.HTTPHandler()]
    if hasattr(urllib.request, "HTTPSHandler"):
        handlers.append(urllib.request.HTTPSHandler())
    # Without these a non-2xx makes `open()` return None, not `HTTPError`.
    handlers += [
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ]
    for handler in handlers:
        opener.add_handler(handler)
    return opener


_OPENER = _build_opener()


def _is_relayable(url) -> bool:
    """Whether this is a URL this proxy will fetch at all."""
    parsed = urllib.parse.urlsplit(str(url))
    return parsed.scheme.lower() in _RELAYABLE_SCHEMES and bool(parsed.netloc)


#: What `_body` returns when it has already answered the caller itself.
_REFUSED = object()


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    records_dir = "/var/lib/exordos/evpn/nf"
    listen_port = 80
    # Workers are bounded; an idle connection must not hold one for good.
    timeout = 10

    @property
    def forwards(self):
        """What this listener will relay: one API, or the prefix allowlist."""
        config = _proxy_config(self.records_dir)
        upstream = config["ports"].get(self.listen_port) or config["services"].get(
            self.listen_port
        )
        if upstream:
            return {"/": upstream}
        grants = dict(config["forwards"])
        grants.update(config["apis"])
        return grants

    @property
    def _rewrites(self):
        """Every upstream this VNI grants, and where the guest reaches it.

        All grants, not just this listener's: a boot script from one port
        names a kernel served under a prefix on another.
        """
        config = _proxy_config(self.records_dir)
        granted = dict(config["forwards"])
        granted.update(config["apis"])
        rewrites = {
            upstream: "%s%s" % (METADATA_BASE_URL, prefix.rstrip("/"))
            for prefix, upstream in granted.items()
        }
        by_port = dict(config["ports"])
        by_port.update(config["services"])
        for port, upstream in by_port.items():
            proxy = METADATA_BASE_URL
            if port != METADATA_PORT:
                proxy = "%s:%d" % (METADATA_BASE_URL, port)
            rewrites[upstream] = proxy
        return rewrites

    def log_message(self, fmt, *args):  # quieter logs
        pass

    def _serve_boot_script(self):
        body = BOOT_SCRIPT.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Headers dropped in both directions. A denylist, not an allowlist: the
    # agents sign requests and need headers (the nonce) nobody would list.
    _HOP_BY_HOP = frozenset(
        h.lower()
        for h in (
            "host",
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
            "content-length",
        )
    )

    # Who the client is, as a proxy in front would say it. The guest is not
    # one: its copy would be taken upstream as this hop's word.
    @staticmethod
    def _is_forwarding_claim(header):
        return header in ("forwarded", "x-real-ip") or header.startswith("x-forwarded-")

    def _forward(self, prefix, upstream, path, method, payload=None):
        rest = path[len(prefix) :].lstrip("/")
        # No `..` out of the grant, checked on the path once-decoded as the
        # upstream sees it (`%2e%2e`, `%2f` would slip past a raw check).
        walked = urllib.parse.unquote(rest)
        if (
            posixpath.normpath("/" + walked) != "/" + walked.rstrip("/")
            or ".." in walked
        ):
            self.send_error(403)
            return
        url = upstream.rstrip("/") + "/" + rest
        if "?" in self.path:
            url += "?" + self.path.split("?", 1)[1]
        if not _is_relayable(url):
            # An installation misconfiguration: log it, tell the guest only 502.
            sys.stderr.write("evpn-proxy: refusing to relay %r\n" % (upstream,))
            self.send_error(502)
            return
        req = urllib.request.Request(url, data=payload, method=method)
        for header, value in self.headers.items():
            lowered = header.lower()
            if (
                lowered in self._HOP_BY_HOP
                or lowered == NODE_UUID_HEADER.lower()
                or lowered
                in {
                    boot_identity.HOST_HEADER.lower(),
                    boot_identity.TIME_HEADER.lower(),
                    boot_identity.NONCE_HEADER.lower(),
                    boot_identity.SIGNATURE_HEADER.lower(),
                }
                or self._is_forwarding_claim(lowered)
            ):
                # Never pass on the caller's claim; only the record's value
                # below leaves here.
                continue
            req.add_header(header, value)
        me = caller_uuid(self.records_dir, self.client_address[0])
        if me is not None:
            req.add_header(NODE_UUID_HEADER, me)
            key_path = os.environ.get("EXORDOS_BOOT_PROXY_KEY_FILE")
            host = os.environ.get("EXORDOS_BOOT_PROXY_HOST")
            if key_path and host and self.listen_port == 11013:
                with open(key_path) as key_file:
                    key = base64.b64decode(key_file.read())
                timestamp = str(int(time.time()))
                nonce = str(uuid.uuid4())
                for header, value in (
                    (boot_identity.HOST_HEADER, host),
                    (boot_identity.TIME_HEADER, timestamp),
                    (boot_identity.NONCE_HEADER, nonce),
                    (
                        boot_identity.SIGNATURE_HEADER,
                        boot_identity.signature(
                            key,
                            host,
                            me,
                            timestamp,
                            nonce,
                            method,
                            req.selector,
                            payload or b"",
                        ),
                    ),
                ):
                    req.add_header(header, value)
        try:
            with _OPENER.open(req, timeout=30) as resp:
                content_type = resp.headers.get("Content-Type") or ""
                length = resp.headers.get("Content-Length")
                # Buffer only what gets rewritten; stream the rest (kernels,
                # initrds) so a netboot cannot eat the shared process's memory.
                if self._rewritable(content_type, length):
                    self._relay(resp.status, content_type, resp.read(), resp.headers)
                else:
                    self._stream(resp.status, resp.headers, resp)
        except urllib.error.HTTPError as err:
            # Relay the upstream's own status and body; the agent reads them.
            self._relay(
                err.code,
                err.headers.get("Content-Type"),
                err.read() or b"",
                err.headers,
            )
        except (urllib.error.URLError, OSError):
            self.send_error(502)

    _REWRITABLE = ("text/", "application/json", "application/octet-stream")
    # Above this, stream instead of rewrite: kernels arrive as octet-stream too.
    _REWRITE_LIMIT = 1 << 20
    _STREAM_CHUNK = 1 << 16

    def _rewritable(self, content_type, length):
        if not any(content_type.startswith(kind) for kind in self._REWRITABLE):
            return False
        try:
            # Unknown length is not "small".
            return int(length) <= self._REWRITE_LIMIT
        except (TypeError, ValueError):
            return False

    def _stream(self, status, headers, resp):
        """Copy the upstream body through without ever holding it whole."""
        self.send_response(status)
        for header, value in (headers or {}).items():
            if header.lower() not in self._HOP_BY_HOP:
                self.send_header(header, value)
        length = headers.get("Content-Length") if headers else None
        if length is not None:
            self.send_header("Content-Length", length)
        else:
            # Nothing else can delimit the body once it is not buffered.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        while True:
            chunk = resp.read(self._STREAM_CHUNK)
            if not chunk:
                return
            self.wfile.write(chunk)

    def _rewrite_upstreams(self, body, content_type):
        """Point the guest back at this proxy for everything it is granted.

        Bodies (the boot script) name upstreams by addresses an overlay guest
        cannot reach. Only granted upstreams are rewritten.
        """
        if not any(content_type.startswith(kind) for kind in self._REWRITABLE):
            return body
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body
        # Longest first: one upstream may be a prefix of another.
        for upstream, proxy in sorted(
            self._rewrites.items(), key=lambda kv: len(kv[0]), reverse=True
        ):
            text = text.replace(upstream.rstrip("/"), proxy)
        return text.encode("utf-8")

    def _relay(self, status, content_type, body, headers=None):
        """Hand the answer back whole — headers included (agents read the nonce)."""
        body = self._rewrite_upstreams(body, content_type or "")
        self.send_response(status)
        for header, value in (headers or {}).items():
            if header.lower() not in self._HOP_BY_HOP:
                self.send_header(header, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _named_machines(self, req):
        """Every machine this request names, query included, lowercased."""
        return {found.lower() for found in _UUID_RE.findall(req)}

    def _claims_to_be(self, me, payload):
        """Whether the request claims to be the caller (`me`, from the address).

        The claim only has to agree with the source address. Looked for in
        the header, else `uuid`/`node` in the body or in its ``value``
        envelope (resource reports).
        """
        claimed = self.headers.get(NODE_UUID_HEADER)
        if claimed and str(claimed).strip().lower() == me:
            return True
        if not payload:
            return False
        try:
            body = json.loads(payload)
        except ValueError:
            return False
        if not isinstance(body, dict):
            return False
        bodies = [body]
        value = body.get("value")
        if isinstance(value, dict):
            bodies.append(value)
        return any(
            str(where.get(field, "")).lower() == me
            for where in bodies
            for field in ("uuid", "node")
        )

    def _about_the_caller(self, req, method, payload, kind="platform"):
        """Whether the platform may answer this request for this guest.

        An allow-list: the platform's APIs authenticate nobody, so this is
        the only place a guest is held to its own business.

        * **boot** / **published** — static, any guest, GET/HEAD only.
        * **api** — authenticates itself; relayed whole.
        * **service** — authenticates itself, but an unknown caller is
          refused rather than relayed without an identity.
        * **platform** — one of `PLATFORM_CALLS`, naming no machine but the
          caller (query included); a new route is closed until listed.
        """
        if kind in ("boot", "published"):
            return method in ("GET", "HEAD")
        if kind == "api":
            return True
        me = caller_uuid(self.records_dir, self.client_address[0])
        if me is None:
            return False
        if kind == "service":
            return True
        me = me.lower()
        for allowed, path, claims in PLATFORM_CALLS:
            if method != allowed or not path.match(req):
                continue
            if "me" in path.groupindex and self._named_machines(self.path) != {me}:
                return False
            return not claims or self._claims_to_be(me, payload)
        return False

    @staticmethod
    def _prefix_matches(req, prefix):
        """A grant is of a path, not a string: ``/boot`` must not match ``/bootstrap``."""
        if prefix == "/":
            return True
        if not req.startswith(prefix):
            return False
        rest = req[len(prefix) :]
        return rest == "" or rest.startswith("/")

    def _route(self, req):
        """Where this request would go: ``(prefix, upstream, kind)``.

        kind is one of boot/published/api/platform/service (see
        `_about_the_caller`). Longest prefix first, so `/` shadows nothing.
        """
        if req == BOOT_SCRIPT_PATH:
            return BOOT_SCRIPT_PATH, None, "boot"
        # A service port is a whole service; its paths are not ours to narrow.
        config = _proxy_config(self.records_dir)
        service = config["services"].get(self.listen_port)
        if service:
            return "/", service, "service"
        apis = config["apis"]
        for prefix, upstream in sorted(
            self.forwards.items(), key=lambda kv: len(kv[0]), reverse=True
        ):
            if not self._prefix_matches(req, prefix):
                continue
            if prefix in apis:
                return prefix, upstream, "api"
            return prefix, upstream, "platform" if prefix == "/" else "published"
        return None, None, None

    def _dispatch(self, method, payload=None):
        req = self.path.split("?", 1)[0]
        prefix, upstream, kind = self._route(req)
        if prefix is None:
            # 404 vs 403 only reveals the installation's grants, not machines.
            self.send_error(404)
            return
        if "%" in req and kind not in ("api", "service"):
            # The gate reads the path as sent and the upstream decodes it:
            # `/v1/%61gents/<uuid>` would name a machine the gate never saw.
            self.send_error(400)
            return
        if not self._about_the_caller(req, method, payload, kind=kind):
            self.send_error(403)
            return
        if kind == "boot":
            self._serve_boot_script()
            return
        self._forward(prefix, upstream, req, method, payload)

    #: Request body ceiling: `max_workers` bounds threads, not their bytes.
    _BODY_LIMIT = 1 << 20

    def _body(self):
        """The request body, read once; on refusal answers itself, returns `_REFUSED`."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self.send_error(400)
            return _REFUSED
        if length < 0 or length > self._BODY_LIMIT:
            self.send_error(413)
            return _REFUSED
        return self.rfile.read(length) if length else None

    def _dispatch_with_body(self, method):
        body = self._body()
        if body is _REFUSED:
            return
        self._dispatch(method, body)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch_with_body("POST")

    def do_PUT(self):
        self._dispatch_with_body("PUT")

    def do_PATCH(self):
        self._dispatch_with_body("PATCH")

    def do_DELETE(self):
        self._dispatch_with_body("DELETE")


class BoundedThreadingMixIn(socketserver.ThreadingMixIn):
    """A threading server with a ceiling on concurrent connections.

    Plain ``ThreadingMixIn`` gives each guest connection a thread in a
    process all tenants share; here excess connections wait in the accept
    queue. Also used by the responder's DNS-over-TCP listener.
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64
    # Not `max_children`: that is `ForkingMixIn`'s and caps nothing here.
    max_workers = 16

    def __init__(self, *args, **kwargs):
        self._slots = threading.Semaphore(self.max_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        # Released in `shutdown_request`, called on every path out.
        self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            self._slots.release()


class _VniHTTPServer(BoundedThreadingMixIn, http.server.HTTPServer):
    """The VNI's HTTP listener, bound without ``getfqdn``.

    ``HTTPServer.server_bind`` resolves its name, and inside a tenant
    namespace the host's resolver is unreachable: bind then hung for minutes
    before listening.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def _handler(records_dir, listen_port=80):
    return type(
        "VniProxyHandler",
        (ProxyHandler,),
        {
            "records_dir": records_dir,
            "listen_port": listen_port,
        },
    )


# How long a listener retries its bind: the address is put on the VNI's
# port later in the same apply that started the worker.
BIND_RETRY_SECONDS = 60
BIND_RETRY_INTERVAL = 0.5


def _bind_listener(bind, port, records_dir):
    """The listener, once its address exists. None if it never does."""
    deadline = time.monotonic() + BIND_RETRY_SECONDS
    while True:
        try:
            return _VniHTTPServer((bind, port), _handler(records_dir, port))
        except OSError as e:
            # EADDRNOTAVAIL: the VNI's port does not hold the address yet.
            if e.errno != errno.EADDRNOTAVAIL or time.monotonic() >= deadline:
                raise
            time.sleep(BIND_RETRY_INTERVAL)


class Listeners:
    """Every listener one VNI's proxy holds, by port, so they can be given back.

    A leaked listener's socket keeps the namespace alive after `ip netns
    del`. A listener still retrying its bind at shutdown must find the door
    shut rather than register behind it. Ports change at runtime (doors
    are data), without restarting the VNI's responder.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._servers = {}
        # Ports held or being bound, so a sweep never starts a second thread.
        self._claimed = set()
        self._closed = False

    def claim(self, port) -> bool:
        """Take responsibility for a port before a thread goes to bind it."""
        with self._lock:
            if self._closed or port in self._claimed:
                return False
            self._claimed.add(port)
            return True

    def add(self, port, srv) -> bool:
        """Take a listener; False (and closed for you) if we are shutting."""
        with self._lock:
            if not self._closed:
                self._servers[port] = srv
                return True
        srv.server_close()
        return False

    def give_up(self, port) -> None:
        """A thread that will not be serving this port lets it go."""
        with self._lock:
            self._claimed.discard(port)

    def ports(self) -> set:
        """The ports held or being claimed right now."""
        with self._lock:
            return set(self._claimed)

    def close(self, port) -> None:
        """Give one port back, synchronously.

        A re-claim right after would otherwise hit `EADDRINUSE`, and no later
        sweep retries a claimed port.
        """
        with self._lock:
            srv = self._servers.pop(port, None)
            self._claimed.discard(port)
        if srv is None:
            return
        try:
            srv.shutdown()
            srv.server_close()
        except OSError:
            pass

    def reconcile(self, records_dir, wanted):
        """Hold exactly the ports asked for."""
        wanted = set(wanted)
        held = self.ports()
        for port in sorted(wanted - held):
            if not self.claim(port):
                continue
            threading.Thread(
                target=_serve_port,
                args=(records_dir, port, self),
                daemon=True,
            ).start()
        for port in sorted(held - wanted):
            self.close(port)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            servers, self._servers = self._servers, {}
            self._claimed.clear()
        for srv in servers.values():
            try:
                srv.shutdown()
                srv.server_close()
            except OSError:
                pass


def serve(records_dir, port, listeners):
    """Serve one port in the namespace this thread is already in.

    Platform ports bind any address: the bridge delivers them here whatever
    address the guest asked (its gateway, the core's name).
    """
    bind = METADATA_IP if port == METADATA_PORT else ""
    srv = _bind_listener(bind, port, records_dir)
    if not listeners.add(port, srv):
        return  # the worker stopped while this one was waiting for its address
    srv.serve_forever()


def _serve_port(records_dir, port, listeners):
    """One listener's whole life; the claim is released on every path out,
    or a failed bind would stay claimed and never be retried.
    """
    try:
        serve(records_dir, port, listeners)
    except Exception as e:
        sys.stderr.write("evpn-proxy: no listener on %s: %r\n" % (port, e))
    finally:
        listeners.give_up(port)


METADATA_PORT = 80
# Claimed by number on the bridge: guests address them elsewhere.
BOOT_CHAIN_PORT = 11013
PLATFORM_PORTS = (11011, 11012, BOOT_CHAIN_PORT)


def run_in_thread(records_dir):
    """Start the proxy in daemon threads of the current namespace.

    Returns the `Listeners` for the owning worker to shut down; later sweeps
    are that worker's (`evpn_nf.serve_vni`).
    """
    listeners = Listeners()
    listeners.reconcile(records_dir, wanted_ports(records_dir))
    return listeners
