#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
"""Dispatch logic for the kube-virt operator charm."""

import logging
import subprocess
from pathlib import Path
from typing import Tuple

import charms.operator_libs_linux.v0.apt as apt
from charms.operator_libs_linux.v0.apt import PackageError, PackageNotFoundError
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.interface_kube_control import KubeControlRequirer
from ops.main import main
from ops.manifests import Collector, ManifestClientError
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

from config import CharmConfig
from kubevirt_manifests import KubeVirtOperator
from kubevirt_peer import KubeVirtPeer

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


def adjust_libvirtd_aa() -> Tuple[str, bool]:
    """Adjust usr.sbin.libvirtd apparmor profile."""
    aa_profile = Path("/etc/apparmor.d/usr.sbin.libvirtd")
    if not aa_profile.exists():
        return f"apparmor profile not available {aa_profile}", True
    lines = aa_profile.read_text().splitlines()
    pux_found = False
    insertable = "  /usr/libexec/qemu-kvm PUx,"
    for idx, line in enumerate(lines):
        line_pux = line.endswith("PUx,")
        pux_found |= line_pux
        if pux_found and not line_pux:
            print("insert line here -- eject")
            lines.insert(idx, insertable)
            break
        if insertable == line:
            print("inserted line already found -- eject")
            break
    aa_profile.write_text("\n".join(lines))

    try:
        subprocess.check_call(["systemctl", "reload", "apparmor.service"])
    except subprocess.CalledProcessError as e:
        msg = f"could not reload apparmor service. Reason: {e}"
        return msg, False

    return "", False


