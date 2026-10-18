from bb.brake import BurstBrake


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_channel_limit_trips_and_cools_down():
    clock = Clock()
    brake = BurstBrake(per_channel=3, per_server=100, window=60, cooldown=600, clock=clock)
    assert [brake.allow(1, 10) for _ in range(4)] == [True, True, True, False]
    assert brake.allow(1, 11)  # other channels are unaffected
    clock.t = 120  # window passed, but the channel is still cooling down
    assert not brake.allow(1, 10)
    clock.t = 601
    assert brake.allow(1, 10)


def test_server_limit_covers_many_channels():
    clock = Clock()
    brake = BurstBrake(per_channel=10, per_server=4, window=60, cooldown=600, clock=clock)
    assert [brake.allow(1, c) for c in (10, 11, 12, 13, 14)] == [True, True, True, True, False]
    assert brake.allow(2, 20)  # another server has its own budget


def test_normal_pace_never_trips():
    clock = Clock()
    brake = BurstBrake(per_channel=10, per_server=40, window=60, cooldown=600, clock=clock)
    for _ in range(100):
        clock.t += 10  # a link every 10 seconds, all day
        assert brake.allow(1, 10)
