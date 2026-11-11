from minus80.core.progress import Progress
from minus80.core.registry import as_url
from minus80.core.remote import counting_reader

DATA = bytes(range(256)) * 64


def test_counting_reader_counts_ranges_and_heads(range_server):
    range_server.files["/f.bin"] = DATA
    reader, counter = counting_reader(f"{range_server.url}/f.bin")
    reader.seek(10)
    assert reader.read(100) == DATA[10:110]
    assert counter.bytes >= 100
    assert counter.requests == len(range_server.requests)


def test_counting_reader_counts_a_local_file(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(DATA)
    reader, counter = counting_reader(as_url(path))
    reader.seek(10)
    assert reader.read(100) == DATA[10:110]
    assert counter.bytes >= 100 and counter.requests >= 1


def test_the_counter_advances_the_progress_it_holds(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(DATA)
    reader, counter = counting_reader(as_url(path))
    with Progress("read", total=len(DATA)) as progress:
        counter.progress = progress
        reader.read(100)
    assert progress.done == counter.bytes > 0


def test_counting_reader_reads_through_the_registry_it_is_given(tmp_path):
    from minus80.core.registry import registry_for

    path = tmp_path / "f.bin"
    path.write_bytes(DATA)
    reader, counter = counting_reader(as_url(path), registry=registry_for(as_url(path)))
    assert reader.read(10) == DATA[:10]
    assert counter.bytes >= 10
