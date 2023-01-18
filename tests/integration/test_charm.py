#!/usr/bin/env python3
# Copyright 2022 Adam Dyess
# See LICENSE file for licensing details.

import asyncio
import logging
import shlex
import urllib.request
from pathlib import Path

import pytest
import yaml
from lightkube.codecs import from_dict
from lightkube.generic_resource import get_generic_resource
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


@pytest.fixture()
async def vsphere_overlay(ops_test: OpsTest) -> Path:
    bundles_dst_dir = self.tmp_path / "bundles"
    bundles_dst_dir.mkdir(exist_ok=True)
    overlay = bundles_dst_dir / "vsphere-overlay.yaml"
    URL = "https://raw.githubusercontent.com/charmed-kubernetes/bundle/main/overlays/vsphere-overlay.yaml"
    with overlay.open("wb") as fp:
        with urllib.request.urlopen(URL) as f:
            fp.write(f.read())


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest, vsphere_overlay: Path):
    """Build the charm-under-test and deploy it together with related charms.

    Assert on the unit status before any relations/configurations take place.
    """
    charm = next(Path(".").glob("kubevirt*.charm"), None)
    if not charm:
        # Build and deploy charm from local source folder
        log.info("Build Charm...")
        charm = await ops_test.build_charm(".")

    overlays = [
        Bundle("kubernetes-core", channel="edge"),
        vsphere_overlay,
        Path("tests/data/charm.yaml"),
    ]
    bundle, *overlays = await ops_test.async_render_bundles(*overlays, charm=charm.resolve())

    log.info("Deploy Charm...")
    model = ops_test.model_full_name
    cmd = f"juju deploy -m {model} {bundle} " + " ".join(
        f"--overlay={f} --trust" for f in overlays
    )
    rc, stdout, stderr = await ops_test.run(*shlex.split(cmd))
    assert rc == 0, f"Bundle deploy failed: {(stderr or stdout).strip()}"

    log.info(stdout)
    await ops_test.model.block_until(lambda: APP_NAME in ops_test.model.applications, timeout=60)

    # Deploy the charm and wait for active/idle status
    await ops_test.model.wait_for_idle(wait_for_active=True, timeout=60 * 60)


async def test_kubevirt_deployed(kubernetes):
    KubeVirt = get_generic_resource("kubevirt.io/v1", "KubeVirt")
    kubevirt = await kubernetes.get(KubeVirt, name="kubevirt", namespace="kubevirt")
    assert kubevirt.status["phase"] == "Deployed"


async def wait_for(client, resource, match):
    async for op, dep in client.watch(resource):
        if match(op, dep):
            return


async def test_launch_vmi(kubernetes):
    storage_yaml = Path("tests/data/storage-pvc.yaml").read_text()
    storage = from_dict(yaml.safe_load(storage_yaml))
    await kubernetes.apply(storage)
    await wait_for(
        kubernetes,
        type(storage),
        lambda ops, dep: (
            dep.metadata.name == storage.metadata.name and dep.status.phase == "Bound"
        ),
    )

    vmi_yaml = Path("tests/data/vmi-ubuntu.yaml").read_text()
    vmi = from_dict(yaml.safe_load(vmi_yaml))
    await kubernetes.apply(vmi)
    await wait_for(
        kubernetes,
        type(vmi),
        lambda ops, dep: (
            dep.metadata.name == vmi.metadata.name and dep.status["phase"] == "Running"
        ),
    )

    instance = await kubernetes.get(type(vmi), vmi.metadata.name)
    assert instance.status["phase"] == "Running"