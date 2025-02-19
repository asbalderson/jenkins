import asyncio
import functools
import ipaddress
import json
import os
import subprocess
import time
import traceback

from contextlib import contextmanager
from juju.controller import Controller
from juju.errors import JujuError
from subprocess import check_output, check_call
from cilib import log
import click


# note: we can't upgrade to focal until after it's released
SERIES_ORDER = [
    "xenial",
    "bionic",
]


def tracefunc(frame, event, arg):
    if event != "call":
        return

    package_name = __name__.split(".")[0]

    if package_name in str(frame):
        co = frame.f_code
        func_name = co.co_name
        if func_name == "write":
            # Ignore write() calls from print statements
            return
        func_line_no = frame.f_lineno
        func_filename = co.co_filename
        if "conftest" in func_filename:
            return
        log.debug(f"Call to {func_name} on line {func_line_no}:{func_filename}")
        for i in range(frame.f_code.co_argcount):
            name = frame.f_code.co_varnames[i]
            log.debug(f"    Argument {name} is {frame.f_locals[name]}")
    return


@contextmanager
def timeout_for_current_task(timeout):
    """Create a context with a timeout.

    If the context body does not finish within the time limit, then the current
    asyncio task will be cancelled, and an asyncio.TimeoutError will be raised.
    """
    loop = asyncio.get_event_loop()
    task = asyncio.Task.current_task()
    handle = loop.call_later(timeout, task.cancel)
    try:
        yield
    except asyncio.CancelledError:
        raise asyncio.TimeoutError("Timed out after %f seconds" % timeout)
    finally:
        handle.cancel()


def apply_profile(model_name):
    """
    Apply the lxd profile
    Args:
        model_name: the model name

    Returns: lxc profile edit output

    """
    here = os.path.dirname(os.path.abspath(__file__))
    profile = os.path.join(here, "templates", "lxd-profile.yaml")
    lxc_aa_profile = "lxc.aa_profile"
    cmd = "lxc --version"
    version = check_output(["bash", "-c", cmd])
    if version.decode("utf-8").startswith("3."):
        lxc_aa_profile = "lxc.apparmor.profile"
    cmd = (
        'sed -e "s/##MODEL##/{0}/" -e "s/##AA_PROFILE##/{1}/" "{2}" | '
        'sudo lxc profile edit "juju-{0}"'.format(model_name, lxc_aa_profile, profile)
    )
    return check_output(["bash", "-c", cmd])


def asyncify(f):
    """ Convert a blocking function into a coroutine """

    async def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        partial = functools.partial(f, *args, **kwargs)
        return await loop.run_in_executor(None, partial)

    return wrapper


async def upgrade_charms(model, channel, tools):

    for app in model.applications.values():
        try:
            await app.upgrade_charm(channel=channel)
        except JujuError as e:
            if "already running charm" not in str(e):
                raise
    await tools.juju_wait()


async def upgrade_snaps(model, channel, tools):
    for app_name, blocking in {
        "kubernetes-master": True,
        "kubernetes-worker": True,
        "kubernetes-e2e": False,
    }.items():
        app = model.applications.get(app_name)
        # missing applications are simply not upgraded
        if not app:
            continue

        config = await app.get_config()
        # If there is no change in the snaps skipping the upgrade
        if channel == config["channel"]["value"]:
            continue

        await app.set_config({"channel": channel})

        if blocking:
            for unit in app.units:
                # wait for blocked status
                deadline = time.time() + 180
                while time.time() < deadline:
                    if (
                        unit.workload_status == "blocked"
                        and unit.workload_status_message
                        == "Needs manual upgrade, run the upgrade action"
                    ):
                        break
                    await asyncio.sleep(3)
                else:
                    raise asyncio.TimeoutError(
                        "Unable to find blocked status on unit {0} - {1} {2}".format(
                            unit.name, unit.workload_status, unit.agent_status
                        )
                    )

                # run upgrade action
                action = await unit.run_action("upgrade")
                await action.wait()
                assert action.status == "completed"

    await tools.juju_wait()


async def is_localhost(controller_name):
    controller = Controller()
    await controller.connect(controller_name)
    cloud = await controller.get_cloud()
    await controller.disconnect()
    return cloud == "localhost"


