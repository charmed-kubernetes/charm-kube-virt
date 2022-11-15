# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of KubeVirt specific details of the kubernetes manifests."""
import logging
import pickle
import tempfile
from functools import cached_property
from hashlib import md5
from pathlib import Path
from typing import Dict, Optional

from lightkube import Client, KubeConfig
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

    CA_CERT_PATH = Path("/root/cdk/ca.crt")
    KUBECONFIG_PATH = Path("/root/cdk/kubeconfig")

    @cached_property
    def kubeconfig(self) -> KubeConfig:
        """Provide kubeconfig found on machine or from kube-control relation."""
        if self.KUBECONFIG_PATH.exists():
            return KubeConfig.from_file(self.KUBECONFIG_PATH)

        self.CA_CERT_PATH.parent.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile() as fp:
            kubeconfig_file = fp.name
            self.kube_control.create_kubeconfig(
                ca=self.CA_CERT_PATH,
                kubeconfig=kubeconfig_file,
                user=self.unit.name,
                k8s_user=self.unit.name,
            )
            return KubeConfig.from_file(kubeconfig_file)

    @cached_property
    def client(self) -> Client:
        """Lazy evaluation of the lightkube client."""
        return Client(config=self.kubeconfig, field_manager=f"{self.model.app.name}-{self.name}")

    def hash(self) -> int:
        """Calculate a hash of the current configuration."""
        return int(md5(pickle.dumps(self.config)).hexdigest(), 16)


class KubeVirtCustomResources(Manifests):
    """Deployment Specific details for the kubevirt-cr."""

    def __init__(self, charm, charm_config, kube_control):
        manipulations = [
            ManifestLabel(self),
            UpdateKubeVirt(self),
        ]
        super().__init__("kubevirt-cr", charm.model, "upstream/custom_resource", manipulations)
        self.unit = charm.unit
        self.charm_config = charm_config
        self.kube_control = kube_control

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
        return dict(**self.charm_config.available_data)


class KubeVirtOperator(Manifests):
    """Deployment Specific details for the kubevirt-operator."""

    def __init__(self, charm, charm_config, kube_control):
        manipulations = [
            ManifestLabel(self),
            ConfigRegistry(self),
        ]
        super().__init__("kubevirt-operator", charm.model, "upstream/operator", manipulations)
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