class CharmKubeVirtCharm(CharmBase):
    """Charm the service."""

    stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        # Relation Validator and datastore
        self.kube_control = KubeControlRequirer(self)
        self.kube_virt = KubeVirtPeer(self)

        # Config Validator and datastore
        self.charm_config = CharmConfig(self)

        self.stored.set_default(
            cluster_tag=None,  # passing along to the integrator from the kube-control relation
            config_hash=None,  # hashed value of the charm config once valid
            deployed=False,  # True if the config has been applied after new hash
            has_kvm=False,  # True if this unit has /dev/kvm
        )
        self.collector = Collector(
            KubeVirtOperator(
                self,
                self.charm_config,
                self.kube_control,
                self.kube_virt,
            )
        )
        self.framework.observe(self.on.kube_control_relation_created, self._kube_control)
        self.framework.observe(self.on.kube_control_relation_joined, self._kube_control)
        self.framework.observe(self.on.kube_control_relation_changed, self._kube_control)
        self.framework.observe(self.on.kube_control_relation_broken, self._merge_config)

        self.framework.observe(self.on.kubevirts_relation_created, self._kube_virt)
        self.framework.observe(self.on.kubevirts_relation_joined, self._kube_virt)
        self.framework.observe(self.on.kubevirts_relation_changed, self._kube_virt)
        self.framework.observe(self.on.kubevirts_relation_broken, self._kube_virt)

        self.framework.observe(self.on.list_versions_action, self._list_versions)
        self.framework.observe(self.on.list_resources_action, self._list_resources)
        self.framework.observe(self.on.scrub_resources_action, self._scrub_resources)
        self.framework.observe(self.on.sync_resources_action, self._sync_resources)
        self.framework.observe(self.on.update_status, self._update_status)

        self.framework.observe(self.on.install, self._install_or_upgrade)
        self.framework.observe(self.on.upgrade_charm, self._install_or_upgrade)
        self.framework.observe(self.on.config_changed, self._merge_config)
        self.framework.observe(self.on.stop, self._cleanup)

    def _list_versions(self, event):
        self.collector.list_versions(event)

    def _list_resources(self, event):
        manifests = event.params.get("manifest", "")
        resources = event.params.get("resources", "")
        self.collector.list_resources(event, manifests, resources)

    def _scrub_resources(self, event):
        manifests = event.params.get("manifest", "")
        resources = event.params.get("resources", "")
        return self.collector.scrub_resources(event, manifests, resources)

    def _sync_resources(self, event):
        manifests = event.params.get("manifest", "")
        resources = event.params.get("resources", "")
        return self.collector.apply_missing_resources(event, manifests, resources)

    def _update_status(self, _):
        if not self.stored.deployed:
            return

        unready = self.collector.unready
        if unready:
            self.unit.status = WaitingStatus(", ".join(unready))
            return

        phases = ", ".join(
            f"{name}: {phase}"
            for name, manifest in self.collector.manifests.items()
            for phase in manifest.phases()
        )

        status_type = ActiveStatus if "Deployed" in phases else WaitingStatus
        self.unit.status = status_type(phases)
        if self.unit.is_leader():
            self.unit.set_workload_version(self.collector.short_version)
            self.app.status = ActiveStatus(self.collector.long_version)

    def _kube_control(self, event):
        self.kube_control.set_auth_request(self.unit.name)
        return self._merge_config(event)

    def _kube_virt(self, event):
        self.kube_virt.discover()
        return self._merge_config(event)

    def _check_kube_virts(self, event):
        self.unit.status = MaintenanceStatus("Evaluating Peers.")
        evaluation = self.kube_virt.evaluate_relation(event)
        if evaluation:
            if "Waiting" in evaluation:
                self.unit.status = WaitingStatus(evaluation)
            else:
                self.unit.status = BlockedStatus(evaluation)
            return False
        return True

    def _check_kube_control(self, event):
        self.unit.status = MaintenanceStatus("Evaluating kubernetes authentication.")
        evaluation = self.kube_control.evaluate_relation(event)
        if evaluation:
            if "Waiting" in evaluation:
                self.unit.status = WaitingStatus(evaluation)
            else:
                self.unit.status = BlockedStatus(evaluation)
            return False
        if not self.kube_control.get_auth_credentials(self.unit.name):
            self.unit.status = WaitingStatus("Waiting for kube-control: unit credentials")
            return False
        if not (Path.home() / ".kube/config").exists():
            logger.info("Expected kubeconfig not found on filesystem")
            self.unit.status = WaitingStatus("Waiting for kubeconfig")
            event.defer()
            return False
        return True

    def _check_config(self):
        self.unit.status = MaintenanceStatus("Evaluating charm config.")
        evaluation = self.charm_config.evaluate()
        if evaluation:
            self.unit.status = BlockedStatus(evaluation)
            return False
        return True

    def _merge_config(self, event):
        if not self._check_kube_control(event):
            return

        if not self._check_kube_virts(event):
            return

        if not self._check_config():
            return

        self.unit.status = MaintenanceStatus("Evaluating Manifests")
        new_hash = 0
        for controller in self.collector.manifests.values():
            evaluation = controller.evaluate()
            if evaluation:
                self.unit.status = BlockedStatus(evaluation)
                return
            new_hash += controller.hash()

        if new_hash == self.stored.config_hash:
            self._update_status(event)
            return

        self.stored.config_hash = new_hash
        self.stored.deployed = False
        self._install_or_upgrade(event)

    def _setup_kvm(self) -> Tuple[str, bool]:
        """Apply machine changes to run qemu-kvm workloads."""
        if not self.stored.has_kvm:
            return "", False

        # Symlink installed binary to expected location
        ubuntu_installed = Path("/usr/bin/qemu-system-x86_64")
        if not ubuntu_installed.exists():
            return f"qemu-kvm not installed at {ubuntu_installed}", True

        kubevirt_expected = Path("/usr/libexec/qemu-kvm")
        if not kubevirt_expected.exists():
            kubevirt_expected.symlink_to(ubuntu_installed)

        return adjust_libvirtd_aa()

    def _upgrade_qemu(self) -> Tuple[str, bool]:
        self.unit.status = MaintenanceStatus("Installing Qemu")
        self.stored.has_kvm = self.kube_virt.dev_kvm_exists
        logger.info("Installing apt packages")
        packages = ["qemu"]

        if self.stored.has_kvm:
            packages += [
                "qemu-system-x86",
                "libvirt-daemon-system",
                "libvirt-clients",
                "bridge-utils",
            ]

        try:
            # Run `apt-get update` and add packages
            apt.add_package(packages, update_cache=True)
        except PackageNotFoundError:
            msg = "a specified package not found in package cache or on system"
            return msg, False
        except PackageError as e:
            msg = f"could not install package. Reason: {e}"
            return msg, False

        return self._setup_kvm()

    def _install_or_upgrade(self, event):
        self._kube_virt(event)

        msg, retriable = self._upgrade_qemu()
        if retriable:
            logger.error(msg)
            self.unit.status = WaitingStatus(msg)
            event.defer()
            return
        elif msg:
            logger.warning(msg)
            self.unit.status = BlockedStatus(msg)
            return

        if not self.stored.config_hash:
            return
        if self.unit.is_leader():
            self.unit.status = MaintenanceStatus("Deploying KubeVirt Operator")
            self.unit.set_workload_version("")
            for controller in self.collector.manifests.values():
                try:
                    controller.apply_manifests()
                except ManifestClientError:
                    self.unit.status = WaitingStatus("Waiting for kube-apiserver")
                    event.defer()
                    return
        self.stored.deployed = True

    def _cleanup(self, event):
        if not self.stored.config_hash:
            return

        if self.unit.is_leader():
            self.unit.status = MaintenanceStatus("Cleaning up KubeVirt Operator")
            for controller in self.collector.manifests.values():
                try:
                    controller.delete_manifests(ignore_unauthorized=True)
                except ManifestClientError:
                    self.unit.status = WaitingStatus("Waiting for kube-apiserver")
                    event.defer()
                    return

        self.unit.status = MaintenanceStatus("Shutting down")


if __name__ == "__main__":  # pragma: nocover
    main(CharmKubeVirtCharm)
