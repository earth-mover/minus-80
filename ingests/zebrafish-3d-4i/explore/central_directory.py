import marimo

__generated_with = "0.25.2"
app = marimo.App(width="full")


@app.cell
def _():
    import marimo as mo
    import polars as pl

    from inputs import directory_table

    table = directory_table()
    meta = {key.decode(): value.decode() for key, value in table.schema.metadata.items()}
    cd = pl.from_arrow(table).with_columns(
        flags=pl.col("flags").map_elements(lambda flags: f"{flags:#06x}", return_dtype=pl.String),
        crc32=pl.col("crc32").map_elements(lambda crc: f"{crc:08x}", return_dtype=pl.String),
    )
    return cd, meta, mo, pl


@app.cell
def _(meta, mo):
    mo.md(f"""
    # The S-BIAD3228 central directory

    A zip keeps its table of contents, the central directory, at the end of the file: one entry per member,
    in the order the members are stored. This one starts at byte {int(meta["directory_offset"]):,} of
    {int(meta["archive_size"]):,} and is {int(meta["directory_size"]):,} bytes long. Each row below is one
    entry, parsed by Python's `zipfile` (`minus80.parsers.zip.directory`). The first run fetches the
    directory from EBI and keeps it as Parquet (`inputs.py`); later runs read the file with no network.

    | column | meaning |
    |---|---|
    | `name` | the member's path: a Zarr key |
    | `local_header_offset` | where the member starts: its local header, the short record just before its data. Past 4 GiB the 32-bit field holds `0xffffffff` and the real offset is in the entry's ZIP64 extra block; this column holds the real one |
    | `compressed`, `uncompressed` | sizes. For a chunk, `uncompressed` is the size of its Blosc frame: 0 is an empty file, 80,897 an all-zero image chunk, 161,455 an all-zero label chunk |
    | `method` | 8 = Deflate |
    | `crc32` | checksum of the uncompressed bytes |
    | `flags` | `0x0808`: bit 3, the sizes follow the data in a data descriptor; bit 11, the name is UTF-8 |
    | `version_made_by` | the writer's host system × 256 plus its zip version: 20, or 45 for an entry that needs ZIP64; host 0 is MS-DOS |
    | `version_needed` | the zip version a reader needs |
    | `extra_ids` | the extra field's blocks: `0x5455` a Unix timestamp, `0x0001` ZIP64 |
    | `entry_offset`, `entry_bytes` | where this entry sits in the file and its length: 46 bytes plus the name and the extra field |
    """)
    return


@app.cell
def _(mo):
    name_filter = mo.ui.text(label="name contains (regex)", value="^C/02/2/0/", full_width=True)
    where = mo.ui.dropdown(["any", "below 4 GiB", "past 4 GiB"], value="any", label="local header")
    mo.hstack([name_filter, where], widths=[3, 1])
    return name_filter, where


@app.cell
def _(cd, mo, name_filter, pl, where):
    view = cd
    if name_filter.value:
        view = view.filter(pl.col("name").str.contains(name_filter.value))
    if where.value == "below 4 GiB":
        view = view.filter(pl.col("local_header_offset") < 2**32 - 1)
    elif where.value == "past 4 GiB":
        view = view.filter(pl.col("local_header_offset") >= 2**32 - 1)
    mo.vstack([
        mo.md(f"**{view.height:,}** of {cd.height:,} entries · {view['compressed'].sum() / 1e9:.2f} GB compressed"),
        mo.ui.table(view, page_size=40, selection=None),
    ])
    return


@app.cell
def _(cd, mo, pl):
    def counts(column):
        """How many entries take each value of one column."""
        return cd.group_by(column).agg(pl.len().alias("entries")).sort("entries", descending=True)

    mo.vstack([
        mo.md(f"## The values each field takes, across all {cd.height:,} entries"),
        mo.hstack([counts("version_made_by"), counts("version_needed"), counts("flags"), counts("method"),
                   counts("extra_ids")], wrap=True),
    ])
    return


if __name__ == "__main__":
    app.run()
