"""
Interface for building and publishing snaps

"""

import sys
import click
import sh
import os
import glob
import re
import yaml
import operator
from lib import snapapi
from pathlib import Path


def _set_snap_alias(build_path, alias):
    click.echo(f"Setting new snap alias: {alias}")
    if build_path.exists():
        snapcraft_yml = yaml.load(build_path.read_text())
        if snapcraft_yml["name"] != alias:
            snapcraft_yml["name"] = alias
            build_path.write_text(
                yaml.dump(snapcraft_yml, default_flow_style=False, indent=2)
            )


@click.group()
def cli():
    pass


@cli.command()
@click.option("--snap", required=True, multiple=True, help="Snaps to build")
@click.option(
    "--build-path", required=True, default="release/snap", help="Path of snap builds"
)
@click.option(
    "--version", required=True, default="1.12.9", help="Version of k8s to build"
)
@click.option(
    "--arch", required=True, default="amd64", help="Architecture to build against"
)
@click.option("--dry-run", is_flag=True)
def build(snap, build_path, version, arch, match_re, rename_re, dry_run):
    """ Build snaps

    Usage:

    snaps.py build --snap kubectl --snap kube-proxy --version 1.10.3 --arch amd64 --match-re '(?=\S*[-]*)([a-zA-Z-]+)(.*)' --rename-re '\1-eks'

    Passing --rename-re and --match-re allows you to manipulate the resulting
    snap file, for example, the above renames kube-proxy_1.10.3_amd64.snap to
    kube-proxy-eks_1.10.3_amd64.snap
    """
    if not version.startswith("v"):
        version = f"v{version}"
    env = os.environ.copy()
    env["KUBE_VERSION"] = version
    env["KUBE_ARCH"] = arch
    sh.git.clone(
        "https://github.com/juju-solutions/release.git",
        build_path,
        branch="rye/snaps",
        depth="1",
    )
    build_path = Path(build_path) / "snap"
    snap_alias = None

    for _snap in snap:
        if match_re and rename_re:
            snap_alias = f"{_snap}-eks"

        if snap_alias:
            snapcraft_fn = build_path / f"{_snap}.yaml"
            _set_snap_alias(snapcraft_fn, snap_alias)

        if dry_run:
            click.echo("dry-run only:")
            click.echo(
                f"  > cd release/snap && bash build-scripts/docker-build {_snap}"
            )
        else:
            for line in sh.bash(
                "build-scripts/docker-build",
                _snap,
                _env=env,
                _cwd=str(build_path),
                _bg_exc=False,
                _iter=True,
            ):
                click.echo(line.strip())


@cli.command()
@click.option(
    "--result-dir",
    required=True,
    default="release/snap/snap/build",
    help="Path of resulting snap builds",
)
@click.option("--version", required=True, default="1.12.9", help="k8s Version")
@click.option("--dry-run", is_flag=True)
def push(result_dir, version, dry_run):
    """ Promote to a snapstore channel/track
    """
    for fname in glob.glob(f"{result_dir}/*.snap"):
        try:
            click.echo(f"Running: snapcraft push {fname}")
            if dry_run:
                click.echo("dry-run only:")
                click.echo(f"  > snapcraft push {fname}")
            else:
                for line in sh.snapcraft.push(
                    fname,
                    "--release",
                    f"{version}/edge,{version}/beta,{version}/candidate,{version}/stable",
                    _iter=True,
                    _bg_exc=False,
                ):
                    click.echo(line.strip())
        except sh.ErrorReturnCode as e:
            click.echo("Failed to upload to snap store")
            click.echo(e.stdout)
            click.echo(e.stderr)


if __name__ == "__main__":
    cli()
