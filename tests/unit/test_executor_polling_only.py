from types import SimpleNamespace

from main_executor import Executor


class Worker:
    def __init__(self) -> None:
        self.ran = False
        self.stopped = False
        self.executor_id = "exec-test"

    def announce_account_mode(self):
        return None

    def run(self) -> None:
        self.ran = True

    def stop(self, *, timeout: float = 5.0) -> None:
        self.stopped = True


class Broker:
    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass


class Reconciliation:
    def reconcile_all(self):
        return []


def test_executor_run_does_not_call_removed_snapshot_listener() -> None:
    executor = Executor.__new__(Executor)
    executor.config = SimpleNamespace(symbols=("XAUUSD",), state_heartbeat_seconds=5.0)
    executor.broker = Broker()
    executor.repository = object()
    executor.heartbeat = None
    executor.controls = None
    executor.worker = Worker()
    executor.reconciliation = Reconciliation()

    executor.run()

    assert executor.worker.ran is True
    assert executor.worker.stopped is True