async def scp_from(unit, remote_path, local_path, controller_name, connection_name):
    cmd = "juju scp -m {} {}:{} {}".format(
        connection_name, unit.name, remote_path, local_path
    )
    await asyncify(subprocess.check_call)(cmd.split())


async def scp_to(local_path, unit, remote_path, controller_name, connection_name):
    cmd = "juju scp -m {} {} {}:{}".format(
        connection_name, local_path, unit.name, remote_path
    )
    await asyncify(subprocess.check_call)(cmd.split())


async def retry_async_with_timeout(
    func,
    args,
    timeout_insec=600,
    timeout_msg="Timeout exceeded",
    retry_interval_insec=5,
):
    """
    Retry a function until a timeout is exceeded. Function should
    return either True or Flase
    Args:
        func: The function to be retried
        args: Agruments of the function
        timeout_insec: What the timeout is (in seconds)
        timeout_msg: What to show in the timeout exception thrown
        retry_interval_insec: The interval between two consecutive executions

    """
    deadline = time.time() + timeout_insec
    while time.time() < deadline:
        if await func(*args):
            break
        await asyncio.sleep(retry_interval_insec)
    else:
        raise asyncio.TimeoutError(timeout_msg)


def arch():
    """Return the package architecture as a string."""
    architecture = check_output(["dpkg", "--print-architecture"]).rstrip()
    architecture = architecture.decode("utf-8")
    return architecture


async def disable_source_dest_check(model_name):
    path = os.path.dirname(__file__) + "/tigera_aws.py"
    env = os.environ.copy()
    env["JUJU_MODEL"] = model_name
    cmd = [path, "disable-source-dest-check"]
    await asyncify(check_call)(cmd, env=env)


async def verify_deleted(unit, entity_type, name, extra_args=""):
    cmd = "/snap/bin/kubectl --kubeconfig /root/.kube/config {} --output json get {}".format(
        extra_args, entity_type
    )
    output = await unit.run(cmd)
    if "error" in output.results.get("Stdout", ""):
        # error resource type not found most likely. This can happen when the
        # api server is restarting. As such, don't assume this means we've
        # finished the deletion
        return False
    try:
        out_list = json.loads(output.results.get("Stdout", ""))
    except json.JSONDecodeError:
        click.echo(traceback.format_exc())
        click.echo("WARNING: Expected json, got non-json output:")
        click.echo(output.results.get("Stdout", ""))
        return False
    for item in out_list["items"]:
        if item["metadata"]["name"] == name:
            return False
    return True


async def find_entities(unit, entity_type, name_list, extra_args=""):
    cmd = "/snap/bin/kubectl --kubeconfig /root/.kube/config {} --output json get {}"
    cmd = cmd.format(extra_args, entity_type)
    output = await unit.run(cmd)
    if output.results["Code"] != "0":
        # error resource type not found most likely. This can happen when the
        # api server is restarting. As such, don't assume this means ready.
        return False
    out_list = json.loads(output.results.get("Stdout", ""))
    matches = []
    for name in name_list:
        # find all entries that match this
        [matches.append(n) for n in out_list["items"] if name in n["metadata"]["name"]]
    return matches


async def verify_ready(unit, entity_type, name_list, extra_args=""):
    """
    note that name_list is a list of entities(pods, services, etc) being searched
    and that partial matches work. If you have a pod with random characters at
    the end due to being in a deploymnet, you can add just the name of the
    deployment and it will still match
    """

    matches = await find_entities(unit, entity_type, name_list, extra_args)
    if not matches:
        return False

    # now verify they are ALL ready, it isn't cool if just one is ready now
    ready = [
        n
        for n in matches
        if n["kind"] == "DaemonSet"
        or n["kind"] == "Service"
        or n["status"]["phase"] == "Running"
        or n["status"]["phase"] == "Active"
    ]
    if len(ready) != len(matches):
        return False

    # made it here then all the matches are ready
    return True


async def verify_completed(unit, entity_type, name_list, extra_args=""):
    """
    note that name_list is a list of entities(pods, services, etc) being searched
    and that partial matches work. If you have a pod with random characters at
    the end due to being in a deploymnet, you can add just the name of the
    deployment and it will still match
    """
    matches = await find_entities(unit, entity_type, name_list, extra_args)
    if not matches or len(matches) == 0:
        return False

    # now verify they are ALL completed - note that is in the phase 'Succeeded'
    return all([n["status"]["phase"] == "Succeeded" for n in matches])


