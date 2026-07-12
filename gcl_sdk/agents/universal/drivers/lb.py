#    Copyright 2025 Genesis Corporation.
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
from __future__ import annotations

import concurrent
import glob
import logging
import os
import pathlib
import shutil
import subprocess
import uuid

from gcl_sdk.agents.universal import constants
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.agents.universal.storage import common as storage_common
from gcl_sdk.common import utils
from gcl_sdk.infra import constants as ic
from gcl_sdk.paas.dm import lb as lb_models

LOG = logging.getLogger(__name__)

LB_TARGET_KIND = "paas_lb_node"
LB_AGENT_TARGET_KIND = "paas_lb_agent"
BALANCE_MAPPING = {
    "roundrobin": "",
    "leastconn": "least_conn;",
}
NGINX_L7_CONFIG_FILE = "/etc/nginx/conf.d/exordos_lb.conf"
NGINX_L4_CONFIG_FILE = "/etc/nginx/exordos/l4.conf"
NGINX_SSL_DIR = "/etc/nginx/ssl/"
NGINX_USER = NGINX_GROUP = "www-data"
# Drop all if not set by user, convenient default
ROOT_LOCATION = """\
location / {
        return 444;
    }
"""
LOCATION_TYPE_MAPPING = {
    "prefix": "",
    "exact": "=",
    "regex": "~",
}
ADD_HEADERS_MAPPING = {
    "Host": "proxy_set_header Host $host;",
    "X-Forwarded-For": "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
    "X-Forwarded-Proto": "proxy_set_header X-Forwarded-Proto $scheme;",
    "X-Forwarded-Port": (
        lambda vhost, route: f'proxy_set_header X-Forwarded-Port "{vhost["port"]}";'
    ),
    "X-Forwarded-Prefix": (
        lambda vhost, route: (
            f'proxy_set_header X-Forwarded-Prefix "{route["value"]}";'
            if route["value"] != "/"
            else ""
        )
    ),
}
DOWNLOAD_DIR = "/var/www/gc_downloaded/"
DOWNLOAD_DIR_TMP = "/var/www/gc_downloaded_tmp/"

SYSTEMD_TEMPLATE = """\
[Unit]
Description=Exordos Tunnel service for %i
After=network.target

[Service]
ExecStart=/var/lib/exordos/tunnels/%i.sh
RestartSec=5
Restart=always

[Install]
WantedBy=multi-user.target
"""
SYSTEMD_TMPL_PATH = "/etc/systemd/system/exordos-tunnel@.service"

TUNNEL_SCRIPT_PATH = "/var/lib/exordos/tunnels/"

REMOTE_SCRIPT = """\
set -eux

cat > /tmp/exordos_nginx_{port}_{mode}.conf <<EOF
load_module /usr/lib/nginx/modules/ngx_stream_module.so;
pid /tmp/exordos_nginx_{port}_{mode}.pid;

events {{
    worker_connections  1024;
}}

stream {{
    upstream backend {{
       server unix:/tmp/exordos_nginx_{port}_{mode}.sock;
    }}

    server {{
        listen {port} {proto};
        proxy_pass backend;
        proxy_protocol on;
    }}
}}
EOF

PID_FILE="/tmp/exordos_nginx_{port}_{mode}.pid"

if [ -f "$PID_FILE" ]; then
    pkill -F "$PID_FILE"
fi;

if [ -S "/tmp/exordos_nginx_{port}_{mode}.sock" ]; then
    if socat -u OPEN:/dev/null UNIX-CONNECT:/tmp/exordos_nginx_{port}_{mode}.sock; then
        echo "Socket is alive, continue"
    else
        rm /tmp/exordos_nginx_{port}_{mode}.sock || true
        exit 1
    fi
fi;

trap "pkill -F $PID_FILE && rm /tmp/exordos_nginx_{port}_{mode}.sock || true" EXIT

/usr/sbin/nginx -g "daemon off; master_process off;" -e stderr -c /tmp/exordos_nginx_{port}_{mode}.conf

"""

