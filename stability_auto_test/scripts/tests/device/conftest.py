from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption("--device", action="store", default=None,
                     help="ADB serial for device tests")
    parser.addoption("--package", action="store", default=None,
                     help="Target package for fault injection")


@pytest.fixture
def device_serial(request) -> str:
    serial = request.config.getoption("--device")
    if not serial:
        pytest.skip("--device is required for device tests")
    return serial


@pytest.fixture
def target_package(request) -> str:
    pkg = request.config.getoption("--package")
    if not pkg:
        pytest.skip("--package is required for device tests")
    return pkg
