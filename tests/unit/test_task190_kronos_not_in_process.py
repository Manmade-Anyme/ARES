"""
Tests for TASK-190: keep Kronos inference out of the trading process.

TASK-186 moved the Kronos consumer in-process because nothing in the Fly
deployment ever launched TASK-184's separate process. That made an
informational number capable of killing the trading loop, and on 2026-07-20 it
did: signal #231 fired at 06:10:54Z, the consumer picked it up inside its
180-second recency window, and inference OOM-killed the machine. Fly restarted
it, main.py re-sent its Discord startup alert, the consumer saw #231 still
under 180s old, and the cycle repeated six times until the signal aged out.

Measured peak RSS for one forecast (`ru_maxrss`, horizon 45):

    context 1000 x 20 paths -> 3613 MB      <- shipped configuration
    context  500 x 20 paths -> 2126 MB
    context 1000 x  5 paths -> 1314 MB
    context 1000 x  2 paths ->  820 MB

Usable memory on the 768mb VM is ~710 MB and the trading baseline is ~340 MB,
so no setting yielding more than one sampled path fits. The consumer is
therefore not started; these tests pin that, because the failure is invisible
until a signal fires in production.
"""
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestKronosNotInTradingProcess(unittest.TestCase):

    def test_run_does_not_schedule_the_kronos_consumer(self):
        """
        The launcher is kept (TASK-186/187 still cover it, and it is the
        reference for rehoming Kronos onto its own machine) but must not be
        scheduled by run().
        """
        source = (REPO_ROOT / "main.py").read_text()
        run_body = source.split("async def run(")[1]
        # assertFalse, not assertNotIn — the latter dumps all of run() into the report.
        self.assertFalse(
            "create_task(_start_in_process_kronos_consumer" in run_body,
            "run() schedules the Kronos consumer again — see TASK-190; give it its "
            "own machine instead of a shared 768mb VM.",
        )


if __name__ == "__main__":
    unittest.main()
