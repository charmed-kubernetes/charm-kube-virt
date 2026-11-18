# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing


import unittest.mock as mock

import ops.testing
import pytest

from charm import CharmKubeVirtCharm
from kubevirt_manifests import _parse_release

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


def _sync_with_missing_operator(harness, deployed):
    """Run sync-resources while the virt-operator Deployment is missing."""
    harness.set_leader(True)
    harness.begin()
    operator = harness.charm.kube_operator
    expected = operator.resources
    installed = frozenset(r for r in expected if r.kind != "Deployment")
    assert installed != expected
    with (
        mock.patch.object(
            type(operator),
            "deployed_release",
            new_callable=mock.PropertyMock,
            return_value=deployed,
        ),
        mock.patch.object(operator, "labelled_resources", return_value=frozenset()),
        mock.patch.object(operator, "installed_resources", return_value=installed),
        mock.patch.object(operator, "conflicting_resources", return_value=frozenset()),
        mock.patch.object(operator, "apply_resources") as apply,
    ):
        try:
            output = harness.run_action("sync-resources")
        except ops.testing.ActionFailed as e:
            output = e
    return output, apply


def test_sync_resources_blocked_by_upgrade_path(harness):
    output, apply = _sync_with_missing_operator(harness, "v0.58.0")
    apply.assert_not_called()
    assert isinstance(output, ops.testing.ActionFailed)
    assert output.message.startswith("KubeVirt v0.58.0 cannot update directly to")
    assert not harness.charm.stored.deployed


def test_sync_resources_applies_missing_on_supported_path(harness):
    output, apply = _sync_with_missing_operator(harness, None)
    assert not isinstance(output, ops.testing.ActionFailed)
    (missing,) = apply.call_args.args
    assert missing.kind == "Deployment"
    assert harness.charm.stored.deployed


def _release(parsed):
    return "v" + ".".join(map(str, parsed))


def _refresh_deployed(harness, pick_deployed):
    """Refresh a deployed leader from a revision without an operator-release default.

    The stored config hash is the one computed while operator-release was unset;
    the refreshed charm then runs upgrade-charm with the schema default.
    """
    harness.set_leader(True)
    harness.begin()
    operator = harness.charm.kube_operator
    deployed = pick_deployed([_parse_release(r) for r in operator.releases])
    harness.update_config({"operator-release": ""})
    previous_hash = operator.hash()
    harness.update_config(unset=["operator-release"])
    harness.charm.stored.config_hash = previous_hash
    assert operator.hash() != previous_hash
    with (
        mock.patch.object(
            type(operator),
            "deployed_release",
            new_callable=mock.PropertyMock,
            return_value=deployed,
        ),
        mock.patch.object(operator, "apply_manifests") as apply,
        mock.patch.object(operator, "evaluate", return_value=None),
        mock.patch.object(CharmKubeVirtCharm, "_check_kube_control", return_value=True),
        mock.patch.object(CharmKubeVirtCharm, "_check_kube_virts", return_value=True),
        mock.patch.object(CharmKubeVirtCharm, "_install_binaries", return_value=None),
    ):
        harness.charm.on.upgrade_charm.emit()
    return apply


def test_schema_default_matches_version_file(harness):
    harness.begin()
    operator = harness.charm.kube_operator
    assert harness.charm.config["operator-release"] == operator.default_release
    assert operator.default_release in operator.releases


def test_refresh_rolls_out_default_release(harness):
    def previous_minor(releases):
        default = _parse_release(harness.charm.kube_operator.default_release)
        return _release(max(r for r in releases if r[:2] < default[:2]))

    apply = _refresh_deployed(harness, previous_minor)
    operator = harness.charm.kube_operator
    assert operator.current_release == operator.default_release
    assert harness.charm.stored.config_hash == operator.hash()
    apply.assert_called()


def test_refresh_blocks_default_release_skipping_steps(harness):
    deployed = "v0.58.0"  # default of the previous charm revisions
    apply = _refresh_deployed(harness, lambda releases: deployed)
    latest_patch = max(
        r
        for r in map(_parse_release, harness.charm.kube_operator.releases)
        if r[:2] == _parse_release(deployed)[:2]
    )
    apply.assert_not_called()
    status = harness.charm.unit.status
    assert isinstance(status, ops.model.BlockedStatus)
    assert status.message.endswith(f"operator-release={_release(latest_patch)} first")
