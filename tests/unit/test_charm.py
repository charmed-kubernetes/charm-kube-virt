# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing


import unittest.mock as mock

import ops.testing
import pytest

from charm import CharmKubeVirtCharm

ops.testing.SIMULATE_CAN_CONNECT = True


@pytest.fixture
def harness():
    harness = ops.testing.Harness(CharmKubeVirtCharm)
    try:
        yield harness
    finally:
        harness.cleanup()


@pytest.fixture
def harness_installed(harness):
    harness.set_leader(True)
    with mock.patch("charm.CharmKubeVirtCharm._install_or_upgrade", autospec=True):
        harness.begin_with_initial_hooks()
        yield harness


def test_update_status_with_conditions(harness_installed):
    harness_installed.charm.stored.deployed = True
    harness_installed.charm.stored.installed = True

    mock_installed = list(harness_installed.charm.kube_operator.status())
    mock_installed[0].resource.kind = "KubeVirt"
    mock_installed[0].resource.metadata.namespace = "kubevirt"
    mock_installed[0].resource.metadata.name = "kubevirt"
    mock_installed[0].resource.status.conditions = [mock.MagicMock(type="Tested", status="False")]

    with mock.patch(
        "ops.manifests.manifest.Manifests.installed_resources",
    ) as mocker:
        mocker.return_value = mock_installed
        assert harness_installed.charm._update_status({}) is None
    assert (
        harness_installed.charm.unit.status.message == "KubeVirt/kubevirt/kubevirt is not Tested"
    )
