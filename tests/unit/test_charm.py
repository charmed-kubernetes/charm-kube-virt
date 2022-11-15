# Copyright 2022 Adam Dyess
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing


import ops.testing
import pytest

from charm import CharmKubeVirtCharm

ops.testing.SIMULATE_CAN_CONNECT = True


@pytest.fixture
def harness():
    harness = ops.testing.Harness(CharmKubeVirtCharm)
    try:
        yield harness
    finally:
        harness.cleanup()


def test_passes():
    pass
