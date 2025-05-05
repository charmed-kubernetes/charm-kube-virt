# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of KubeVirt specific details of the kubernetes manifests."""

import logging
import pickle
from hashlib import md5
from typing import Dict, Optional

from ops.manifests import ConfigRegistry, ManifestLabel, Manifests, Patch

log = logging.getLogger(__file__)


class UpdateKubeVirt(Patch):
    """Update the CRD KubeVirt as a patch."""

    NAME = "kubevirt"
    REQUIRED = {"software-emulation", "pvc-tolerate-less-space-up-to-percent"}

    def __call__(self, obj):
        """Update the kubevirt object."""
        if not (obj.kind == "KubeVirt" and obj.metadata.name == self.NAME):
            return
        software_emulation = self.manifests.config.get("software-emulation")
        if not isinstance(software_emulation, bool):
            log.error(
                f"kubevirt software-emulation was an unexpected type: {type(software_emulation)}"
            )
        dev_config = obj.spec["configuration"]["developerConfiguration"]
        log.info(
            ("Enabling" if software_emulation else "Disabling")
            + " kubevirt software-emulation"
        )
        dev_config["useEmulation"] = software_emulation

        pvc_toleration = self.manifests.config.get(
            "pvc-tolerate-less-space-up-to-percent"
        )
        log.info(f"kubevirt pvcTolerateLessSpaceUpToPercent={pvc_toleration}%")
        dev_config["pvcTolerateLessSpaceUpToPercent"] = pvc_toleration


class KubeVirtOperator(Manifests):
    """Deployment Specific details for the kubevirt-operator."""

    def __init__(self, charm, charm_config, kube_control, kube_virts):
        manipulations = [
            ManifestLabel(self),
            ConfigRegistry(self),
            UpdateKubeVirt(self),
        ]
        super().__init__("kubevirt", charm.model, "upstream/operator", manipulations)
        self.unit = charm.unit
        self.charm_config = charm_config
        self.kube_control = kube_control
        self.kube_virts = kube_virts

    def hash(self) -> int:
        """Calculate a hash of the current configuration."""
        return int(md5(pickle.dumps(self.config)).hexdigest(), 16)

    @property
    def config(self) -> Dict:
        """Returns current config available from charm config and joined relations."""
        config = {}
        if self.kube_control.is_ready:
            config["image-registry"] = self.kube_control.get_registry_location()

        if self.kube_virts.is_ready:
            config["software-emulation"] = not self.kube_virts.supports_kvm

        config.update(**self.charm_config.available_data)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("operator-release", None)

        return config

    def evaluate(self) -> Optional[str]:
        """Determine if manifest_config can be applied to manifests."""
        props = UpdateKubeVirt.REQUIRED
        for prop in props:
            value = self.config.get(prop)
            if value is None:
                return f"KubeVirt manifests waiting for definition of {prop}"

        percent = self.config.get("pvc-tolerate-less-space-up-to-percent")
        if percent is not None and not (0 < percent < 100):
            return f"pvc-tolerate-less-space-up-to-percent is not in range: 0 < {percent} < 100"

        return None

    @property
    def phases(self):
        """Details phases of resources in this manifest."""
        return sorted(
            (obj, phase)
            for obj in self.installed_resources()
            if obj.kind == "KubeVirt"
            for phase in [
                obj.resource.status["phase"] if obj.resource.status else "Unknown"
            ]
        )
