from __future__ import annotations
from typing import Self, TypeAlias, TYPE_CHECKING
import argparse
import asyncio
import concurrent.futures
import numpy as np
import rtlsdr

RtlSdr: TypeAlias = rtlsdr.rtlsdraio.RtlSdrAio


from common import SamplesT, SampleConfig, ProcessConfig
from sample_processor import SampleProcessor


class SampleReader:
    sdr: RtlSdr|None = None
    """RtlSdr instance"""

    aio_qsize: int = 1000

    sample_config: SampleConfig

    def __init__(
        self,
        sample_config: SampleConfig,
        buffer: SampleBuffer|None = None
    ):
        self.sample_config = sample_config
        self._buffer = buffer
        self._running_sync = False
        self._running_async = False
        self.aio_queue: asyncio.Queue[SamplesT|None] = asyncio.Queue(maxsize=self.aio_qsize)
        self._read_future: asyncio.Future|None = None
        self._aio_streaming = False
        self._aio_loop: asyncio.AbstractEventLoop|None = None
        self._callback_tasks: set[asyncio.Task] = set()
        self._callback_futures: set[concurrent.futures.Future] = set()
        self._wrapped_futures: set[asyncio.Future] = set()
        self._cleanup_task: asyncio.Task|None = None

    @property
    def sample_rate(self): return self.sample_config.sample_rate

    @property
    def center_freq(self): return self.sample_config.center_freq

    @property
    def num_samples(self): return self.sample_config.read_size

    @property
    def gain(self): return self.sample_config.gain


    @property
    def gain_values_db(self) -> list[float]:
        """List of possible values for :attr:`gain` in dB
        (instead of "tenths of dB")
        """
        if self.sdr is None:
            raise RuntimeError('SampleReader not open')
        return [v / 10 for v in self.sdr.gain_values]

    @property
    def buffer(self) -> SampleBuffer|None:
        return self._buffer
    @buffer.setter
    def buffer(self, value: SampleBuffer|None):
        if value is self._buffer:
            return
        if self._aio_streaming:
            raise RuntimeError('cannot change buffer while streaming')
        self._buffer = value

    def read_samples(self) -> SamplesT:
        """Read :attr:`num_samples` from the device
        """
        self._ensure_sync()
        if self.sdr is None:
            raise RuntimeError('SampleReader not open')
        samples = self.sdr.read_samples(self.num_samples)
        if TYPE_CHECKING:
            # This is just because `read_samples()`` can return a list
            # if numpy isn't installed
            assert isinstance(samples, np.ndarray)
        return samples

    async def open_stream(self):
        self._ensure_async()
        if self.num_samples % 512 != 0:
            raise ValueError('num_samples (chunk size) must be a multiple of 512')
        if self.sdr is None:
            raise RuntimeError('SampleReader not open')
        assert self._read_future is None
        loop = asyncio.get_running_loop()
        self._aio_loop = loop
        self._aio_streaming = True
        fut = loop.run_in_executor(
            None,
            self.sdr.read_samples_async,
            self._async_callback,
            self.num_samples,
        )
        self._read_future = asyncio.ensure_future(fut)
        self._cleanup_task = asyncio.create_task(self._bg_task_loop())

    async def close_stream(self):
        if not self._aio_streaming:
            return
        self._aio_streaming = False
        if self.sdr is not None:
            self.sdr.cancel_read_async()
        t = self._cleanup_task
        self._cleanup_task = None
        if t is not None:
            await t
        t = self._read_future
        self._read_future = None
        if t is not None:
            try:
                await t
            except Exception as exc:
                print(exc)
                print('fixme')
        while self.aio_queue.qsize() > 0:
            try:
                _ = self.aio_queue.get_nowait()
                self.aio_queue.task_done()
            except asyncio.QueueEmpty:
                break
        await self.aio_queue.put(None)
        self._wrap_futures()
        if len(self._wrapped_futures):
            await asyncio.gather(*self._wrapped_futures)


    def _async_callback(self, samples: SamplesT, *args):
        if not self._aio_streaming:
            return

        async def add_to_queue(samples: SamplesT):
            q = self.buffer if self.buffer is not None else self.aio_queue
            try:
                if self.buffer is None:
                    self.aio_queue.put_nowait(samples)
                else:
                    await self.buffer.put_nowait(samples)
            except asyncio.QueueFull:
                print(f'buffer overrun: {q.qsize()=}')

        if self._aio_loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(
            add_to_queue(samples),
            self._aio_loop,
        )
        self._callback_futures.add(fut)

    def _wrap_futures(self):
        fut: concurrent.futures.Future
        for fut in self._callback_futures.copy():
            self._wrapped_futures.add(asyncio.wrap_future(fut))
            self._callback_futures.discard(fut)

    async def _wait_for_futures(self, timeout: float = .01):
        done: set[asyncio.Future]
        done, pending = await asyncio.wait(
            self._wrapped_futures,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        for fut in done:
            self._wrapped_futures.discard(fut)

    async def _bg_task_loop(self):
        while self._aio_streaming:
            self._wrap_futures()
            if len(self._wrapped_futures):
                await self._wait_for_futures()
            if not len(self._wrapped_futures) and not len(self._callback_futures):
                await asyncio.sleep(.1)


    def _ensure_sync(self):
        if self._running_async:
            raise RuntimeError('Currently in async mode')

    def _ensure_async(self):
        if self._running_sync:
            raise RuntimeError('Currently in sync mode')

    def open(self):
        """Open the device and set all necessary parameters
        """
        self._ensure_sync()
        self._running_sync = True
        self._open()

    def _open(self):
        assert self.sdr is None
        if TYPE_CHECKING:
            # NOTE: Another workaround for the above TYPE_CHECKING stuff
            #       (just ignore for now)
            assert RtlSdr is not None
        sdr = self.sdr = RtlSdr()
        sdr.sample_rate = self.sample_rate
        sdr.center_freq = self.center_freq
        sdr.gain = self.gain

        # NOTE: Just for debug purposes. This might help with your gain issue
        print(f'{sdr.sample_rate=}, {sdr.center_freq=}, {sdr.gain=}')
        print(f'{self.gain_values_db=}')

    def close(self):
        """Close the device if it's currently open
        """
        self._ensure_sync()
        self._close()
        self._running_sync = False

    def _close(self):
        sdr = self.sdr
        if sdr is None:
            return
        self.sdr = None
        sdr.close()

    async def aopen(self):
        self._ensure_async()
        self._running_async = True
        self._open()

    async def aclose(self):
        self._ensure_async()
        try:
            await self.close_stream()
        finally:
            self._close()
        self._running_async = False

    # NOTE: These two methods allow use as a context manager:
    #       with SampleReader() as reader:
    #           ...
    #
    #       This way it will always close when the program does
    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

    async def __aenter__(self) -> Self:
        await self.aopen()
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def __anext__(self) -> SamplesT:
        if not self._aio_streaming:
            raise StopAsyncIteration
        if self.buffer is not None:
            raise RuntimeError('cannot use async for if SampleBuffer is set')
        samples = await self.aio_queue.get()
        self.aio_queue.task_done()
        if samples is None:
            raise StopAsyncIteration
        return samples

    def __aiter__(self):
        if self.buffer is not None:
            raise RuntimeError('cannot use async for if SampleBuffer is set')
        return self



class SampleBuffer:
    """Buffer for samples with thread-safe reads and writes

    Behavior is similar to :class:`queue.Queue`, with the exception of
    the ``task_done()`` and ``join()`` methods (which are not present).
    """

    maxsize: int
    """The maximum length for the buffer. If less than or equal to zero, the
    buffer length is infinite
    """

    def __init__(self, maxsize: int = 0) -> None:
        self.maxsize = maxsize
        self._samples: SamplesT = np.zeros(0, dtype=np.complex128)
        self._lock: asyncio.Lock = asyncio.Lock()
        self._notify_w = asyncio.Condition(self._lock)
        self._notify_r = asyncio.Condition(self._lock)

    async def put(
        self,
        samples: SamplesT,
        block: bool = True,
        timeout: float|None = None
    ):
        """Append new samples to the end of the buffer, blocking if necessary

        If *block* is True and *timeout* is ``None``, block if necessary until
        enough space is available to write the samples. If *timeout* is given,
        blocks at most *timeout* seconds and raises :class:`~asyncio.QueueFull`
        if no space was available during that time.

        Otherwise (if *block* is False), write the samples if enough space is
        immediately available (raising :class:`~asyncio.QueueFull` if necessary)


        Raises:
            QueueFull: If a timeout occurs waiting for available write space
        """

        async with self._lock:
            new_size = len(self) + samples.size
            if self.maxsize > 0 and new_size > self.maxsize:
                if not block:
                    raise asyncio.QueueFull()
                def can_write():
                    return new_size <= self.maxsize
                if timeout is not None:
                    try:
                        async with asyncio.timeout(timeout):
                            await self._notify_w.wait_for(can_write)
                    except asyncio.TimeoutError:
                        raise asyncio.QueueFull()
                else:
                    await self._notify_w.wait_for(can_write)
            self._samples = np.concatenate((self._samples, samples))
            self._notify_r.notify_all()

    async def put_nowait(self, samples: SamplesT):
        """Equivalent to ``put(samples, block=False)``
        """
        await self.put(samples, block=False)

    async def get(
        self,
        count: int,
        block: bool = True,
        timeout: float|None = None
    ) -> SamplesT:
        """Get *count* number of samples and remove them from the buffer

        If *block* is True and *timeout* is ``None``, block if necessary for
        enough samples to exist on the buffer.  If *timeout* is given,
        blocks at most *timeout* seconds and raises :class:`~asyncio.QueueEmpty`
        if no samples were available during that time.

        Otherwise (if *block* is False), return the samples if immediately
        available (raising :class:`~asyncio.QueueEmpty`) if necessary)

        Raises:
            QueueEmpty: If a timeout occurs waiting for samples
        """
        def has_enough_samples():
            return len(self) >= count

        async with self._lock:
            if not has_enough_samples():
                if not block:
                    raise asyncio.QueueEmpty()
                if timeout is not None:
                    try:
                        async with asyncio.timeout(timeout):
                            await self._notify_w.wait_for(has_enough_samples)
                    except asyncio.TimeoutError:
                        raise asyncio.QueueEmpty()
                else:
                    await self._notify_r.wait_for(has_enough_samples)
            samples = self._samples[:count]
            self._samples = self._samples[count:]
            self._notify_w.notify_all()
            return samples

    async def get_nowait(self, count: int) -> SamplesT:
        """Equivalent to ``get(count, block=False)``
        """
        return await self.get(count, block=False)

    def qsize(self) -> int:
        return len(self)

    def empty(self) -> bool:
        return len(self) == 0

    def full(self) -> bool:
        if self.maxsize > 0:
            return len(self) >= self.maxsize
        return False

    def __len__(self) -> int:
        return self._samples.size



def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        '-r', '--read-only',
        dest='read_only',
        action='store_true',
        help='Read samples from the device and save to file',
    )
    p.add_argument(
        '-o', '--outfile',
        dest='outfile',
        help='Filename to save samples to (only in "-r/--read-only" mode)'
    )
    p.add_argument(
        '-m', '--max-samples',
        dest='max_samples',
        type=int,
        help='Number of samples to read when in "-r/--read-only" mode'
    )
    p.add_argument(
        '-c', '--chunk-size',
        dest='chunk_size',
        type=int,
        default=SampleConfig().read_size,
        help='Chunk size for sdr.read_samples',
    )
    args = p.parse_args()
    if args.read_only:
        assert args.outfile is not None
        assert args.max_samples is not None
        asyncio.run(
            run_readonly(args.outfile, args.chunk_size, args.max_samples)
        )
    else:
        asyncio.run(
            run_main(args.chunk_size)
        )


