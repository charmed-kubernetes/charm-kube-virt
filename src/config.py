# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Config Management for the KubeVirt charm."""

import logging
from typing import Optional

log = logging.getLogger(__name__)


class CharmConfig:
    """Representation of the charm configuration."""

    def __init__(self, charm):
        """Creates a CharmConfig object from the configuration data."""
        self.charm = charm

    def evaluate(self) -> Optional[str]:
        """Determine if configuration is valid."""
        return None

    @property
    def available_data(self):
        """Parse valid charm config into a dict, drop keys if unset."""
        data = {}
        for key, value in self.charm.config.items():
            data[key] = value

        for key, value in dict(**data).items():
            if value == "" or value is None:
                del data[key]

        return data
