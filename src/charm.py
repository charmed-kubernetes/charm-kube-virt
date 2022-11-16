#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Dispatch logic for the kube-virt operator charm."""

import logging

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.interface_kube_control import KubeControlRequirer
from ops.main import main
from ops.manifests import Collector
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

from config import CharmConfig
from kubevirt_manifests import KubeVirtOperator
from kubevirt_peer import KubeVirtPeer

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


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
        )
        self.collector = Collector(
            KubeVirtOperator(
                self,
                self.charm_config,
                self.kube_control,
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

        self.unit.status = ActiveStatus("Ready")
        if self.unit.is_leader():
            self.unit.set_workload_version(self.collector.short_version)
            self.app.status = ActiveStatus(self.collector.long_version)

    def _kube_control(self, event=None):
        self.kube_control.set_auth_request(self.unit.name)
        return self._merge_config(event)

    def _kube_virt(self, event=None):
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
            return
        return True

    def _check_config(self):
        self.unit.status = MaintenanceStatus("Evaluating charm config.")
        evaluation = self.charm_config.evaluate()
        if evaluation:
            self.unit.status = BlockedStatus(evaluation)
            return False
        return True

    def _merge_config(self, event=None):
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
            return

        self.stored.config_hash = new_hash
        self.stored.deployed = False
        self._install_or_upgrade()

    def _install_or_upgrade(self, _event=None):
        self._kube_virt(_event)

        if not self.stored.config_hash:
            return
        if self.unit.is_leader():
            self.unit.status = MaintenanceStatus("Deploying KubeVirt Operator")
            self.unit.set_workload_version("")
            for controller in self.collector.manifests.values():
                controller.apply_manifests()
        self.stored.deployed = True

    def _cleanup(self, _event):
        if self.stored.config_hash:
            self.unit.status = MaintenanceStatus("Cleaning up KubeVirt Operator")
            for controller in self.collector.manifests.values():
                controller.delete_manifests(ignore_unauthorized=True)
        self.unit.status = MaintenanceStatus("Shutting down")


if __name__ == "__main__":  # pragma: nocover
    main(CharmKubeVirtCharm)
