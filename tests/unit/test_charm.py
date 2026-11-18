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
    mock_installed[0].resource.status.conditions = [
        mock.MagicMock(type="Tested", status="False")
    ]

    with (
        mock.patch(
            "ops.manifests.manifest.Manifests.installed_resources",
        ) as mocker,
        mock.patch.object(
            type(harness_installed.charm.kube_operator),
            "deployed_release",
            new_callable=mock.PropertyMock,
            return_value=None,
        ),
    ):
        mocker.return_value = mock_installed
        assert harness_installed.charm._update_status({}) is None
    assert (
        harness_installed.charm.unit.status.message
        == "KubeVirt/kubevirt/kubevirt is not Tested"
    )


RELEASES = ["v1.0.0", "v0.2.1", "v0.2.0", "v0.1.1", "v0.1.0"]


@pytest.mark.parametrize(
    "deployed, target, step",
    [
        (None, "v1.0.0", None),  # fresh install
        ("devel", "v1.0.0", None),  # unparsable deployed version
        ("v0.1.0", "v0.1.1", None),  # patch update
        ("v0.1.1", "v0.2.1", None),  # next minor from its latest patch
        ("v0.2.1", "v1.0.0", None),  # next minor across a major release
        ("v1.0.0", "v0.2.1", None),  # not an upgrade
        ("v0.1.0", "v0.2.1", "v0.1.1"),  # latest patch must come first
        ("v0.1.1", "v1.0.0", "v0.2.1"),  # minor releases may not be skipped
    ],
)
def test_upgrade_path_error(harness, deployed, target, step):
    harness.begin()
    operator = harness.charm.kube_operator
    operator.releases = RELEASES
    with (
        mock.patch.object(
            type(operator), "deployed_release", new_callable=mock.PropertyMock
        ) as deployed_release,
        mock.patch.object(
            type(operator), "current_release", new_callable=mock.PropertyMock
        ) as current_release,
    ):
        deployed_release.return_value = deployed
        current_release.return_value = target
        error = operator.upgrade_path_error()
    if step:
        assert f"operator-release={step}" in error
    else:
        assert error is None


def test_deployed_release(harness, lk_client, api_error_klass):
    harness.begin()
    lk_client.get.return_value.status = {"observedKubeVirtVersion": "v0.1.0"}
    assert harness.charm.kube_operator.deployed_release == "v0.1.0"

    api_error_klass.status.code = 404
    lk_client.get.side_effect = api_error_klass()
    assert harness.charm.kube_operator.deployed_release is None


def test_install_manifests_blocked_by_upgrade_path(harness):
    harness.set_leader(True)
    harness.begin()
    with (
        mock.patch.object(
            harness.charm.kube_operator, "upgrade_path_error", return_value="skip"
        ),
        mock.patch.object(harness.charm.kube_operator, "apply_manifests") as apply,
    ):
        assert (
            harness.charm._install_manifests(mock.MagicMock(), config_hash=1) is False
        )
    apply.assert_not_called()
    assert harness.charm.unit.status == ops.model.BlockedStatus("skip")
