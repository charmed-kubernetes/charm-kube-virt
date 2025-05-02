# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
import random
import string
from pathlib import Path

import pytest
import pytest_asyncio
from lightkube import AsyncClient, KubeConfig
from lightkube.generic_resource import async_load_in_cluster_generic_resources
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Namespace

log = logging.getLogger(__name__)


def pytest_addoption(parser):
    parser.addoption("--series", action="store", default="", help="Customize ubuntu series")


@pytest.fixture(scope="module")
def module_name(request):
    return request.module.__name__.replace("_", "-")


@pytest_asyncio.fixture()
async def kubeconfig(ops_test):
    kubeconfig_path = ops_test.tmp_path / "kubeconfig"
    retcode, stdout, stderr = await ops_test.run(
        "juju",
        "scp",
        "kubernetes-control-plane/leader:/home/ubuntu/config",
        kubeconfig_path,
    )
    if retcode != 0:
        log.error(f"retcode: {retcode}")
        log.error(f"stdout:\n{stdout.strip()}")
        log.error(f"stderr:\n{stderr.strip()}")
        pytest.fail("Failed to copy kubeconfig from kubernetes-control-plane")
    assert Path(kubeconfig_path).stat().st_size, "kubeconfig file is 0 bytes"
    yield kubeconfig_path


@pytest.fixture()
async def kubernetes(kubeconfig, module_name):
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    namespace = f"{module_name}-{rand_str}"
    config = KubeConfig.from_file(kubeconfig)
    client = AsyncClient(
        config=config.get(context_name="juju-context"),
        namespace=namespace,
        trust_env=False,
        field_manager=rand_str,
    )
    await async_load_in_cluster_generic_resources(client)
    namespace_obj = Namespace(metadata=ObjectMeta(name=namespace))
    await client.create(namespace_obj)
    yield client
    await client.delete(Namespace, namespace)
