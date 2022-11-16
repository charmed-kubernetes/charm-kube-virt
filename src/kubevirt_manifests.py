# Copyright 2022 Canonical Ltd.
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
    REQUIRED = {"software-emulation"}

    def __call__(self, obj):
        """Update the kubevirt object."""
        if not (obj.kind == "kubevirt" and obj.metadata.name == self.NAME):
            return
        software_emulation = self.manifests.config.get("software-emulation")
        if not isinstance(software_emulation, bool):
            log.error(
                f"kubevirt software-emulation was an unexpected type: {type(software_emulation)}"
            )
        obj.spec.configuration.developerConfiguration.useEmulation = software_emulation


class KubeVirtBase(Manifests):
    """Base class for KubeVirt Manifests."""

    def hash(self) -> int:
        """Calculate a hash of the current configuration."""
        return int(md5(pickle.dumps(self.config)).hexdigest(), 16)


class KubeVirtCustomResources(KubeVirtBase):
    """Deployment Specific details for the kubevirt-cr."""

    def __init__(self, charm, charm_config, kube_virts):
        manipulations = [
            ManifestLabel(self),
            UpdateKubeVirt(self),
        ]
        super().__init__("kubevirt-custom-resource", charm.model, "upstream/custom_resource", manipulations)
        self.unit = charm.unit
        self.charm_config = charm_config
        self.kube_virts = kube_virts

    def evaluate(self) -> Optional[str]:
        """Determine if manifest_config can be applied to manifests."""
        props = UpdateKubeVirt.REQUIRED
        for prop in props:
            value = self.config.get(prop)
            if not value:
                return f"KubeVirt manifests waiting for definition of {prop}"
        return None

    @property
    def config(self) -> Dict:
        """Returns current config available from charm config and joined relations."""
        config = {}
        if self.kube_virts.is_ready:
            config["software-emulation"] = not self.kube_virts.supports_kvm

        config.update(**self.charm_config.available_data)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        return config


class KubeVirtOperator(KubeVirtBase):
    """Deployment Specific details for the kubevirt-operator."""

    def __init__(self, charm, charm_config, kube_control):
        manipulations = [
            ManifestLabel(self),
            ConfigRegistry(self),
        ]
        super().__init__("kubevirt", charm.model, "upstream/operator", manipulations)
        self.unit = charm.unit
        self.charm_config = charm_config
        self.kube_control = kube_control

    @property
    def config(self) -> Dict:
        """Returns current config available from charm config and joined relations."""
        config = {}
        if self.kube_control.is_ready:
            config["image-registry"] = self.kube_control.get_registry_location()

        config.update(**self.charm_config.available_data)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("operator-release", None)

        return config

    def evaluate(self) -> Optional[str]:
        """These manifests are always ready to be updated."""
        return None