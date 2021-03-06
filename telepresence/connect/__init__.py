# Copyright 2018 Datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import re
from typing import Tuple

from telepresence.runner.background import launch_local_server
from telepresence.connect.expose import expose_local_services
from telepresence.connect.ssh import SSH
from telepresence.startup import MAC_LOOPBACK_IP
from telepresence.proxy.remote import RemoteInfo
from telepresence.runner import Runner
from telepresence.utilities import find_free_port


def connect(
    runner: Runner, remote_info: RemoteInfo, cmdline_args: argparse.Namespace
) -> Tuple[int, SSH]:
    """
    Start all the processes that handle remote proxying.

    Return (local port of SOCKS proxying tunnel, SSH instance).
    """
    span = runner.span()
    # Keep local copy of pod logs, for debugging purposes:
    runner.launch(
        "kubectl logs",
        runner.kubectl(
            "logs", "-f", remote_info.pod_name, "--container",
            remote_info.container_name
        ),
        bufsize=0,
    )

    ssh = SSH(runner, find_free_port())

    # forward remote port to here, by tunneling via remote SSH server:
    runner.launch(
        "kubectl port-forward",
        runner.kubectl(
            "port-forward", remote_info.pod_name, "{}:8022".format(ssh.port)
        )
    )
    if cmdline_args.method == "container":
        # kubectl port-forward currently only listens on loopback. So we
        # portforward from the docker0 interface on Linux, and the lo0 alias we
        # added on OS X, to loopback (until we can use kubectl port-forward
        # option to listen on docker0 -
        # https://github.com/kubernetes/kubernetes/pull/46517, or all our users
        # have latest version of Docker for Mac, which has nicer solution -
        # https://github.com/datawire/telepresence/issues/224).
        if runner.platform == "linux":

            # If ip addr is available use it if not fall back to ifconfig.
            missing = runner.depend(["ip", "ifconfig"])
            if "ip" not in missing:
                docker_interfaces = re.findall(
                    r"(\d+\.\d+\.\d+\.\d+)",
                    runner.get_output(["ip", "addr", "show", "dev", "docker0"])
                )
            elif "ifconfig" not in missing:
                docker_interfaces = re.findall(
                    r"(\d+\.\d+\.\d+\.\d+)",
                    runner.get_output(["ifconfig", "docker0"])
                )
            else:
                raise runner.fail(
                    """At least one of "ip addr" or "ifconfig" must be """ +
                    "available to retrieve Docker interface info."
                )

            if len(docker_interfaces) == 0:
                raise runner.fail("No interface for docker found")

            docker_interface = docker_interfaces[0]

        else:
            # The way to get routing from container to host is via an alias on
            # lo0 (https://docs.docker.com/docker-for-mac/networking/). We use
            # an IP range that is assigned for testing network devices and
            # therefore shouldn't conflict with real IPs or local private
            # networks (https://tools.ietf.org/html/rfc6890).
            runner.require(
                ["ifconfig"],
                "Needed to manage networking with the container method.",
            )
            runner.require_sudo()
            runner.check_call([
                "sudo", "ifconfig", "lo0", "alias", MAC_LOOPBACK_IP
            ])
            runner.add_cleanup(
                "Mac Loopback", runner.check_call,
                ["sudo", "ifconfig", "lo0", "-alias", MAC_LOOPBACK_IP]
            )
            docker_interface = MAC_LOOPBACK_IP

        runner.require(
            ["socat"],
            "Needed to manage networking with the container method.",
        )
        runner.launch(
            "socat for docker", [
                "socat", "TCP4-LISTEN:{},bind={},reuseaddr,fork".format(
                    ssh.port,
                    docker_interface,
                ), "TCP4:127.0.0.1:{}".format(ssh.port)
            ]
        )

    ssh.wait()

    # In Docker mode this happens inside the local Docker container:
    if cmdline_args.method != "container":
        expose_local_services(
            runner,
            ssh,
            cmdline_args.expose.local_to_remote(),
        )

    # Start tunnels for the SOCKS proxy (local -> remote)
    # and the local server for the proxy to poll (remote -> local).
    socks_port = find_free_port()
    local_server_port = find_free_port()
    runner.track_background(
        launch_local_server(local_server_port, runner.output)
    )
    forward_args = [
        "-L127.0.0.1:{}:127.0.0.1:9050".format(socks_port),
        "-R9055:127.0.0.1:{}".format(local_server_port)
    ]
    runner.launch(
        "SSH port forward (socks and proxy poll)",
        ssh.bg_command(forward_args)
    )

    span.end()
    return socks_port, ssh
