import pytest

from tests.fakes.amp import FakeAcomSerial
from tests.fakes.rig import FakeRigctldClient
from tests.fakes.sdr import FakeSdrClient


@pytest.fixture
def fake_rig() -> FakeRigctldClient:
    return FakeRigctldClient()


@pytest.fixture
def fake_amp() -> FakeAcomSerial:
    return FakeAcomSerial(port="/dev/fake-acom")


@pytest.fixture
def fake_sdr() -> FakeSdrClient:
    return FakeSdrClient()
