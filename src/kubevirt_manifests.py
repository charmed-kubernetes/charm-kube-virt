# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of KubeVirt specific details of the kubernetes manifests."""

import logging
import pickle
import re
from hashlib import md5
from typing import Dict, Optional, Tuple

from httpx import HTTPError
from lightkube import ApiError
from lightkube.generic_resource import create_namespaced_resource
from ops.manifests import (
    ConfigRegistry,
    ManifestClientError,
    ManifestLabel,
    Manifests,
    Patch,
)

log = logging.getLogger(__file__)
RELEASE_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _parse_release(release: Optional[str]) -> Optional[Tuple[int, ...]]:
    """Parse a vX.Y.Z release tag into a comparable tuple."""
    match = RELEASE_RE.match(release or "")
    return tuple(map(int, match.groups())) if match else None


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
    def deployed_release(self) -> Optional[str]:
        """KubeVirt release fully rolled out in the cluster, None if not deployed."""
        kubevirt_cls = create_namespaced_resource(
            "kubevirt.io", "v1", "KubeVirt", "kubevirts"
        )
        try:
            kubevirt = self.client.get(
                kubevirt_cls, UpdateKubeVirt.NAME, namespace="kubevirt"
            )
        except ApiError as e:
            if e.status.code == 404:
                return None
            raise ManifestClientError("Failed to read the KubeVirt resource", e) from e
        except HTTPError as e:
            raise ManifestClientError("Failed to read the KubeVirt resource", e) from e
        return (kubevirt.status or {}).get("observedKubeVirtVersion")

    def upgrade_path_error(self) -> Optional[str]:
        """Reject a release which skips a step of KubeVirt's supported upgrade path.

        KubeVirt only supports updating from the latest patch of one minor release
        to the latest patch of the next minor release, see
        https://kubevirt.io/user-guide/cluster_admin/updating_and_deletion/
        """
        deployed, target = self.deployed_release, self.current_release
        current, wanted = _parse_release(deployed), _parse_release(target)
        if not (current and wanted) or wanted[:2] <= current[:2]:
            return None
        bundled = sorted(filter(None, map(_parse_release, self.releases)))
        newer_patches = [r for r in bundled if r[:2] == current[:2] and r > current]
        if newer_patches:
            step = newer_patches[-1]
        else:
            next_minor = min(
                (r[:2] for r in bundled if r[:2] > current[:2]), default=None
            )
            if next_minor is None or wanted[:2] == next_minor:
                return None
            step = max(r for r in bundled if r[:2] == next_minor)
        return (
            f"KubeVirt {deployed} cannot update directly to {target}; "
            f"set operator-release=v{'.'.join(map(str, step))} first"
        )

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
