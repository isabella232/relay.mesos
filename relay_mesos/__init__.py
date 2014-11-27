import logging
log = logging.getLogger('relay.mesos')

# expose configure_logging to those who wish to develop relay
from relay import configure_logging
configure_logging(True)

import os.path as _p
import pkg_resources as _pkg_resources
__version__ = _pkg_resources.get_distribution(
    _p.basename(_p.dirname(_p.dirname(_p.abspath(__file__))))).version


def metric():
    while True:
        yield 0


def target():
    while True:
        yield 0
