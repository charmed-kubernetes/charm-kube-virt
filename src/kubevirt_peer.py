# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of kubevirts relation (peers)."""

import logging
from functools import cached_property
from pathlib import Path
from typing import List, Optional

from ops.charm import CharmBase, RelationBrokenEvent
from ops.framework import Object
from ops.model import Relation
from pydantic import BaseModel, Field, Json, ValidationError

log = logging.getLogger("KubeControlRequirer")


class Data(BaseModel):
    """Data aquired from the kubevirts peer relation."""

    supports_kvm: Json[bool] = Field(alias="supports-kvm")


class KubeVirtPeer(Object):
    """Manages data exchange across the kubevirts relation."""

    def __init__(self, charm: CharmBase, endpoint: str = "kubevirts"):
        super().__init__(charm, f"relation-{endpoint}")
        self.endpoint = endpoint

    @cached_property
    def relation(self) -> Optional[Relation]:
        """The lone relation endpoint or None."""
        return self.model.get_relation(self.endpoint)

    @cached_property
    def _data(self) -> List[Data]:
        if self.relation:
            units = self.relation.units | {self.model.unit}
            return [Data(**self.relation.data[u]) for u in units]
        return []

    def evaluate_relation(self, event) -> Optional[str]:
        """Determine if relation is ready."""
        no_relation = not self.relation or (
            isinstance(event, RelationBrokenEvent) and event.relation is self.relation
        )
        if not self.is_ready:
            if no_relation:
                return f"Missing required {self.endpoint} relation"
            return f"Waiting for {self.endpoint} relation"
        return None

    @property
    def is_ready(self):
        """Whether the request for this instance has been completed."""
        try:
            self._data
        except ValidationError as ve:
            log.error(f"{self.endpoint} relation data not yet valid. ({ve}")
            return False
        if not self._data:
            log.error(f"{self.endpoint} relation data not yet available.")
            return False
        return True

    def discover(self) -> None:
        """Determine if this host supports kvm, and informs peers."""
        if self.relation:
            self.relation.data[self.model.unit].update(
                {"supports-kvm": "true" if Path("/dev/kvm").exists() else "false"}
            )

    @property
    def supports_kvm(self) -> Optional[bool]:
        """At least one peer supports kvm."""
        return any(_.supports_kvm for _ in self._data) if self.is_ready else None
