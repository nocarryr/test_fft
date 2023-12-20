from __future__ import annotations
from typing import TYPE_CHECKING
import asyncio

import numpy as np
import numpy.typing as npt
from scipy import signal

from common import SamplesT, FloatArray
if TYPE_CHECKING:
    from sample_reader import SampleBuffer


class SampleProcessor:
    threshold: float = 0.9
    freq_offset: float = -43.8e4 #Hz
    beep_duration: float = 0.017 # seconds

    num_samples_to_process: int = int(1.024e6)
    """Number of samples needed to process"""

    sample_rate: float
    stateful_index: int

    def __init__(self, sample_rate: float) -> None:
        self.sample_rate = sample_rate
        self.stateful_index = 0
        self._time_array = None
        self._fir = None
        self._phasor = None

    @property
    def time_array(self) -> FloatArray:
        t = self._time_array
        if t is None:
            t = np.arange(self.num_samples_to_process)/self.sample_rate
            self._time_array = t
        return t

    @property
    def fir(self) -> FloatArray:
        h = self._fir
        if h is None:
            h = self._fir = signal.firwin(501, 0.02, pass_zero=True)
        return h

    @property
    def phasor(self) -> npt.NDArray[np.complex128]:
        p = self._phasor
        if p is None:
            t = self.time_array
            p = np.exp(2j*np.pi*t*self.freq_offset)
            self._phasor = p
        return p

    @property
    def fft_size(self) -> int:
        # this makes sure there's at least 1 full chunk within each beep
        return int(self.beep_duration * self.sample_rate / 2)

    def process(self, samples: SamplesT):
        # fft_size = self.fft_size
        # f = np.linspace(self.sample_rate/-2, self.sample_rate/2, fft_size)
        # num_ffts = len(samples) // fft_size # // is an integer division which rounds down
        # fft_thresh = 0.1
        # beep_freqs = []
        # for i in range(num_ffts):
        #     fft = np.abs(np.fft.fftshift(np.fft.fft(samples[i*fft_size:(i+1)*fft_size]))) / fft_size
        #     if np.max(fft) > fft_thresh:
        #         beep_freqs.append(np.linspace(self.sample_rate/-2, self.sample_rate/2, fft_size)[np.argmax(fft)])
        #     plt.plot(f,fft)
        # #print(beep_freqs)
        # #plt.show()

        t = self.time_array
        samples = samples * self.phasor
        h = self.fir
        samples = np.convolve(samples, h, 'valid')
        samples = samples[::100]
        sample_rate = self.sample_rate/100
        samples = np.abs(samples)
        samples = np.convolve(samples, [1]*10, 'valid')/10
        max_samp = np.max(samples)
        # samples /= np.max(samples)
        #print(f"max sample : {max_samp}")
        #plt.plot(samples)
        #plt.show()

        # Get a boolean array for all samples higher or lower than the threshold
        low_samples = samples < self.threshold
        high_samples = samples >= self.threshold

        # Compute the rising edge and falling edges by comparing the current value to the next with
        # the boolean operator & (if both are true the result is true) and converting this to an index
        # into the current array
        rising_edge_idx = np.nonzero(low_samples[:-1] & np.roll(high_samples, -1)[:-1])[0]
        falling_edge_idx = np.nonzero(high_samples[:-1] & np.roll(low_samples, -1)[:-1])[0]

        # This would need to be handled more gracefully with a stateful
        # processing (e.g. saving samples at the end if the pulse is in-between two processing blocks)
        # Remove stray falling edge at the start
        if len(rising_edge_idx) == 0 or len(falling_edge_idx) == 0:
            return
        #print(f"passed len test for idx's")
        if rising_edge_idx[0] > falling_edge_idx[0]:
            falling_edge_idx = falling_edge_idx[1:]

        # Remove stray rising edge at the end
        if rising_edge_idx[-1] > falling_edge_idx[-1]:
            rising_edge_idx = rising_edge_idx[:-1]

        rising_edge_diff = np.diff(rising_edge_idx)
        time_between_rising_edge = sample_rate / rising_edge_diff * 60

        pulse_widths = falling_edge_idx - rising_edge_idx
        rssi_idxs = list(np.arange(r, r + p) for r, p in zip(rising_edge_idx, pulse_widths))
        rssi = [np.mean(samples[r]) * max_samp for r in rssi_idxs]

        for t, r in zip(time_between_rising_edge, rssi):
            print(f"BPM: {t:.02f}")
            print(f"rssi: {r:.02f}")
        self.stateful_index += len(samples)
        print(f"stateful index : {self.stateful_index}")
