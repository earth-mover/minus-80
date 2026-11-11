import pytest

from minus80.core.blocks import compare_blocks, sampled_blocks


def _reader(data: bytes):
    return lambda start, stop: data[start:stop]


def test_sampled_blocks_hold_the_first_the_last_and_the_requested_number_between():
    found = sampled_blocks(100 * 8, 8, 5)
    assert len(found) == 7 and found[0] == 0 and found[-1] == 99
    assert found == sorted(set(found)) and all(0 < b < 99 for b in found[1:-1])


def test_sampled_blocks_give_the_same_blocks_for_the_same_seed():
    assert sampled_blocks(1000, 8, 5, seed=3) == sampled_blocks(1000, 8, 5, seed=3)
    assert sampled_blocks(1000, 8, 5, seed=3) != sampled_blocks(1000, 8, 5, seed=4)


@pytest.mark.parametrize("size, block", [(0, 8), (5, 8), (16, 8), (17, 8), (40, 8)])
def test_sampled_blocks_hold_every_block_when_the_samples_cover_the_rest(size, block):
    assert sampled_blocks(size, block, 3) == list(range(-(-size // block)))


def test_equal_bytes_have_no_differing_block():
    data = bytes(range(256)) * 40
    sampled, differing = compare_blocks(_reader(data), _reader(data), len(data), block=1000, samples=2)
    assert len(sampled) == 4 and differing == []


def test_a_changed_byte_in_a_sampled_block_is_found():
    data = bytes(range(256)) * 40
    changed = bytearray(data)
    target = sampled_blocks(len(data), 1000, 2)[1]
    changed[target * 1000 + 7] ^= 1
    sampled, differing = compare_blocks(_reader(data), _reader(bytes(changed)), len(data), block=1000, samples=2)
    assert differing == [target] and target in sampled


def test_the_last_block_is_the_remainder_and_is_compared_whole():
    data = bytes(2500)
    changed = data[:-1] + b"\x01"
    sampled, differing = compare_blocks(_reader(data), _reader(changed), len(data), block=1000, samples=0)
    assert sampled == [0, 2] and differing == [2]


def test_a_changed_byte_in_a_block_that_the_sample_skips_is_not_found():
    # The comparison covers the sampled blocks only; this documents that bound.
    data = bytes(100 * 8)
    sampled = sampled_blocks(len(data), 8, 2)
    skipped = next(b for b in range(100) if b not in sampled)
    changed = bytearray(data)
    changed[skipped * 8] = 1
    assert compare_blocks(_reader(data), _reader(bytes(changed)), len(data), block=8, samples=2)[1] == []


def test_each_reader_gets_one_block_range_per_sampled_block():
    asked = []

    def read(start, stop):
        asked.append((start, stop))
        return bytes(stop - start)

    compare_blocks(read, read, 2500, block=1000, samples=0)
    assert asked == [(0, 1000), (0, 1000), (2000, 2500), (2000, 2500)]

