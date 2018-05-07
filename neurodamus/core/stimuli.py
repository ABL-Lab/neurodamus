from __future__ import absolute_import
from . import Neuron
from .random import RNG
import logging


class CurrentSource(object):
    _all_sources = []

    def __init__(self, base_amp=0.0, rng=None):
        """
        Creates a new signal source, which can create composed signals
        Args:
            base_amp: The base (resting) amplitude of the signal (Default: 0)
            rng: The Random Number Generator. Used in the Noise functions
        """
        h = Neuron.h
        self.stim_vec = h.Vector()
        self.time_vec = h.Vector()
        self._cur_t = 0
        self._clamps = set()
        self._all_sources.append(self)
        self._base_amp = base_amp
        self._rng = rng

    class _Clamp:
        def __init__(self, cell_section, position=0.5, clamp_container=None,
                     stim_vec_mode=True, time_vec=None, stim_vec=None,
                     **clamp_params):
            self.clamp = Neuron.h.IClamp(position, sec=cell_section)
            if stim_vec_mode:
                assert time_vec is not None and stim_vec is not None
                self.clamp.dur = time_vec[-1]
                stim_vec.play(self.clamp._ref_amp, time_vec, 1)
            else:
                for param, val in clamp_params.items():
                    setattr(self.clamp, param, val)
            # Clamps must be kept otherwise they are garbage-collected
            self._all_clamps = clamp_container
            clamp_container.add(self)

        def detach(self):
            """Detaches a clamp from a cell, destroying it"""
            self._all_clamps.discard(self)
            del self.clamp  # Force del on the clamp (there might be references to self)

    def attach_to(self, section, position=0.5):
        return CurrentSource._Clamp(section, position, self._clamps, True,
                                    self.time_vec, self.stim_vec)

    def reset(self):
        self.stim_vec.resize(0)
        self.time_vec.resize(0)

    def _add_point(self, amp):
        """Appends a single point to the time-voltage source.
        Note: It doesnt advance time, not supposed to be called directly
        """
        self.time_vec.append(self._cur_t)
        self.stim_vec.append(amp)

    def delay(self, duration):
        """Increments the ref time so that the next created signal is delayed
        """
        self._cur_t += duration
        return self

    def add_segment(self, amp, duration, amp2=None):
        """Sets a linear voltage for a certain duration
        If amp2 is None (default) then we have constant voltage
        """
        self._add_point(amp)
        self.delay(duration)
        self._add_point(amp if amp2 is None else amp2)
        return self

    def add_pulse(self, max_amp, duration, **kw):
        """Adds a pulse.
        A pulse is characterized by raising from a base voltage, default 0, for a certain duration.
        """
        base_amp = kw.get("base_amp", self._base_amp)
        self._add_point(base_amp)
        self.add_segment(max_amp, duration)
        self._add_point(base_amp)
        return self

    def add_ramp(self, amp1, amp2, duration, **kw):
        """Adds a ramp.
        A ramp is characterized by a pulse whose peak changes uniformly during its length.
        """
        base_amp = kw.get("base_amp", self._base_amp)
        self._add_point(base_amp)
        self.add_segment(amp1, duration, amp2)
        self._add_point(base_amp)
        return self

    def add_train(self, amp, frequency, pulse_duration, total_duration, **kw):
        """Stimulus with repeated pulse injections at a specified frequency.
        Args:
          amp: the amplitude of a each pulse
          frequency: determines the number of pulses per second (hz)
          pulse_duration: the duration of a single pulse (peak time) (ms)
          total_duration: duration of the whole train (ms)
          base_amp: The base amplitude
        """
        base_amp = kw.get("base_amp", self._base_amp)
        tau = 1000 // frequency
        delay = tau - pulse_duration
        number_pulses = total_duration // tau
        for _ in range(number_pulses):
            self.add_pulse(amp, pulse_duration, base_amp=base_amp)
            self.delay(delay)

        # Add extra pulse if fits
        remaining_time = total_duration % tau
        if pulse_duration <= remaining_time:
            self.add_pulse(amp, pulse_duration, base_amp=base_amp)
            self.delay(min(delay, remaining_time - pulse_duration))
        # Last point
        self._add_point(base_amp)
        return self

    def add_sin(self, amp, total_duration, freq, step=0.025):
        """ Builds a sinusoidal signal
        Args:
            amp: The max amplitude of the wave
            total_duration: Total duration, in ms
            freq: The wave frequency, in Hz
            step: The step, in ms (default: 0.025)
        """
        n_steps = total_duration // step + 1

        t_vec = Neuron.h.Vector(n_steps)
        t_vec.indgen(step)
        self.time_vec.append(t_vec)

        v_vec = Neuron.h.Vector(n_steps)
        v_vec.sin(freq, .0, step)
        v_vec.mul(amp)
        self.stim_vec.append(v_vec)
        return self

    def add_sinspec(self, start, dur):
        raise NotImplementedError()

    def add_pulses(self, pulse_duration, amp, *more_amps, **kw):
        """Appends a set of voltages without returning to zero
           Each voltage is applied 'dur' time\
        Args:
          pulse_duration: The duration of each pulse
          amp: The amplitude of the first pulse
          *more_amps: 2nd, 3rd, ... pulse amplitudes
          **kw: Additional params:
            - base_amp [default: 0]
        """
        # First and last are base_amp
        base_amp = kw.get("base_amp", self._base_amp)
        self._add_point(base_amp)
        self.add_segment(amp, pulse_duration)
        for amp in more_amps:
            self.add_segment(amp, pulse_duration)
        self._add_point(base_amp)
        return self

    def add_noise(self, mean, variance, duration, dt=0.5):
        rng = self._rng or RNG()  # Creates a default RNG
        if not self._rng:
            logging.warn("Using a default RNG for noise generation")
        rng.normal(mean, variance)
        tvec = Neuron.h.Vector()
        tvec.indgen(self._cur_t, self._cur_t + duration, dt)
        svec = Neuron.h.Vector(len(tvec))
        svec.setrand(rng)
        svec.x[0] = 0
        self.time_vec.append(tvec)
        self.stim_vec.append(svec)
        self._cur_t += duration
        self._add_point(.0)
        return self

    # PLOTTING
    def plot(self, ylims=None):
        from matplotlib import pyplot
        fig = pyplot.figure()
        ax = fig.add_subplot(1, 1, 1)  # (nrows, ncols, axnum)
        ax.plot(self.time_vec, self.stim_vec, label="Stimulous amplitude")
        ax.legend()
        if ylims:
            ax.set_ylim(*ylims)
        fig.show()

    # ==== Helpers =====
    @classmethod
    def pulse(cls, max_amp, duration, base_amp=.0, delay=0):
        return cls(base_amp).delay(delay).add_pulse(max_amp, duration)

    @classmethod
    def ramp(cls, amp1, amp2, duration, base_amp=.0, delay=0):
        return cls(base_amp).delay(delay).add_ramp(amp1, amp2, duration)

    @classmethod
    def train(cls, amp, frequency, pulse_duration, total_duration, base_amp=.0, delay=0):
        return cls(base_amp).delay(delay).add_train(amp, frequency, pulse_duration, total_duration)

    @classmethod
    def sin(cls, amp, total_duration, freq, step=0.025, delay=0, base_amp=.0):
        return cls(base_amp).delay(delay).add_sin(amp, total_duration, freq, step)

    # Operations
    def __add__(self, other):
        """# Adds signals. Two added signals sum amplitudes"""
        raise NotImplementedError("Adding signals is not available yet")

    # Constant has a special attach_to and doesnt share any composing method
    class Constant:
        """Class implementing a minimal IClamp for a Constant current."""
        _clamps = set()

        def __init__(self, amp, duration, delay=0):
            self._amp = amp
            self._dur = duration
            self._delay = delay

        def attach_to(self, section, position=0.5):
            return CurrentSource._Clamp(section, position, self._clamps, False,
                                        amp=self._amp, delay=self._delay, dur=self._dur)


# EStim class is a derivative of TStim for stimuli with an extracelular electrode. The main
# difference is that it collects all elementary stimuli pulses and converts them using a
# VirtualElectrode object before it injects anything
#
# The stimulus is defined on the hoc level by using the addpoint function for every (step) change
# in extracellular electrode voltage. At this stage only step changes can be used. Gradual,
# i.e. sinusoidal changes will be implemented in the future
# After every step has been defined, you have to call initElec() to perform the frequency dependent
# transformation. This transformation turns e_electrode into e_extracellular at distance d=1 micron
# from the electrode. After the transformation is complete, NO MORE STEPS CAN BE ADDED!
# You can then use inject() to first scale e_extracellular down by a distance dependent factor
# and then vector.play() it into the currently accessed compartment
#
# TODO: 1. more stimulus primitives than step. 2. a dt of 0.1 ms is hardcoded. make this flexible!