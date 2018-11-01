"""Tools for working with async tasks
"""
import asyncio
import queue
import itertools
import logging

EOS_MARKER = object()
log = logging.getLogger(__name__)


async def async_q2q_map(func, q_in, q_out,
                        eos_marker=EOS_MARKER,
                        eos_passthrough=True,
                        **kwargs):
    """Like `map` but operating on values from/to queues.

       Roughly equivalent to:

       > while not end of stream:
       >    q_out.put(func(q_in.get(), **kwargs))

       Processing stops when `eos_marker` object is observed on input, by
       default `eos_marker` is passed through to output queue, but you can
       disable that.

       Calls `task_done()` method on input queue after result was copied to output queue.

       Assumption is that mapping function doesn't raise exceptions, instead it
       should return some sort of error object. If calling `func` does result
       in an exception it will be caught and logged but otherwise ignored.

       It is safe to have multiple consumers/producers reading/writing from the
       queues, although you might want to disable eos pass-through in those
       cases.

       func : Callable

       q_in: Input asyncio.Queue
       q_out: Output asyncio.Queue

       eos_marker: Value that indicates end of stream

       eos_passthrough: If True copy eos_marker to output queue before
                        terminating, if False then don't

    """
    while True:
        x = await q_in.get()

        if x is eos_marker:
            if eos_passthrough:
                await q_out.put(x)
            q_in.task_done()
            return

        err, result = (None, None)
        try:
            result = await func(x, **kwargs)
        except Exception as e:
            err = str(e)
            log.error("Uncaught exception: %s", err)

        if err is None:
            await q_out.put(result)

        q_in.task_done()


async def q2q_nmap(func,
                   q_in,
                   q_out,
                   nconcurrent,
                   eos_marker=EOS_MARKER,
                   eos_passthrough=True,
                   dt=0.01,
                   loop=None):
    """Pump data from synchronous queue to another synchronous queue via a worker
       pool of async `func`s. Allow upto `nconcurrent` concurrent `func` tasks
       at a time.

                / [func] \
         q_in ->  [func]  >--> q_out
                \ [func] /


        - Order is not preserved.
        - func is expected not to raise exceptions
    """
    def safe_get(src):
        try:
            x = src.get_nowait()
            return (x, True)
        except queue.Empty:
            return (None, False)

    def safe_put(x, dst):
        try:
            dst.put_nowait(x)
        except queue.Full:
            return False

        return True

    async def push_to_dst(x, dst, dt):
        while not safe_put(x, dst):
            await asyncio.sleep(dt)

    async def intake_loop(src, dst, dt):
        while True:
            x, ok = safe_get(src)
            if not ok:
                await asyncio.sleep(dt)
            elif x is eos_marker:
                src.task_done()
                break
            else:
                await dst.put(x)
                src.task_done()

        for _ in range(nconcurrent):
            await dst.put(eos_marker)

        await dst.join()

    async def output_loop(src, dst, dt):
        while True:
            x = await src.get()

            if x is eos_marker:
                src.task_done()
                break

            await push_to_dst(x, dst, dt)
            src.task_done()

    aq_in = asyncio.Queue(nconcurrent*2)
    aq_out = asyncio.Queue(aq_in.maxsize)

    #                 / [func] \
    # q_in -> aq_in ->  [func]  >--> aq_out -> q_out
    #                 \ [func] /

    # Launch async worker pool: aq_in ->[func]-> aq_out
    for _ in range(nconcurrent):
        asyncio.ensure_future(async_q2q_map(func, aq_in, aq_out,
                                            eos_marker=eos_marker,
                                            eos_passthrough=False),
                              loop=loop)

    # Pump from aq_out -> q_out (async to sync interface)
    asyncio.ensure_future(output_loop(aq_out, q_out, dt), loop=loop)

    # Pump from q_in -> aq_in (sync to async interface)
    await intake_loop(q_in, aq_in, dt)

    # by this time all input items have been mapped through func and are in aq_out

    # terminate output pump
    await aq_out.put(eos_marker)  # tell output_loop to stop
    await aq_out.join()           # wait for ack, all valid data is in `q_out` now

    # finally push through eos_marker unless asked not too
    if eos_passthrough:
        await push_to_dst(eos_marker, q_out, dt)


