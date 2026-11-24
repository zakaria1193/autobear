from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic


@contextmanager
def report_wait(stage, *, interval=15):
    start = monotonic()
    finished = Event()

    def report():
        while not finished.wait(interval):
            elapsed = int(monotonic() - start)
            print(f"{stage} still running ({elapsed}s elapsed).", flush=True)

    reporter = Thread(target=report, daemon=True)
    reporter.start()
    try:
        yield
    finally:
        finished.set()
        reporter.join()
    print(f"{stage} finished in {monotonic() - start:.1f}s.", flush=True)
