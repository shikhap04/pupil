'''
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2018 Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
'''

import numpy as np
from plugin import Plugin
import os
import av
from bisect import bisect_left as bisect

import pyaudio as pa
import itertools
from threading import Timer
from time import monotonic

# logging
import logging
logger = logging.getLogger(__name__)


class Audio_Playback(Plugin):
    """Calibrate using a marker on your screen
    We use a ring detector that moves across the screen to 9 sites
    Points are collected at sites not between
    """

    def __init__(self, g_pool):
        super().__init__(g_pool)

        self.play = False
        self.pa_stream = None
        self.audio_sync = 0.
        self.audio_delay = 0.
        self.audio_container = None
        self.audio_stream = None
        self.next_audio_frame = None
        self.audio_start_pts = 0
        audio_file = os.path.join(self.g_pool.rec_dir, 'audio.mp4')
        if os.path.isfile(audio_file):
            self.audio_container = av.open(str(audio_file))
            try:
                self.audio_stream = next(s for s in self.audio_container.streams if s.type == 'audio')
                logger.debug("loaded audiostream: %s" % self.audio_stream)
            except StopIteration:
                self.audio_stream = None
                logger.debug("No audiostream found in media container")
        else:
            return
        if self.audio_stream is not None:
            self.audio_bytes_fifo = []
            audiots_path = os.path.splitext(audio_file)[0] + '_timestamps.npy'
            try:
                self.audio_timestamps = np.load(audiots_path)
            except IOError:
                self.audio_timestamps = None
                logger.warning("Could not load audio timestamps")
            self.next_audio_frame = self._next_audio_frame()
            self.audio_fifo = av.audio.fifo.AudioFifo()
            self.audio_resampler = av.audio.resampler.AudioResampler(format=self.audio_stream.format.packed,
                                                                     layout=self.audio_stream.layout,
                                                                     rate=self.audio_stream.rate)
            self.audio_paused = False
            af0, af1 = next(self.next_audio_frame), next(self.next_audio_frame)
            # Check pts

            self.audio_pts_rate = af0.samples  # af1.pts - af0.pts
            self.audio_start_pts = 0
            logger.info("audio_pts_rate = {} start_pts = {}".format(self.audio_pts_rate, self.audio_start_pts))

            print("**** Checking stream")
            for i, af in enumerate(self.next_audio_frame):
                fnum = i + 2
                if af.samples != af0.samples:
                    print("fnum {} samples = {}".format(fnum, af.samples))
                if af.pts != self.audio_idx_to_pts(fnum):
                    print("af.pts = {} fnum = {} idx2pts = {}".format(af.pts, fnum, self.audio_idx_to_pts(fnum)))
                if self.audio_timestamps[fnum] != self.audio_timestamps[0] + af.pts * self.audio_stream.time_base:
                    print("ts[0] + af.pts = {} fnum = {} timestamp = {}".format(
                        self.audio_timestamps[0] + af.pts * self.audio_stream.time_base, fnum, self.audio_timestamps[fnum]))
            print("**** Done")
            self.seek_to_audio_frame(0)

            logger.info("Audio file format {} chans {} rate {} framesize {} ".format(self.audio_stream.format,
                                                                                     self.audio_stream.channels,
                                                                                     self.audio_stream.rate,
                                                                                     self.audio_stream.frame_size))
            self.audio_start_time = 0
            self.audio_measured_latency = -1.

            def audio_callback(in_data, frame_count, time_info, status):
                cb_to_adc_time = time_info['output_buffer_dac_time'] - time_info['current_time']
                start_to_cb_time = monotonic() - self.audio_start_time
                if self.audio_measured_latency < 0:
                    self.audio_measured_latency = start_to_cb_time + cb_to_adc_time
                    lat_diff = self.audio_reported_latency - self.audio_measured_latency
                    self.audio_sync -= lat_diff
                    self.g_pool.seek_control.time_slew = self.audio_sync

                    print("Measured latency = {}".format(self.audio_measured_latency))

                    # print("Time diff {}".format(time_info['output_buffer_dac_time'] - time_info['current_time']))
                    # print("Callback delay from audio start {}".format(monotonic() - self.audio_start_time))
                if not self.play:
                    self.audio_paused = True
                    logger.info("audio cb abort 1")
                    return (None, pa.paAbort)
                try:
                    samples = self.audio_bytes_fifo.pop(0)
                    return (samples, pa.paContinue)
                except IndexError:
                    self.audio_paused = True
                    logger.info("audio cb abort 2")
                    return (None, pa.paAbort)

            try:
                self.pa = pa.PyAudio()
                self.pa_stream = self.pa.open(format=self.pa.get_format_from_width(self.audio_stream.format.bytes),
                                              channels=self.audio_stream.channels,
                                              rate=self.audio_stream.rate,
                                              frames_per_buffer=self.audio_stream.frame_size,
                                              stream_callback=audio_callback,
                                              output=True,
                                          start=False)
                logger.info("Audio output latency: {}".format(self.pa_stream.get_output_latency()))
                self.audio_sync = self.pa_stream.get_output_latency()
                self.audio_reported_latency = self.pa_stream.get_output_latency()

            except ValueError:
                self.pa_stream = None


    def get_audio_sync(self):
        # Audio has been started without delay
        if self.audio_measured_latency > 0:
            lat_diff = self.pa_stream.get_output_latency() - self.audio_measured_latency
            return self.audio_sync - lat_diff
        else:
            return self.audio_sync


    def _next_audio_frame(self):
        for packet in self.audio_container.demux(self.audio_stream):
            for frame in packet.decode():
                if frame:
                    yield frame
        raise StopIteration()


    def audio_idx_to_pts(self, idx):
        return idx * self.audio_pts_rate


    def seek_to_audio_frame(self, seek_pos):
        try:
            self.audio_stream.seek(self.audio_start_pts + self.audio_idx_to_pts(seek_pos), mode='time')
        except av.AVError as e:
            raise FileSeekError()
        else:
            self.next_audio_frame = self._next_audio_frame()
            self.audio_bytes_fifo.clear()


    def seek_to_frame(self, frame_idx):
        if self.audio_stream is not None:
            audio_idx = bisect(self.audio_timestamps, self.timestamps[frame_idx])
            print("audio_idx = {}, ts = {}".format(audio_idx, self.timestamps[frame_idx]))
            self.seek_to_audio_frame(audio_idx)


    def get_frame_index(self):
        raise NotImplementedError()


    def seek_to_prev_frame(self):
        raise NotImplementedError()


    def get_frame(self, frame_idx=-1):
        if self.pa_stream is not None and self.play:
            samples_written = 0
            if self.playback_speed == 1.:
                if (self.pa_stream.is_stopped() or self.audio_paused) and self.audio_delay <= 0.001:
                    if frame_idx == -1:
                        frame_idx = 0
                    playback_start_audio = monotonic()
                    audio_idx = bisect(self.audio_timestamps, self.timestamps[frame_idx])
                    self.seek_to_audio_frame(audio_idx)
                frames_chunk = itertools.islice(self.next_audio_frame, 10)
                for audio_frame_p in frames_chunk:
                    audio_frame = self.audio_resampler.resample(audio_frame_p)
                    self.audio_bytes_fifo.append(bytes(audio_frame.planes[0]))
                if (self.pa_stream.is_stopped() or self.audio_paused) and self.audio_delay <= 0.001:
                    if frame_idx == -1:
                        frame_idx = 0
                    playback_clock_delay = monotonic() - playback_start_audio
                    print("Delay from starting playback is {}".format(playback_clock_delay))
                    rt_delay = self.audio_timestamps[audio_idx] - self.g_pool.seek_control.current_playback_time
                    adj_delay = rt_delay - self.pa_stream.get_output_latency()
                    self.audio_delay = 0
                    self.audio_sync = 0
                    if adj_delay > 0:
                        self.audio_delay = adj_delay
                        self.audio_sync = 0
                    else:
                        self.audio_sync = adj_delay

                    self.g_pool.seek_control.time_slew = self.audio_sync

                    # ts_delay = self.audio_timestamps[0] - self.timestamps[frame_idx]
                    # if ts_delay > 0.:
                    #    delay_lat = ts_delay - self.pa_stream.get_output_latency() - playback_clock_delay
                    #    if delay_lat > 0.:
                    #        self.audio_delay = delay_lat
                    #        self.audio_sync = 0
                    #    else:
                    #        self.audio_delay = 0
                    #        self.audio_sync = - delay_lat
                    # else:
                    #    self.audio_delay = 0.
                    #    self.audio_sync = self.pa_stream.get_output_latency() + playback_clock_delay

                    # if self.pa_stream.is_stopped() or self.audio_paused:
                    self.pa_stream.stop_stream()
                    self.audio_measured_latency = -1
                if self.audio_delay < 0.001:
                    self.audio_start_time = monotonic()
                    self.pa_stream.start_stream()
                else:
                    def delayed_audio_start():
                        if self.pa_stream.is_stopped():
                            self.audio_start_time = monotonic()
                            self.pa_stream.start_stream()
                            self.audio_delay = 0
                            logger.info("Started delayed audio")
                        self.audio_timer.cancel()

                    self.audio_timer = Timer(self.audio_delay, delayed_audio_start)
                    self.audio_timer.start()

                self.audio_paused = False

        elif not self.pa_stream.is_stopped():
            self.pa_stream.stop_stream()

    def on_notify(self, notification):
        pass

    def recent_events(self, events):
        if self.g_pool.seek_control.play and self.pa_stream is not None and self.g_pool.capture.playback_speed == 1.:
            self.play = True
            if (self.pa_stream.is_stopped() or self.audio_paused) and self.audio_delay <= 0.001:
                pbt = self.g_pool.seek_control.current_playback_time
                frame_idx = self.g_pool.seek_control.ts_idx_from_playback_time(pbt)
                playback_start_audio = monotonic()
                audio_idx = bisect(self.audio_timestamps, self.g_pool.timestamps[frame_idx])
                self.seek_to_audio_frame(audio_idx)
            frames_chunk = itertools.islice(self.next_audio_frame, 10)
            for audio_frame_p in frames_chunk:
                audio_frame = self.audio_resampler.resample(audio_frame_p)
                self.audio_bytes_fifo.append(bytes(audio_frame.planes[0]))
            if (self.pa_stream.is_stopped() or self.audio_paused) and self.audio_delay <= 0.001:
                playback_clock_delay = monotonic() - playback_start_audio
                print("Delay from starting playback is {}".format(playback_clock_delay))
                rt_delay = self.audio_timestamps[audio_idx] - self.g_pool.seek_control.current_playback_time
                adj_delay = rt_delay - self.pa_stream.get_output_latency()
                self.audio_delay = 0
                self.audio_sync = 0
                if adj_delay > 0:
                    self.audio_delay = adj_delay
                    self.audio_sync = 0
                else:
                    self.audio_sync = adj_delay

                # ts_delay = self.audio_timestamps[0] - self.timestamps[frame_idx]
                # if ts_delay > 0.:
                #    delay_lat = ts_delay - self.pa_stream.get_output_latency() - playback_clock_delay
                #    if delay_lat > 0.:
                #        self.audio_delay = delay_lat
                #        self.audio_sync = 0
                #    else:
                #        self.audio_delay = 0
                #        self.audio_sync = - delay_lat
                # else:
                #    self.audio_delay = 0.
                #    self.audio_sync = self.pa_stream.get_output_latency() + playback_clock_delay

                # if self.pa_stream.is_stopped() or self.audio_paused:
                self.pa_stream.stop_stream()
                self.audio_measured_latency = -1
                if self.audio_delay < 0.001:
                    self.audio_start_time = monotonic()
                    self.pa_stream.start_stream()
                else:
                    def delayed_audio_start():
                        if self.pa_stream.is_stopped():
                            self.audio_start_time = monotonic()
                            self.pa_stream.start_stream()
                            self.audio_delay = 0
                            logger.info("Started delayed audio")
                        self.audio_timer.cancel()

                    self.audio_timer = Timer(self.audio_delay, delayed_audio_start)
                    self.audio_timer.start()

                self.audio_paused = False

        else:
            if self.pa_stream is not None and not self.pa_stream.is_stopped():
                self.pa_stream.stop_stream()
            self.play = False
