import atexit
import multiprocessing as mp
import os
import signal
import sys

from relay import argparse_shared as at
from relay.runner import main as relay_main, build_arg_parser as relay_ap
from relay_mesos import log
from relay_mesos.util import catch
from relay_mesos.scheduler import Scheduler


def warmer_cooler_wrapper(MV):
    """
    Act as a warmer or cooler function such that, instead of executing code,
    we ask mesos to execute it.
    """
    def _warmer_cooler_wrapper(n):
        # inform mesos that it should spin up n tasks of type f, where f is
        # either the warmer or cooler.  Since Relay assumes that the choice of
        # `f` (either a warmer or cooler func) is determined by the sign of n,
        # we can too!
        log.debug('asking mesos to spawn tasks')
        with MV.get_lock():
            # max MV since tasks last run on mesos
            if abs(MV.value) < abs(n):
                MV.value = n
        log.debug('...finished asking mesos to spawn tasks')
    return _warmer_cooler_wrapper


def main(ns):
    """
    Run Relay as a Mesos framework.
    Relay's event loop and the Mesos scheduler each run in separate processes
    and communicate through a multiprocessing.Pipe.

    These two processes bounce control back and forth between mesos
    resourceOffers and Relay's warmer/cooler functions.  Relay warmer/cooler
    functions request that mesos tasks get spun up, but those requests are only
    filled if the mesos scheduler receives enough relevant offers.  Relay's
    requests don't build up: only the largest request since the last fulfilled
    request is fulfilled at moment enough mesos resources are available.
    """
    if ns.mesos_master is None:
        log.error("Oops!  You didn't define --mesos_master")
        build_arg_parser().print_usage()
        sys.exit(1)
    log.info(
        "Starting Relay Mesos!",
        extra={k: str(v) for k, v in ns.__dict__.items()})

    # a distributed value storing the num and type of tasks mesos scheduler
    # should create at any given moment in time.
    # Sign of MV determines task type: warmer or cooler
    # ie. A positive value of n means n warmer tasks
    MV = mp.Value('l')  # max_val is a ctypes.c_int64

    # store exceptions that may be raised
    exception_receiver, exception_sender = mp.Pipe(False)
    # notify relay when mesos framework is ready
    mesos_ready = mp.Condition()

    # copy and then override warmer and cooler
    ns_relay = ns.__class__(**{k: v for k, v in ns.__dict__.items()})
    if ns.warmer:
        ns_relay.warmer = warmer_cooler_wrapper(MV)
    if ns.cooler:
        ns_relay.cooler = warmer_cooler_wrapper(MV)

    mesos_name = "Relay.Mesos Scheduler"
    mesos = mp.Process(
        target=catch(init_mesos_scheduler, exception_sender),
        kwargs=dict(ns=ns, MV=MV, exception_sender=exception_sender,
                    mesos_ready=mesos_ready),
        name=mesos_name)
    relay_name = "Relay.Runner Event Loop"
    relay = mp.Process(
        target=catch(init_relay, exception_sender),
        args=(ns_relay, mesos_ready),
        name=relay_name)
    mesos.start()  # start mesos framework
    relay.start()  # start relay's loop

    def kill_children(signal, frame):
        log.error(
            'Received a signal that is trying to terminate this process.'
            ' Terminating mesos and relay child processes!', extra=dict(
                signal=signal))
        try:
            mesos.terminate()
            log.info('terminated mesos scheduler')
        except:
            log.exception('could not terminate mesos scheduler')
        try:
            relay.terminate()
            log.info('terminated relay')
        except:
            log.exception('could not terminate relay')
        sys.exit(1)
    signal.signal(signal.SIGTERM, kill_children)
    signal.signal(signal.SIGINT, kill_children)
    while True:
        if exception_receiver.poll():
            exception_receiver.recv()
            relay.is_alive()
            log.error(
                'Terminating child processes because one of them raised'
                ' an exception', extra=dict(was_it_relay=not relay.is_alive(),
                                            was_it_mesos=not mesos.is_alive()))
            break
        if not relay.is_alive():
            log.error("Relay died.  Check logs to see why.")
            break
        if not mesos.is_alive():
            log.error(
                "Mesos Scheduler died and didn't notify me of its exception."
                "  This may be a code bug.  Check logs.")
            break
    relay.terminate()
    mesos.terminate()
    sys.exit(1)


def init_relay(ns_relay, mesos_ready):
    log.debug('Relay waiting to start until mesos framework is registered')
    mesos_ready.acquire()
    mesos_ready.wait()
    log.debug('Relay notified that mesos framework is registered')
    relay_main(ns_relay)


def init_mesos_scheduler(ns, MV, exception_sender, mesos_ready):
    import mesos.interface
    from mesos.interface import mesos_pb2
    try:
        import mesos.native
    except ImportError:
        log.error(
            "Oops! Mesos native bindings are not installed.  You can download"
            " these binaries from mesosphere.")
        raise

    log.info('starting mesos scheduler')

    # build framework
    framework = mesos_pb2.FrameworkInfo()
    framework.user = ""  # Have Mesos fill in the current user.
    framework.name = "Relay.Mesos Framework"
    framework.principal = "test-framework-python"  # TODO: what is this?

    # build driver
    driver = mesos.native.MesosSchedulerDriver(
        Scheduler(
            MV=MV, exception_sender=exception_sender, mesos_ready=mesos_ready,
            ns=ns),
        framework,
        ns.mesos_master)
    atexit.register(driver.stop)

    # run things
    status = 0 if driver.run() == mesos_pb2.DRIVER_STOPPED else 1
    driver.stop()  # Ensure that the driver process terminates.
    sys.exit(status)


build_arg_parser = at.build_arg_parser([
    at.group(
        "How does Relay.mesos affect your metric?",
        at.warmer(type=str, help=(
            "A bash command to run on a mesos slave."
            " A warmer should eventually increase metric values.")),
        at.cooler(type=str, help=(
            "A bash command to run on a mesos slave."
            " A cooler should eventually decrease metric values.")),
    ),
    at.group(
        "Relay.Mesos parameters",
        at.add_argument(
            '--mesos_master', default=os.getenv('RELAY_MESOS_MASTER'),
            help="URI to mesos master. We support whatever mesos supports"
        ),
        at.add_argument(
            '--task_resources', type=lambda x: x.split('='), nargs='*',
            default=dict(x.split('=') for x in os.getenv(
                'RELAY_TASK_RESOURCES', '').split(' ')), help=(
                "Specify what resources your task needs to execute.  These"
                " can be any recognized mesos resource"
                "  ie: --task_resources cpus=10 mem=30000"
            )),
        at.add_argument(
            '--docker_image', default=os.getenv('RELAY_DOCKER_IMAGE'), help=(
                "The name of a docker image if you wish to execute the"
                " warmer and cooler in it")),
        at.add_argument(
            '--max_failures', type=int,
            default=os.getenv('RELAY_MAX_FAILURES', -1), help=(
                "If tasks are failing too often, stop the driver and raise"
                " an error.  If given, this (always positive) number"
                " is a running count of (failures - successes - starting)"
                " tasks.  It is sensitive to many consecutive failures and"
                " will mostly ignore failures if a lot of tasks"
                " are starting or completing at once"
            )),
    ),
],
    description="Convert your Relay app into a Mesos Framework",
    parents=[relay_ap()], conflict_handler='resolve')


if __name__ == '__main__':
    NS = build_arg_parser().parse_args()
    main(NS)