async def log_snap_versions(model, prefix="before"):
    click.echo("Logging snap versions")
    for unit in model.units.values():
        if unit.dead:
            continue
        action = await unit.run("snap list")
        snap_versions = (
            action.data["results"].get("Stdout", "").strip() or "No snaps found"
        )
        click.echo(f"{prefix} {unit.name} {snap_versions}")


async def validate_storage_class(model, sc_name, test_name):
    master = model.applications["kubernetes-master"].units[0]
    # write a string to a file on the pvc
    pod_definition = """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {0}-pvc
  annotations:
   volume.beta.kubernetes.io/storage-class: {0}
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
---
kind: Pod
apiVersion: v1
metadata:
  name: {0}-write-test
spec:
  volumes:
  - name: shared-data
    persistentVolumeClaim:
      claimName: {0}-pvc
      readOnly: false
  containers:
    - name: {0}-write-test
      image: rocks.canonical.com/cdk/ubuntu:focal
      command: ["/bin/bash", "-c", "echo 'JUJU TEST' > /data/juju"]
      volumeMounts:
      - name: shared-data
        mountPath: /data
  restartPolicy: Never
""".format(
        sc_name
    )
    cmd = "/snap/bin/kubectl --kubeconfig /root/.kube/config create -f - << EOF{}EOF".format(
        pod_definition
    )
    click.echo("{}: {} writing test".format(test_name, sc_name))
    output = await master.run(cmd)
    assert output.status == "completed"

    # wait for completion
    await retry_async_with_timeout(
        verify_completed,
        (master, "po", ["{}-write-test".format(sc_name)]),
        timeout_msg="Unable to create write pod for {} test".format(test_name),
    )

    # read that string from pvc
    pod_definition = """
kind: Pod
apiVersion: v1
metadata:
  name: {0}-read-test
spec:
  volumes:
  - name: shared-data
    persistentVolumeClaim:
      claimName: {0}-pvc
      readOnly: false
  containers:
    - name: {0}-read-test
      image: rocks.canonical.com/cdk/ubuntu:focal
      command: ["/bin/bash", "-c", "cat /data/juju"]
      volumeMounts:
      - name: shared-data
        mountPath: /data
  restartPolicy: Never
""".format(
        sc_name
    )
    cmd = "/snap/bin/kubectl --kubeconfig /root/.kube/config create -f - << EOF{}EOF".format(
        pod_definition
    )
    click.echo("{}: {} reading test".format(test_name, sc_name))
    output = await master.run(cmd)
    assert output.status == "completed"

    # wait for completion
    await retry_async_with_timeout(
        verify_completed,
        (master, "po", ["{}-read-test".format(sc_name)]),
        timeout_msg="Unable to create write" " pod for ceph test",
    )

    output = await master.run(
        "/snap/bin/kubectl --kubeconfig /root/.kube/config logs {}-read-test".format(
            sc_name
        )
    )
    assert output.status == "completed"
    click.echo("output = {}".format(output.data["results"].get("Stdout", "")))
    assert "JUJU TEST" in output.data["results"].get("Stdout", "")

    click.echo("{}: {} cleanup".format(test_name, sc_name))
    pods = "{0}-read-test {0}-write-test".format(sc_name)
    pvcs = "{}-pvc".format(sc_name)
    output = await master.run(
        "/snap/bin/kubectl --kubeconfig /root/.kube/config delete po {}".format(pods)
    )
    assert output.status == "completed"
    output = await master.run(
        "/snap/bin/kubectl --kubeconfig /root/.kube/config delete pvc {}".format(pvcs)
    )
    assert output.status == "completed"

    await retry_async_with_timeout(
        verify_deleted,
        (master, "po", pods),
        timeout_msg="Unable to remove {} test pods".format(test_name),
    )
    await retry_async_with_timeout(
        verify_deleted,
        (master, "pvc", pvcs),
        timeout_msg="Unable to remove {} test pvcs".format(test_name),
    )


def _units(machine):
    return [unit for unit in machine.model.units if unit.machine.id == machine.id]


