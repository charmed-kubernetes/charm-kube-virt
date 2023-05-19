#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
"""Dispatch logic for the kube-virt operator charm."""

import logging
import os
import subprocess
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

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
VIRTCTL_URL = "https://github.com/kubevirt/kubevirt/releases/download/{version}/virtctl-{version}-linux-{arch}"


@contextmanager
def _modified_env(**update: str):
    orig = dict(os.environ)
    os.environ.update({k: v for k, v in update.items() if isinstance(v, str)})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(orig)


def _fetch_file(url, dest):
    proxies = {
        "HTTPS_PROXY": os.environ.get("JUJU_CHARM_HTTPS_PROXY"),
        "HTTP_PROXY": os.environ.get("JUJU_CHARM_HTTP_PROXY"),
    }
    with _modified_env(**proxies):
        proxy = urllib.request.ProxyHandler()
        opener = urllib.request.build_opener(proxy)
        urllib.request.install_opener(opener)
        urllib.request.urlretrieve(url, filename=dest)


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
        self.kube_operator = KubeVirtOperator(
            self, self.charm_config, self.kube_control, self.kube_virt
        )

        self.stored.set_default(
            cluster_tag=None,  # passing along to the integrator from the kube-control relation
            config_hash=None,  # hashed value of the charm config once valid
            install_failure=None,  # None if the binaries have been installed successfully
            deployed=False,  # True if the config has been applied after new hash
            has_kvm=False,  # True if this unit has /dev/kvm
        )
        self.collector = Collector(self.kube_operator)
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
        self.framework.observe(self.on.sync_install_action, self._binary_installation)
        self.framework.observe(self.on.update_status, self._update_status)

        self.framework.observe(self.on.install, self._install_or_upgrade)
        self.framework.observe(self.on.upgrade_charm, self._install_or_upgrade)
        self.framework.observe(self.on.config_changed, self._merge_config)
        self.framework.observe(self.on.stop, self._cleanup)

    def _ops_wait_for(self, event, msg: str, exc_info=None) -> str:
        self.unit.status = WaitingStatus(msg)
        if exc_info:
            logger.exception(msg)
        event.defer()
        return msg

    def _ops_blocked_by(self, msg: str, exc_info=None) -> str:
        self.unit.status = BlockedStatus(msg)
        if exc_info:
            logger.exception(msg)
        return msg

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
        try:
            self.collector.apply_missing_resources(event, manifests, resources)
        except ManifestClientError:
            msg = "Failed to apply missing resources. API Server unavailable."
            event.set_results({"result": msg})
        else:
            self.stored.deployed = True

    def _update_status(self, event):
        if not self.stored.deployed:
            logger.info("update-status: hook discovered not yet deployed")
            return

        if self.stored.install_failure:
            logger.info("update-status: hook discovered not yet installed...retrying")
            self._ops_blocked_by(self.stored.install_failure)
            return

        def unready_conditions(cond_pair):
            (_, rsc), cond = cond_pair
            if rsc.kind == "Kubevirt" and cond.status == "False":
                if cond.type in ["Degraded", "Progressing"]:
                    # ignore not degraded or not progressing
                    return None

            if cond.status == "True":
                return None

            return f"{rsc} is not {cond.type}"

        unready = [
            cond
            for pair in self.collector.conditions.items()
            if (cond := unready_conditions(pair))
        ]

        if unready:
            self.unit.status = WaitingStatus(", ".join(unready))
            return

        phases = ", ".join(f"{obj}: {phase}" for obj, phase in self.kube_operator.phases)

        status_type = WaitingStatus if "Deployed" not in phases else ActiveStatus
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
            self._ops_wait_for(event, "Waiting for kubeconfig")
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

        self.stored.deployed = False
        if self._install_manifests(event, config_hash=new_hash):
            self.stored.config_hash = new_hash
            self.stored.deployed = True
            self._update_status(event)

    def _setup_kvm(self, event) -> Optional[str]:
        """Apply machine changes to run qemu-kvm workloads."""
        if not self.stored.has_kvm:
            return None

        # Symlink installed binary to expected location
        ubuntu_installed = Path("/usr/bin/qemu-system-x86_64")
        if not ubuntu_installed.exists():
            logger.info(f"qemu-kvm not installed at {ubuntu_installed}")
            return self._ops_wait_for(event, "Waiting for qemu-kvm")

        kubevirt_expected = Path("/usr/libexec/qemu-kvm")
        if not kubevirt_expected.exists():
            kubevirt_expected.symlink_to(ubuntu_installed)
        return None

    def _adjust_libvirtd_aa(self, event) -> Optional[str]:
        """Adjust usr.sbin.libvirtd apparmor profile."""
        if not self.stored.has_kvm:
            return None

        aa_profile = Path("/etc/apparmor.d/usr.sbin.libvirtd")
        if not aa_profile.exists():
            logger.info(f"AppArmor libvirtd profile not available {aa_profile}")
            return self._ops_wait_for(event, "Waiting for AppArmor libvirtd profile")

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
        except subprocess.CalledProcessError:
            return self._ops_blocked_by("Could not reload apparmor service", exc_info=True)
        return None

    def _install_binaries(self, event) -> Optional[str]:
        self.unit.status = MaintenanceStatus("Installing Binaries")
        self.stored.has_kvm = self.kube_virt.dev_kvm_exists
        packages = ["qemu"]

        if self.stored.has_kvm:
            packages += [
                "qemu-system-x86",
                "libvirt-daemon-system",
                "libvirt-clients",
                "bridge-utils",
            ]

        logger.info(f"Installing apt packages {', '.join(packages)}")
        try:
            # Run `apt-get update` and add packages
            apt.add_package(packages, update_cache=True)
        except PackageNotFoundError:
            return self._ops_blocked_by("Apt packages not found.", exc_info=True)
        except PackageError:
            return self._ops_blocked_by("Could not apt install packages", exc_info=True)

        logger.info("Installing virtctl")
        try:
            fmt = dict(version=self.kube_operator.current_release, arch="amd64")
            virtctl = Path("virtctl")
            _fetch_file(VIRTCTL_URL.format(**fmt), virtctl)
        except urllib.error.URLError:
            return self._ops_blocked_by("Could not download virtctl", exc_info=True)

        virtctl.chmod(0o775)
        return None

    def _binary_installation(self, event):
        logger.info("Installing KubeVirt binaries...")
        error = self._kube_virt(event)
        error = error or self._install_binaries(event)
        error = error or self._setup_kvm(event)
        error = error or self._adjust_libvirtd_aa(event)
        self.stored.install_failure = error
        if error:
            logger.error(error)
        return error

    def _install_or_upgrade(self, event):
        error = self._binary_installation(event)
        error or self._install_manifests(event)

    def _install_manifests(self, event, config_hash=None):
        if self.stored.config_hash == config_hash:
            logger.info("Skipping until the config is evaluated.")
            return True
        if self.unit.is_leader():
            self.unit.status = MaintenanceStatus("Deploying KubeVirt Operator")
            self.unit.set_workload_version("")
            for controller in self.collector.manifests.values():
                try:
                    controller.apply_manifests()
                except ManifestClientError as e:
                    self._ops_wait_for(event, "Waiting for kube-apiserver")
                    logger.warn(f"Encountered retryable installation error: {e}")
                    event.defer()
                    return False
        return True

    def _cleanup(self, event):
        if not self.stored.config_hash:
            return

        if self.unit.is_leader():
            self.unit.status = MaintenanceStatus("Cleaning up KubeVirt Operator")
            for controller in self.collector.manifests.values():
                try:
                    controller.delete_manifests(ignore_unauthorized=True)
                except ManifestClientError:
                    self._ops_wait_for(event, "Waiting for kube-apiserver", exc_info=True)
                    event.defer()
                    return

        self.unit.status = MaintenanceStatus("Shutting down")


if __name__ == "__main__":  # pragma: nocover
    main(CharmKubeVirtCharm)
