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
import sys
from subprocess import CalledProcessError, Popen
from time import time, sleep
from typing import Dict

import os
from shutil import copy

from telepresence.utilities import kill_process
from telepresence.proxy.remote import RemoteInfo
from telepresence.runner import Runner
from telepresence.connect.ssh import SSH
from telepresence.vpn import connect_sshuttle


def sip_workaround(
    runner: Runner, existing_paths: str, unsupported_tools_path: str
) -> str:
    """
    Workaround System Integrity Protection.

    Newer OS X don't allow injecting libraries into binaries in /bin, /sbin and
    /usr. We therefore make a copy of them and modify $PATH to point at their
    new location. It's only ~100MB so this should be pretty fast!

    :param existing_paths: Current $PATH.
    :param unsupported_tools_path: Path where we have custom versions of ping
        etc. Needs to be first in list so that we override system versions.
    """
    protected = {"/bin", "/sbin", "/usr/sbin", "/usr/bin"}
    # Remove protected paths from $PATH:
    paths = [p for p in existing_paths.split(":") if p not in protected]
    # Add temp dir
    bin_dir = str(runner.make_temp("sip_bin"))
    paths.insert(0, bin_dir)
    for directory in protected:
        for file in os.listdir(directory):
            try:
                copy(os.path.join(directory, file), bin_dir)
            except IOError:
                continue
            os.chmod(os.path.join(bin_dir, file), 0o775)
    paths = [unsupported_tools_path] + paths
    # Return new $PATH
    return ":".join(paths)


NICE_FAILURE = """\
#!/bin/sh
echo {} is not supported under Telepresence.
echo See \
https://telepresence.io/reference/limitations.html \
for details.
exit 55
"""


def get_unsupported_tools(runner: Runner, dns_supported: bool) -> str:
    """
    Create replacement command-line tools that just error out, in a nice way.

    Returns path to directory where overriden tools are stored.
    """
    commands = ["ping", "traceroute"]
    if not dns_supported:
        commands += ["nslookup", "dig", "host"]
    unsupported_bin = str(runner.make_temp("unsup_bin"))
    for command in commands:
        path = unsupported_bin + "/" + command
        with open(path, "w") as f:
            f.write(NICE_FAILURE.format(command))
        os.chmod(path, 0o755)
    return unsupported_bin


TORSOCKS_CONFIG = """
# Allow process to listen on ports:
AllowInbound 1
# Allow process to connect to localhost:
AllowOutboundLocalhost 1
# Connect to custom port for SOCKS server:
TorPort {}
"""


def setup_torsocks(runner, env, socks_port, unsupported_tools_path):
    """Setup environment variables to make torsocks work correctly."""
    # Create custom torsocks.conf, since some options we want (in particular,
    # port) aren't accessible via env variables in older versions of torconf:
    with open(str(runner.temp / "tel_torsocks.conf"), "w") as tor_conffile:
        tor_conffile.write(TORSOCKS_CONFIG.format(socks_port))
    env["TORSOCKS_CONF_FILE"] = tor_conffile.name
    if runner.output.logfile is not sys.stdout:
        env["TORSOCKS_LOG_FILE_PATH"] = runner.output.logfile.name
    if runner.platform == "darwin":
        env["PATH"] = sip_workaround(
            runner, env["PATH"], unsupported_tools_path
        )
    # Try to ensure we're actually proxying network, by forcing DNS resolution
    # via torsocks:
    start = time()
    while time() - start < 10:
        try:
            runner.check_call([
                "torsocks", "python3", "-c",
                "import socket; socket.socket().connect(('google.com', 80))"
            ],
                              env=env)
        except CalledProcessError:
            sleep(0.1)
        else:
            return
    raise RuntimeError("SOCKS network proxying failed to start...")


def run_local_command(
    runner: Runner,
    remote_info: RemoteInfo,
    args: argparse.Namespace,
    env_overrides: Dict[str, str],
    socks_port: int,
    ssh: SSH,
) -> Popen:
    """--run-shell/--run support, run command locally."""
    env = os.environ.copy()
    env.update(env_overrides)

    # Don't use runner.popen() since we want to give program access to current
    # stdout and stderr if it wants it.
    env["PROMPT_COMMAND"
        ] = ('PS1="@{}|$PS1";unset PROMPT_COMMAND'.format(args.context))

    # Inject replacements for unsupported tools like ping:
    unsupported_tools_path = get_unsupported_tools(
        runner, args.method != "inject-tcp"
    )
    env["PATH"] = unsupported_tools_path + ":" + env["PATH"]

    # Make sure we use "bash", no "/bin/bash", so we get the copied version on
    # OS X:
    if args.run is None:
        # We skip .bashrc since it might e.g. have kubectl running to get bash
        # autocomplete, and Go programs don't like DYLD on macOS at least (see
        # https://github.com/datawire/telepresence/issues/125).
        command = ["bash", "--norc"]
    else:
        command = args.run
    if args.method == "inject-tcp":
        setup_torsocks(runner, env, socks_port, unsupported_tools_path)
        p = Popen(["torsocks"] + command, env=env)
    elif args.method == "vpn-tcp":
        connect_sshuttle(runner, remote_info, args, env, ssh)
        p = Popen(command, env=env)

    def terminate_if_alive():
        runner.write("Shutting down local process...\n")
        if p.poll() is None:
            runner.write("Killing local process...\n")
            kill_process(p)

    runner.add_cleanup("Terminate local process", terminate_if_alive)
    return p
