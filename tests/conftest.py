# @author Daniel McCoy Stephenson
import pytest

from helpers import Env


@pytest.fixture
def env(tmp_path):
    environment = Env(tmp_path)
    yield environment
    environment.stop()