TUNNEL_SCRIPT_TEMPLATE = """\
#!/bin/bash
set -xe

MODE={mode}


COMMAND="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -F /dev/null {ssh_user}@{ssh_host} -p {ssh_port} -i /var/lib/exordos/tunnels/{name}.key"
REMOTE_COMMAND=$(cat <<'END'
{remote_command}
END
)

if [[ "$MODE" == "tcp" ]]; then
    if [[ "$REMOTE_COMMAND" = *[^[:space:]]* ]]; then
        $COMMAND -R /tmp/exordos_nginx_{port}_{mode}.sock:0.0.0.0:{port} <<END
$REMOTE_COMMAND
END
    else
        $COMMAND -N -T -R 0.0.0.0:{port}:0.0.0.0:{port}
    fi;
fi;

if [[ "$MODE" == "udp" ]]; then
    socat tcp-listen:{port},fork,reuseaddr udp:0.0.0.0:{port} &
    $COMMAND -R 0.0.0.0:{port}:0.0.0.0:{port} socat udp-listen\\:{port},fork,reuseaddr tcp\\:0.0.0.0:{port} &
    wait -n
fi;
"""


def secure_opener(path, flags):
    return os.open(path, flags, 0o700)


def executable_opener(path, flags):
    return os.open(path, flags, 0o755)


TPOOL = concurrent.futures.ThreadPoolExecutor(max_workers=10)


