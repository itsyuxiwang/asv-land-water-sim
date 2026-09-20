import unittest
from types import SimpleNamespace

from run import animation_crowd_counts, gif_frame_times


class GifFrameTimesTest(unittest.TestCase):
    def test_samples_every_four_seconds_and_stops_before_limit(self):
        frames = gif_frame_times(gif_seconds=1400, end_seconds=1400)

        self.assertEqual(frames[:3], [0, 4, 8])
        self.assertEqual(frames[-1], 1396)
        self.assertEqual(len(frames), 350)

    def test_caps_animation_at_simulation_end(self):
        self.assertEqual(gif_frame_times(gif_seconds=20, end_seconds=10), [0, 4, 8])

    def test_counts_pier_station_and_office_crowds(self):
        travellers = [
            SimpleNamespace(mode="asv", t_pier=100, t_board=200, t_arrive=500),
            SimpleNamespace(mode="metro", t_pier=None, t_board=None, t_arrive=140),
            SimpleNamespace(mode="asv", t_pier=50, t_board=80, t_arrive=120),
        ]

        counts = animation_crowd_counts(travellers, metro_waiting=3, now=150)

        self.assertEqual(counts, (1, 3, 2))


if __name__ == "__main__":
    unittest.main()