async def run_readonly(outfile: str, chunk_size: int, max_samples: int):
    nrows = max_samples // chunk_size
    if nrows * chunk_size < max_samples:
        nrows += 1
    sample_config = SampleConfig(read_size=chunk_size)
    samples = np.zeros((nrows, chunk_size), dtype=np.complex128)
    reader = SampleReader(sample_config=sample_config)

    async with reader:
        await reader.open_stream()
        i = 0
        count = 0
        async for _samples in reader:
            if count == 0:
                print(f'{_samples.size=}')
            samples[i,:] = _samples
            count += _samples.size
            # print(f'{i}\t{reader.aio_queue.qsize()=}\t{count=}')
            i += 1
            if count >= max_samples:
                break
    samples = samples.flatten()[:max_samples]
    np.save(outfile, samples)


async def run_main(chunk_size: int):
    sample_config = SampleConfig(read_size=chunk_size)
    process_config = ProcessConfig(sample_config=sample_config)
    reader = SampleReader(sample_config=sample_config)
    processor = SampleProcessor(config=process_config)
    buffer = SampleBuffer(maxsize=process_config.num_samples_to_process * 3)
    reader.buffer = buffer

    async with reader:
        await reader.open_stream()
        while True:
            samples = await buffer.get(processor.num_samples_to_process)
            await asyncio.to_thread(processor.process, samples)


if __name__ == '__main__':
    main()