class AsyncWorkerPool(object):
    """NxM worker pool, maintains N threads running M concurrent async tasks
    each. Provides synchronous access via iterators.
    """

    def __init__(self,
                 nthreads=1,
                 tasks_per_thread=1,
                 max_buffer=None):
        from concurrent.futures import ThreadPoolExecutor

        if max_buffer is None:
            max_buffer = min(100, nthreads*tasks_per_thread*2)

        self._pool = ThreadPoolExecutor(nthreads)
        self._loops = [asyncio.new_event_loop() for _ in range(nthreads)]
        self._nthreads = nthreads
        self._default_tasks_per_thread = tasks_per_thread
        self._q1 = queue.Queue(max_buffer)
        self._q2 = queue.Queue(max_buffer)

    def _run_worker_thread(self, func, loop, nconcurrent):
        q2q_task = q2q_nmap(func, self._q1, self._q2,
                            nconcurrent,
                            eos_passthrough=False,
                            loop=loop)

        loop.run_until_complete(q2q_task)

    def map(self, func, its, nconcurrent=None, **kwargs):
        from time import sleep
        dt = 0.01

        proc = func if len(kwargs) == 0 else (lambda x: func(x, **kwargs))

        if nconcurrent is None:
            nconcurrent = self._default_tasks_per_thread

        workers = [self._pool.submit(self._run_worker_thread, proc, loop, nconcurrent)
                   for loop in self._loops]

        feedq = self._q1
        outq = self._q2

        # max_tasks_at_once = len(workers)*nconcurrent

        # append EOS_MARKER for every worker thread
        its = itertools.chain(its, [EOS_MARKER]*len(workers))
        nin = 0
        nout = 0

        for x in its:
            while feedq.full():
                n = 0
                while feedq.full() and not outq.empty():
                    n += 1
                    nout += 1
                    yield outq.get_nowait()

                if feedq.full() and n == 0:
                    sleep(dt)  # Avoid busy loop, feedq is full, but outq was empty

            feedq.put_nowait(x)
            nin += 1

            # flush upto 10 results for every input, so that we can catch up,
            # but at the same time don't neglect input side either. We can
            # probably be smarter and monitor tasks in flight using nin/nout
            # and decide based on that.
            for _ in range(10):
                if outq.empty():
                    break
                nout += 1
                yield outq.get_nowait()

        # We are done with input, need to flush output queue now. We can't just
        # wait for workers to finish as they might be blocked by output queue
        # being full, we are "done" when both conditions hold:
        #  1. worker threads have finished
        #  2. outq is empty and will stay empty because of (1.)

        def prune_workers(ww):
            return [w for w in ww if not w.done()]

        while not (len(workers) == 0 and outq.empty()):
            n = 0
            while not outq.empty():
                nout += 1
                n += 1
                yield outq.get_nowait()

            workers = prune_workers(workers)

            if len(workers) and n == 0:
                sleep(dt)  # Avoid busy looping, outq was empty but workers are still running


################################################################################
# tests below
################################################################################


def test_q2q_map():
    async def proc(x):
        await asyncio.sleep(0.01)
        return (x, x)

    loop = asyncio.new_event_loop()

    def run(**kwargs):
        q1 = asyncio.Queue(10)
        q2 = asyncio.Queue(10)

        for i in range(4):
            q1.put_nowait(i)
        q1.put_nowait(EOS_MARKER)

        async def run_test(**kwargs):
            await async_q2q_map(proc, q1, q2, **kwargs)
            await q1.join()

            xx = []
            while not q2.empty():
                xx.append(q2.get_nowait())
            return xx

        return loop.run_until_complete(run_test(**kwargs))

    expect = [(i, i) for i in range(4)]
    assert run() == expect + [EOS_MARKER]
    assert run(eos_passthrough=False) == expect

    loop.close()


def test_q2qnmap():
    from concurrent.futures import ThreadPoolExecutor
    import random
    from types import SimpleNamespace

    async def proc(x, state, delay=0.1):
        state.active += 1

        delay = random.uniform(0, delay)
        await asyncio.sleep(delay)

        state.max_active = max(state.active, state.max_active)
        state.active -= 1
        return (x, x)

    def run_producer(n, q, eos_marker):
        for i in range(n):
            q.put(i)
        q.put(eos_marker)
        q.join()

    def run_consumer(q, eos_marker):
        xx = []
        while True:
            x = q.get()
            q.task_done()
            xx.append(x)
            if x is eos_marker:
                break

        return xx

    wk_pool = ThreadPoolExecutor(max_workers=2)
    src = queue.Queue(3)
    dst = queue.Queue(3)

    # first do self test of consumer/producer
    N = 100

    wk_pool.submit(run_producer, N, src, EOS_MARKER)
    xx = wk_pool.submit(run_consumer, src, EOS_MARKER)
    xx = xx.result()

    assert len(xx) == N + 1
    assert len(set(xx) - set(range(N)) - set([EOS_MARKER])) == 0
    assert src.qsize() == 0

    loop = asyncio.new_event_loop()

    def run(N, nconcurrent, delay, eos_passthrough=True):
        async def run_test(func, N, nconcurrent):
            wk_pool.submit(run_producer, N, src, EOS_MARKER)
            xx = wk_pool.submit(run_consumer, dst, EOS_MARKER)
            await q2q_nmap(func, src, dst, nconcurrent, eos_passthrough=eos_passthrough)

            if eos_passthrough is False:
                dst.put(EOS_MARKER)

            return xx.result()

        state = SimpleNamespace(active=0, max_active=0)
        func = lambda x: proc(x, delay=delay, state=state)
        return state, loop.run_until_complete(run_test(func, N, nconcurrent))

    expect = set([(x, x) for x in range(N)] + [EOS_MARKER])

    st, xx = run(N, 20, 0.1)
    assert len(xx) == N + 1
    assert 1 < st.max_active <= 20
    assert set(xx) == expect

    st, xx = run(N, 4, 0.01)
    assert len(xx) == N + 1
    assert 1 < st.max_active <= 4
    assert set(xx) == expect

    st, xx = run(N, 4, 0.01, eos_passthrough=False)
    assert len(xx) == N + 1
    assert 1 < st.max_active <= 4
    assert set(xx) == expect


def test_async_work_pool():
    async def proc(x, delay):
        await asyncio.sleep(delay)
        return (x, x)

    pool = AsyncWorkerPool(2, 10, max_buffer=30)

    xx = list(pool.map(proc, range(100), delay=0.1))

    expect = set((x, x) for x in range(100))
    assert len(xx) == 100
    assert expect == set(xx)