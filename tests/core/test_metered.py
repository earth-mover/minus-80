import numpy as np
import zarr
from zarr.storage import MemoryStore

from minus80.core.metered import Meter, MeteredStore


def test_every_shard_written_is_counted_at_its_stored_size():
    inner, meter = MemoryStore(), Meter()
    store = MeteredStore(inner, meter, name="memory://test")
    array = zarr.create_array(store, shape=(4, 64, 64), chunks=(1, 64, 64), shards=(2, 64, 64), dtype="u2")
    array[:] = np.arange(4 * 64 * 64, dtype="u2").reshape(4, 64, 64)
    shards = {k: v for k, v in inner._store_dict.items() if k.startswith("c/")}
    documents = {k: v for k, v in inner._store_dict.items() if not k.startswith("c/")}
    assert meter.writes == len(shards) + len(documents)
    assert meter.bytes == sum(len(v) for v in inner._store_dict.values())
    assert meter.running == 0 and meter.seconds >= meter.slowest > 0


def test_its_name_never_asks_the_wrapped_store():
    class Unprintable(MemoryStore):
        def __str__(self):
            raise AssertionError("the wrapped store was asked for its name")

    store = MeteredStore(Unprintable(), Meter(), name="icechunk://main")
    assert str(store) == "icechunk://main"
    zarr.create_array(store, name="a", shape=(2,), dtype="u1")
    assert zarr.open_group(store, mode="r")["a"].shape == (2,)