class LB(lb_models.LB, meta.MetaDataPlaneModel):
    META_PATH = os.path.join(constants.WORK_DIR, "lb_meta.json")

    _download_dirs_futures = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._meta_file = self.META_PATH
        self._common_storage = storage_common.JsonFileStorageSingleton.get_instance(
            self._meta_file
        )
        if "lb_driver_info" not in self._common_storage:
            self._common_storage["lb_driver_info"] = {"download_dirs": {}}
        self._download_dirs = self._common_storage["lb_driver_info"]["download_dirs"]

    def get_meta_model_fields(self) -> set[str] | None:
        """Return a list of meta fields or None.

        Meta fields are the fields that cannot be fetched from
        the data plane or we just want to save them into the meta file.

        `None` means all fields are meta fields but it doesn't mean they
        won't be updated from the data plane.
        """

        # Keep all fields as meta fields for simplicity
        return {
            "uuid",
            "vhosts",
            "backend_pools",
        }

    def _dataplane_views(self) -> list[dict]:
        """All LB views sharing this node's nginx dataplane, self included.

        The nginx config files are one shared dataplane, but the agent may
        carry several LB resources (e.g. the element's own core_agent LB and
        the ecosystem's realm-ingress LB). Rendering only ``self`` would make
        every apply of one LB wipe the other's vhosts and the two resources'
        validations flip-flop forever, reload-storming nginx. So every render
        and validation is done over the union of all LB resources in the meta
        storage, uuid-sorted — every resource produces the byte-identical
        file, so a correct file satisfies all of them at once.

        ``self`` is authoritative for its own uuid (the meta may hold its
        pre-update view while an update is being applied).
        """
        views = {}
        for capstor in self._common_storage.values():
            # Skip non-capability entries such as lb_driver_info.
            if not isinstance(capstor, dict) or "resources" not in capstor:
                continue
            for view_uuid, view in capstor["resources"].items():
                views[str(view_uuid)] = view
        views[str(self.uuid)] = {
            "uuid": str(self.uuid),
            "vhosts": self.vhosts,
            "backend_pools": self.backend_pools,
        }
        return [views[u] for u in sorted(views)]

    def _agg_vhosts(self) -> list[dict]:
        vhosts = []
        for view in self._dataplane_views():
            vhosts.extend(view.get("vhosts") or [])
        return vhosts

    def _agg_pools(self) -> dict:
        pools = {}
        for view in self._dataplane_views():
            pools.update(view.get("backend_pools") or {})
        return pools

    def _gen_backends(self, proto_lvl):
        upstreams = []
        for pid, pool in self._agg_pools().items():
            servers = "\n    ".join(
                f"server {e['host']}:{e['port']} weight={e['weight']};"
                for e in pool["endpoints"]
                if e["kind"] == "host"
            )
            upstreams.append(f"""\
            upstream {pid} {{
                {BALANCE_MAPPING[pool.get("balance", "roundrobin")]}
                zone {pid}_{proto_lvl} 64K;
                {servers}
                {"keepalive 2;" if proto_lvl == "l7" else ""}
            }}
            """)
        return upstreams

    def _gen_modifiers(self, vhost, route, modifiers):
        res = []
        for m in modifiers:
            if m["kind"] == "auto_header":
                for h in m["headers"]:
                    val = ADD_HEADERS_MAPPING[h]
                    res.append(val(vhost, route) if callable(val) else val)
            elif m["kind"] == "set_header":
                name = m["name"].replace('"', '\\"')
                value = m["value"].replace('"', '\\"')
                res.append(f'proxy_set_header "{name}" "{value}";')
            elif m["kind"] == "rewrite_url":
                reg = m["regex"].replace('"', '\\"')
                repl = m["replacement"].replace('"', '\\"')
                res.append(f'rewrite "{reg}" "{repl}" break;')
        return res

    def _gen_vhosts(self):
        vhosts_l4 = []
        vhosts_l7 = []
        ext_sources = {}
        # Only one server per port may carry default_server; with several
        # catch-all ("_") vhosts aggregated the first (uuid-sorted, so
        # deterministic) wins instead of failing the whole nginx config.
        default_ports = set()
        for v in self._agg_vhosts():
            if len(v["routes"]) == 0:
                continue
            if v["proto"].startswith("http"):
                is_default = "_" in v["domains"] and v["port"] not in default_ports
                if is_default:
                    default_ports.add(v["port"])
                vhosts_l7.append(self._gen_vhost_l7(v, is_default))
                proto = "tcp"
            else:
                vhosts_l4.append(self._gen_vhost_l4(v))
                proto = "udp"
            for ext_source in v.get("ext_sources", []):
                e = ext_source.copy()
                e["lport"] = v["port"]
                e["lproto"] = proto
                e["proxy_proto_from"] = v.get("proxy_proto_from")
                ext_sources[f"{e['host']}_{e['lport']}_{e['lproto']}"] = e

        return vhosts_l4, vhosts_l7, ext_sources

    def _gen_vhost_l4(self, v):
        for r in v["routes"].values():
            c = r["cond"]
            ips = "    \n".join(f"allow {ip};" for ip in c["allowed_ips"])
            return f"""\
server {{
listen 0.0.0.0:{v["port"]}{f" {v['proto']}" if v["proto"] != "tcp" else ""};
{ips}
deny all;
proxy_pass {c["actions"][0]["pool"]};
}}
"""
        return ""

    def _gen_file_content_l4(self, vhosts) -> str:
        backends = "\n".join(b for b in self._gen_backends(proto_lvl="l4"))
        vhosts = "\n".join(v for v in vhosts)
        return f"""\
stream {{
{backends}

{vhosts}
}}
"""

    def _gen_vhost_l7(self, v, is_default=False):
        locations = [ROOT_LOCATION]
        for r in v["routes"].values():
            c = r["cond"]

            actions = []
            for a in c["actions"]:
                if a["kind"] == "backend":
                    actions.append(f"""\
proxy_pass {a["protocol"]["kind"]}://{a["pool"]};""")
                    if (
                        a["protocol"]["kind"] == "https"
                        and a["protocol"]["verify"] is not True
                    ):
                        actions.append("proxy_ssl_verify off;")
                    break
                elif a["kind"] == "redirect":
                    actions.append(f"""\
return {a["code"]} {a["url"]}$request_uri;""")
                    break
                elif a["kind"] == "local_dir":
                    actions.append(f"""\
alias {os.path.join(a["path"], "")};""")
                    if a.get("is_spa"):
                        actions.append("try_files $uri $uri/ /index.html;")
                    break
                elif a["kind"] == "local_dir_download":
                    actions.append(
                        f"""\
alias {os.path.join(DOWNLOAD_DIR, str(uuid.uuid5(uuid.NAMESPACE_URL, f"{v['uuid']}{c['kind']}{c['value']}")), "")};"""
                    )
                    if a.get("is_spa"):
                        actions.append("try_files $uri $uri/ /index.html;")
                    break
            # Upgrade + Connection headers must be inside location
            acts = "\n    ".join(a for a in actions)
            mods = "\n    ".join(m for m in self._gen_modifiers(v, c, c["modifiers"]))
            loc = f"""
location {LOCATION_TYPE_MAPPING[c["kind"]]} {c["value"]} {{
    {acts}
    {mods}
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
}}"""
            if c["value"] == "/":
                # Replace default root location
                locations[0] = loc
            else:
                locations.append(loc)

        part = (
            f"""\
        set_real_ip_from {v["proxy_proto_from"]};
        real_ip_header proxy_protocol;"""
            if v.get("proxy_proto_from")
            else ""
        )

        if v["proto"] == "https":
            ssl_info = f"""
ssl_certificate      {NGINX_SSL_DIR}{v["uuid"]}_exordos.crt;
ssl_certificate_key  {NGINX_SSL_DIR}{v["uuid"]}_exordos.key;
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ecdh_curve X25519:prime256v1:secp384r1;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:DHE-RSA-CHACHA20-POLY1305;
"""
        else:
            ssl_info = ""

        locs = "\n    ".join(locations)
        ips = "    \n".join(f"allow {ip};" for ip in c["allowed_ips"])

        default_server = " default_server" if is_default else ""
        return f"""\
server {{
gzip_proxied any;
client_max_body_size 0;
server_tokens off;
listen 0.0.0.0:{v["port"]}{" ssl http2" if v["proto"] == "https" else ""}{" proxy_protocol" if v.get("proxy_proto_from") else ""}{default_server};
{part}
server_name {" ".join(v["domains"])};{ssl_info}
{ips}
deny all;
{locs}
}}
"""

    def _gen_http_defaults(self) -> str:
        return """\
ssl_session_timeout 10m;
ssl_session_cache shared:SSL:10m;

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

"""

    def _gen_file_content_l7(self, vhosts) -> str:
        backends = "\n".join(b for b in self._gen_backends(proto_lvl="l7"))
        vhosts = "\n".join(v for v in vhosts)

        return f"""\
{self._gen_http_defaults()}

{backends}

{vhosts}
"""

    # We use tmp dir existence as a fact to unfinished download, so clean insides only
    def _clean_dir_insides(self, path):
        tmpdir = os.path.join(DOWNLOAD_DIR_TMP, path)
        for filename in os.listdir(tmpdir):
            file_path = os.path.join(tmpdir, filename)
            if os.path.isdir(file_path) and not os.path.islink(file_path):
                shutil.rmtree(file_path)
            else:
                os.unlink(file_path)

    def _clean_path(self, path):
        self._purge_path_only(path)
        self._download_dirs.pop(path, None)
        return True

    def _purge_path_only(self, dir_name):
        LOG.info("_purge_dir: %s", dir_name)
        tmpdir = os.path.join(DOWNLOAD_DIR_TMP, dir_name)
        tgtdir = os.path.join(DOWNLOAD_DIR, dir_name)
        if os.path.exists(tmpdir):
            shutil.rmtree(tmpdir)
        if os.path.exists(tgtdir):
            shutil.rmtree(tgtdir)

    def _clean_external_symlinks(self, root_dir):
        # Basic safety from path traversal via symlinks
        root = pathlib.Path(root_dir).resolve()
        for path in root.rglob("*"):
            if path.is_symlink():
                try:
                    target_path = path.resolve()
                    if not target_path.is_relative_to(root):
                        path.unlink()
                except (OSError, RuntimeError):
                    # Handles broken links or infinite loops
                    path.unlink()

    def _download_url(self, path, url):
        LOG.info("_download_url start: %s %s", path, url)
        key = "--zstd"
        if url.endswith(".gz"):
            key = "-z"
        tmpdir = os.path.join(DOWNLOAD_DIR_TMP, path)
        if os.path.exists(tmpdir):
            self._clean_dir_insides(tmpdir)
        else:
            os.makedirs(tmpdir, mode=0o775, exist_ok=True)
        shutil.chown(tmpdir, user="www-data", group="www-data")
        tgtdir = os.path.join(DOWNLOAD_DIR, path)
        wgetps = subprocess.Popen(
            ("wget", "-O", "-", "--timeout=60", url),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tarps = subprocess.Popen(
            (
                "sudo",
                "-u",
                "www-data",
                "tar",
                key,
                "--no-same-owner",
                "-xvf",
                "-",
                "-C",
                tmpdir,
            ),
            stdin=wgetps.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        tar_rc = tarps.wait()
        wget_rc = wgetps.wait()
        if tar_rc != 0 or wget_rc != 0:
            wgetps.kill()
            tarps.kill()
            return f"wget({wget_rc}):{wgetps.stderr.read()}\ntar({tar_rc}):{tarps.stderr.read()}"
        self._clean_external_symlinks(tmpdir)
        if os.path.exists(tgtdir):
            utils.swap_dirs(tmpdir, tgtdir)
            shutil.rmtree(tmpdir)
        else:
            os.rename(tmpdir, tgtdir)
        # Update url in persistent storage
        self._download_dirs[path] = url
        LOG.info("_download_url finish: %s %s", path, url)
        return True

    def _get_target_paths(self):
        # Aggregated: the orphan-dir cleanup in _actualize_downloaded_dirs
        # removes anything outside this set, so it must span every LB
        # sharing the dataplane, not just self.
        target_paths = {}
        for v in self._agg_vhosts():
            if len(v["routes"]) == 0:
                continue
            if not v["proto"].startswith("http"):
                continue
            for r in v["routes"].values():
                c = r["cond"]
                for a in c["actions"]:
                    if a["kind"] != "local_dir_download":
                        continue
                    target_paths[
                        str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"{v['uuid']}{c['kind']}{c['value']}",
                            )
                        )
                    ] = a["url"]
        return target_paths

    def _actualize_downloaded_dirs(self):
        target_paths = self._get_target_paths()
        target_paths_set = set(target_paths.keys())
        # Download new dirs/Update already existing with new link
        for p, u in target_paths.items():
            # Already in progress, skip
            if p in self._download_dirs_futures:
                continue
            if self._download_dirs.get(p) != u:
                # Url changed, get new one
                self._download_dirs_futures[p] = TPOOL.submit(self._download_url, p, u)
            elif os.path.isdir(os.path.join(DOWNLOAD_DIR, p)) and not os.path.isdir(
                os.path.join(DOWNLOAD_DIR_TMP, p)
            ):
                # already exists and "ok", just track it
                self._download_dirs[p] = u
            else:
                self._download_dirs_futures[p] = TPOOL.submit(self._download_url, p, u)
        # Clean orphan dirs
        actual_ondisk_dirs = set(
            entry.name for entry in os.scandir(DOWNLOAD_DIR) if entry.is_dir()
        )
        for d in actual_ondisk_dirs - target_paths_set:
            self._download_dirs_futures[d] = TPOOL.submit(self._clean_path, d)

    def _validate_downloaded_dirs(self):
        for p, u in self._get_target_paths().items():
            # If TMP dir exists - it's a signal that we didn't finish our job
            #  before (for ex. when url was updated)
            if (
                not os.path.isdir(os.path.join(DOWNLOAD_DIR, p))
                or os.path.isdir(os.path.join(DOWNLOAD_DIR_TMP, p))
                or self._download_dirs.get(p, None) != u
            ):
                self._download_dirs.pop(p, None)
                return False
        return True

    def _reload_or_restart_nginx(self):
        try:
            subprocess.check_call(["systemctl", "reload", "nginx"])
        except subprocess.CalledProcessError:
            subprocess.check_call(["systemctl", "restart", "nginx"])

    def dump_to_dp(self) -> None:
        vhosts_l4, vhosts_l7, ext_sources = self._gen_vhosts()
        with open(NGINX_L4_CONFIG_FILE, "w") as f:
            f.write(self._gen_file_content_l4(vhosts_l4))

        with open(NGINX_L7_CONFIG_FILE, "w") as f:
            f.write(self._gen_file_content_l7(vhosts_l7))

        # Aggregated: the not-in-use cleanup below removes any cert file
        # outside actual_keys, so certs of every LB sharing the dataplane
        # must be written/kept, not just self's.
        actual_keys = set()
        for v in self._agg_vhosts():
            if v["proto"] != "https":
                continue
            crt_name = f"{NGINX_SSL_DIR}{v['uuid']}_exordos.crt"
            with open(crt_name, "w", opener=secure_opener) as f:
                f.write(v["cert"]["crt"])
            shutil.chown(crt_name, user=NGINX_USER, group=NGINX_GROUP)
            key_name = f"{NGINX_SSL_DIR}{v['uuid']}_exordos.key"
            actual_keys.add(key_name)
            with open(key_name, "w", opener=secure_opener) as f:
                f.write(v["cert"]["key"])
            shutil.chown(key_name, user=NGINX_USER, group=NGINX_GROUP)

        # Clean up SSL certificate files that are not in use
        for filepath in glob.glob(f"{NGINX_SSL_DIR}*_exordos.key"):
            if filepath not in actual_keys:
                try:
                    os.remove(f"{os.path.splitext(filepath)[0]}.crt")
                    os.remove(filepath)
                except OSError:
                    pass

        self._actualize_downloaded_dirs()

        self._reload_or_restart_nginx()

        # external sources
        for n, e in ext_sources.items():
            with open(
                os.path.join(TUNNEL_SCRIPT_PATH, f"{n}.sh"),
                "w",
                opener=executable_opener,
            ) as f:
                f.write(
                    TUNNEL_SCRIPT_TEMPLATE.format(
                        name=n,
                        mode=e["lproto"],
                        ssh_user=e["user"],
                        ssh_port=e["port"],
                        ssh_host=e["host"],
                        port=e["lport"],
                        remote_command=(
                            REMOTE_SCRIPT.format(
                                port=e["lport"],
                                mode=e["lproto"],
                                proto=(e["lproto"] if e["lproto"] == "udp" else ""),
                            )
                            if e["proxy_proto_from"]
                            else ""
                        ),
                    )
                )
            with open(
                os.path.join(TUNNEL_SCRIPT_PATH, f"{n}.key"),
                "w",
                opener=secure_opener,
            ) as f:
                f.write(e["private_key"])
            subprocess.check_call(
                ["systemctl", "enable", "--now", f"exordos-tunnel@{n}"]
            )
        self._remove_external_sources(ext_sources)

    def _validate_file(self, path, expected_content):
        try:
            with open(path, "r") as f:
                if expected_content != f.read():
                    raise driver_exc.InvalidDataPlaneObjectError(
                        obj={"uuid": str(self.uuid)}
                    )
        except FileNotFoundError:
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})

    def restore_from_dp(self) -> None:
        self.status = ic.InstanceStatus.IN_PROGRESS.value
        try:
            subprocess.check_output(["systemctl", "is-active", "nginx"])
        except subprocess.CalledProcessError:
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})

        vhosts_l4, vhosts_l7, ext_sources = self._gen_vhosts()
        # Force file validation
        self._validate_file(NGINX_L4_CONFIG_FILE, self._gen_file_content_l4(vhosts_l4))
        self._validate_file(NGINX_L7_CONFIG_FILE, self._gen_file_content_l7(vhosts_l7))
        for v in self.vhosts:
            if v["proto"] == "https":
                self._validate_file(
                    f"{NGINX_SSL_DIR}{v['uuid']}_exordos.crt", v["cert"]["crt"]
                )
                self._validate_file(
                    f"{NGINX_SSL_DIR}{v['uuid']}_exordos.key", v["cert"]["key"]
                )
        for path, future in self._download_dirs_futures.copy().items():
            try:
                if (ret := future.result(timeout=0)) is not True:
                    LOG.error(
                        "Future for %s failed with errors:\n%s\n",
                        self._download_dirs[path],
                        ret,
                    )
                    self.status = ic.InstanceStatus.ERROR.value
                    self._download_dirs_futures.pop(path, None)
                    raise driver_exc.InvalidDataPlaneObjectError(
                        obj={"uuid": str(self.uuid)}
                    )
            except TimeoutError:
                # Future is not finished yet
                continue
            except Exception as e:
                # Future got exception
                LOG.error(
                    "Future for %s failed with exception:\n%s",
                    self._download_dirs[path],
                    e,
                )
                self.status = ic.InstanceStatus.ERROR.value
                self._download_dirs_futures.pop(path, None)
                raise driver_exc.InvalidDataPlaneObjectError(
                    obj={"uuid": str(self.uuid)}
                )
            self._download_dirs_futures.pop(path, None)
        if not self._validate_downloaded_dirs():
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})

        for n, e in ext_sources.items():
            self._validate_file(
                os.path.join(TUNNEL_SCRIPT_PATH, f"{n}.sh"),
                TUNNEL_SCRIPT_TEMPLATE.format(
                    name=n,
                    mode=e["lproto"],
                    ssh_user=e["user"],
                    ssh_port=e["port"],
                    ssh_host=e["host"],
                    port=e["lport"],
                    remote_command=(
                        REMOTE_SCRIPT.format(
                            port=e["lport"],
                            mode=e["lproto"],
                            proto=e["lproto"] if e["lproto"] == "udp" else "",
                        )
                        if e["proxy_proto_from"]
                        else ""
                    ),
                ),
            )
            self._validate_file(
                os.path.join(TUNNEL_SCRIPT_PATH, f"{n}.key"), e["private_key"]
            )
            try:
                subprocess.check_output(
                    ["systemctl", "is-active", f"exordos-tunnel@{n}"]
                )
            except subprocess.CalledProcessError:
                raise driver_exc.InvalidDataPlaneObjectError(
                    obj={"uuid": str(self.uuid)}
                )

        self.status = ic.InstanceStatus.ACTIVE.value

    def _remove_file(self, path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _remove_external_sources(self, existing_sources=None):
        existing_sources = existing_sources or {}
        snames_on_disk = {
            os.path.splitext(f)[0] for f in os.listdir(TUNNEL_SCRIPT_PATH)
        }
        snames_to_remove = snames_on_disk - set(existing_sources.keys())

        for sname in snames_to_remove:
            try:
                subprocess.check_call(
                    [
                        "systemctl",
                        "disable",
                        "--now",
                        f"exordos-tunnel@{sname}",
                    ]
                )
            except subprocess.CalledProcessError:
                pass

            for ext in (".sh", ".key"):
                try:
                    os.remove(os.path.join(TUNNEL_SCRIPT_PATH, f"{sname}{ext}"))
                except OSError:
                    pass

    def delete_from_dp(self) -> None:
        # The config files are a shared dataplane: re-render it without this
        # LB instead of removing them, so sibling LBs keep their vhosts.
        # With nothing contributed by self, dump_to_dp's aggregated cleanups
        # also drop self's certs, downloaded dirs and tunnels (our meta view
        # is deleted from storage right after this returns).
        self.vhosts = []
        self.backend_pools = {}
        self.dump_to_dp()

    def update_on_dp(self) -> None:
        self.dump_to_dp()


class LBCapabilityDriver(meta.MetaFileStorageAgentDriver):
    META_PATH = os.path.join(constants.WORK_DIR, "lb_meta.json")

    __model_map__ = {LB_TARGET_KIND: LB}

    def __init__(self, *args, **kwargs) -> None:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        os.makedirs(DOWNLOAD_DIR_TMP, exist_ok=True)
        os.makedirs(TUNNEL_SCRIPT_PATH, exist_ok=True)
        with open(SYSTEMD_TMPL_PATH, mode="w") as f:
            f.write(SYSTEMD_TEMPLATE)
        subprocess.check_call(["systemctl", "daemon-reload"])
        super().__init__(*args, meta_file=self.META_PATH, **kwargs)


class LBAgentCapabilityDriver(LBCapabilityDriver):
    __model_map__ = {LB_AGENT_TARGET_KIND: LB}