async def wait_for_status(workload_status, units):
    if not isinstance(units, (list, tuple)):
        units = [units]
    log.info(f'waiting for {workload_status} status on {", ".join(units)}')
    model = units[0].model
    try:
        await model.block_until(
            lambda: all(unit.workload_status == workload_status for unit in units),
            timeout=120,
        )
    except asyncio.TimeoutError as e:
        unmatched_units = [
            f"{unit.name}={unit.workload_status}"
            for unit in units
            if unit.workload_status != workload_status
        ]
        raise AssertionError(
            f'Units with unexpected status: {",".join(unmatched_units)}'
        ) from e


async def prep_series_upgrade(machine, new_series, tools):
    log.info(f"preparing series upgrade for machine {machine.id}")
    await tools.run(
        "juju",
        "upgrade-series",
        "--yes",
        "-m",
        tools.connection,
        machine.id,
        "prepare",
        new_series,
    )
    await wait_for_status("blocked", _units(machine))


async def do_series_upgrade(machine):
    file_name = "/etc/apt/apt.conf.d/50unattended-upgrades"
    option = "--force-confdef"
    log.info(f"doing series upgrade for machine {machine.id}")
    await machine.ssh(
        f"""
        if ! grep -q -- '{option}' {file_name}; then
          echo 'DPkg::options {{ "{option}"; }};' | sudo tee -a {file_name}
        fi
        sudo DEBIAN_FRONTEND=noninteractive do-release-upgrade -f DistUpgradeViewNonInteractive
    """
    )
    log.info(f"rebooting machine {machine.id}")
    try:
        await machine.ssh("sudo reboot && exit")
    except JujuError:
        # We actually expect this to "fail" because the reboot closes the session prematurely.
        pass


async def finish_series_upgrade(machine, tools):
    log.info(f"completing series upgrade for machine {machine.id}")
    await tools.run(
        "juju",
        "upgrade-series",
        "--yes",
        "-m",
        tools.connection,
        machine.id,
        "complete",
    )
    await wait_for_status("active", _units(machine))


class JujuRunError(AssertionError):
    def __init__(self, unit, command, result):
        self.unit = unit
        self.command = command
        self.code = result.code
        self.stdout = result.stdout
        self.stderr = result.stderr
        self.output = result.output
        super().__init__(
            f"`{self.command}` failed on {self.unit.name}:\n{self.stdout}\n{self.stderr}"
        )


class JujuRunResult:
    def __init__(self, action):
        self.status = action.status
        self.code = int(action.results["Code"])
        self.stdout = action.results.get("Stdout", "").strip()
        self.stderr = action.results.get("Stderr", "").strip()
        self.output = self.stderr or self.stdout
        self.success = self.status == "completed" and self.code == 0


async def juju_run(unit, cmd, check=True):
    result = JujuRunResult(await unit.run(cmd))
    if check and not result.success:
        raise JujuRunError(unit, cmd, result)
    return result


async def kubectl(model, cmd, check=True):
    master = model.applications["kubernetes-master"].units[0]
    return await juju_run(
        master, f"/snap/bin/kubectl --kubeconfig /root/.kube/config {cmd}", check
    )


async def vault(unit, cmd, **env):
    env[
        "VAULT_FORMAT"
    ] = "json"  # Can't override this or we won't be able to parse the results
    env.setdefault("VAULT_ADDR", "http://localhost:8200")
    env = " ".join(f"{key}='{value}'" for key, value in env.items())
    result = await juju_run(unit, f"{env} /snap/bin/vault {cmd}")
    return json.loads(result.stdout)


async def vault_status(unit):
    try:
        click.echo(f"Checking Vault status on {unit.name}")
        result = await vault(unit, "status")
    except JujuRunError as e:
        if e.code == 2:
            # This just means Vault is sealed, which is fine.
            result = json.loads(e.stdout)
        else:
            click.echo(f"Vault not running on {unit.name}: {e.output}")
            return None
    click.echo(f"Vault is running on {unit.name}: {result}")
    return result


async def get_ipv6_addr(unit):
    """Return the first globally scoped IPv6 address found on the given unit, or None."""
    output = await unit.run("ip -br a show scope global")
    assert output.status == "completed" and output.results["Code"] == "0"
    for intf in output.results["Stdout"].splitlines():
        if "UP" not in intf:
            continue
        for addr in intf.split("  ")[-1].split():
            addr = ipaddress.ip_interface(addr).ip
            if addr.version == 6:
                return str(addr)
    return None
