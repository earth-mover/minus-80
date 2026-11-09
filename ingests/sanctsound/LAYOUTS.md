# SanctSound: how the bucket maps onto the store

The NOAA-Navy Sanctuary Soundscape Monitoring Project (SanctSound) recorded
underwater sound in the US National Marine Sanctuary System from fall 2018 to
spring 2022. It covers seven national marine sanctuaries and one marine national
monument ([overview](https://sanctuaries.noaa.gov/news/feb21/sanctsound-overview.html),
[portal](https://sanctsound.ioos.us/)). The project has ended, so the archive is
static. The raw audio has DOI
[10.25921/saca-sp25](https://doi.org/10.25921/saca-sp25), and each derived
product has its own.

## Files

The data is in `gs://noaa-passive-bioacoustic/sanctsound/`, a Google Cloud
Storage bucket that allows anonymous reads. It has no S3 copy. The same bucket
is reachable as `https://storage.googleapis.com/noaa-passive-bioacoustic/`.

```
sanctsound/
  audio/<site>/sanctsound_<deployment>/audio/*.flac    raw recordings
  audio/<site>/sanctsound_<deployment>/ancillary/      calibration PDFs, temperature logs
  audio/<site>/sanctsound_<deployment>/metadata/       SanctSound_<DEPLOYMENT>.json
  products/detections/<site>/                          species and event presence
  products/sound_level_metrics/<site>/                 BB, OL, TOL, PSD
  products/sound_propagation_models/<site>/            modeled, site level
  products/sound_clips/<site>/                         short WAV excerpts
```

The archive holds 30 sites and 196 deployments: 158,544 audio files (49.67 TB)
and 19,626 product files (0.40 TB). CI01_01, the deployment the ingest was built
on, is 355 files and 272 GB. Indexing cost follows bytes, not file count,
because a frame scan reads every byte.

## Audio

Every recording in all 196 deployments is mono, 16-bit FLAC with a fixed
blocksize. Every frame holds the same number of samples, is byte-aligned and
decodes on its own. Three properties vary between deployments, and so the
codec configuration (sample rate and blocksize) is per deployment, never per
site:

| property | values seen (392 sampled files) |
| --- | --- |
| sample rate | 48 kHz (354), 96 kHz (16), 200 kHz (22) |
| blocksize | 4096 (320), 4608 (58), 8192 (14) samples |
| SEEKTABLE | present in 320, absent in 72 |

The SEEKTABLE is absent in files from 36 of the 196 deployments, mostly the
first deployment at a site. The ingest therefore finds frames by scanning each
whole file, which works for every deployment, instead of reading the
SEEKTABLE where it exists. A SEEKTABLE also spaces seekpoints by time, not by
frame count, so they are irregular in samples.

The files carry no tags. A recording's start time exists only in its filename,
in one of these forms (`ingest.FILENAME_TIMES` reads the first two):

```
SanctSound_CI01_01_671379494_20181031T220000Z.flac                  ISO 8601
SanctSound_SB01_13_67403784_201122183500.flac                       two-digit year, the most common
SanctSound_HI03_02_1208496153_200130201723_Post_ReDeployment.flac   free-text suffix
SanctSound_CI01_02_671883305_190325210000_o.flac                    trailing marker
```

The long number is the instrument serial number, which appears nowhere else in
the audio. `ingest.write_provenance` reads it from the first recording's name.
The code does not parse the third and fourth forms, so it fails on a
deployment that uses them, until a pattern is added.

## Timestamps and gaps

Filename timestamps are accurate to about 1.5 s. Across CI01_01's 354 file
boundaries, the stated start of one file minus the end implied by the previous
one ranges from -1.528 s to +0.342 s, with a mean of -0.585 s. That makes 345
boundaries look like overlaps. They are not: correlating the 1.53 s that
file 46 and file 47 appear to share gives r = 0.005, so the audio is different
and continuous. The filenames are what is imprecise. Nine boundaries are real
gaps, one of them 3.05 h.

So the store never places samples at wall-clock positions. Recordings are packed end to
end on one sample axis, and a segment table maps each recording's samples to its
filename time. `overlap_seconds` records the apparent overlap and is a
measure of timestamp confidence, never a reason to trim samples. Relative
timing within a recording is exact. Of CI01_01's 355 files, 353 are nominally
3 h, but they have 307 distinct sample counts, and 354 end on a short final
frame.

## Layout of the store

One group per deployment, named like the archive (`CI01_01`). A deployment is
the unit of a uniform array, because the sample rate and blocksize are
constant inside one and vary between them.

| node | content | stored as |
| --- | --- | --- |
| `audio/waveform` | `("time",)`, int16; every recording end to end | references into the FLAC files |
| `audio/segments/` | `start_sample`, `n_samples`, `start_time`, `end_time`, `source_uri`, `overlap_seconds`, one row per recording | written |
| `sound_levels/` | `broadband_spl`, `octave_spl`, `third_octave_spl`, `psd` on one hourly `time` axis, with `frequency_*` axes | written, from NOAA's netCDF |
| `detections/` | one event table: `start_time`, `end_time`, `label`, `presence`, `effort` | written, from NOAA's netCDF |
| `detections/labels/` | per label: taxon name and LSID, source type, NOAA's `history` as `method`, DOI | written |
| `temperature/` | the mooring logger's `time` and `sea_water_temperature` | written, from NOAA's `.rsk` |
| `calibration/` | the certificate PDF as `uint8` bytes, the sensitivity it states, and a measured `counts_to_upa_db` | written |

The audio is referenced because it is 272 GB per deployment. The products are
copied because they total about 205 MB and their source chunking cannot be
referenced: `PSD_1h` is one contiguous 204 MB block, so each frequency
selection would pull the whole file.

The waveform's chunk grid is rectilinear: each recording's last chunk ends at
its last sample. A regular grid would need the codec to zero-fill each tail, and
those zeros would sit mid-array where Zarr returns them as data. Readers set
`zarr.config.set({"array.rectilinear_chunks": True})` and need the `flac` codec.

A chunk is 512 FLAC frames, 2,097,152 samples at a blocksize of 4096. See
`ingest.FRAMES_PER_CHUNK` for why that is open. The waveform's `time_index`
attribute names its group's `audio/segments` table, and that table's `comment`
says how to turn a sample index into a time and back.

## Judgement calls

- **Frames per chunk.** The shared FLAC parser's default is one frame per
  chunk. The ingest merges 512, the value the published
  `earthmover-biology/sanctsound` store uses, to hold CI01_01 at 175,314
  references instead of about 90 million. The repository rule is to match the
  source's storage unit, so this is a deviation. It is recorded here for a
  maintainer to settle.
- **One concatenated array per deployment, no padding.** The 3.05 h gap and the
  apparent overlaps are carried by the segment table, not by fill values.
- **No channel axis.** Every SanctSound recording is mono, so the waveform is
  written with `write_array(..., channel_axis=False)` as `("time",)`. A
  length-1 channel axis would add an index to every read and carry no
  information.
- **Sound levels are copied, not referenced.** See above.
- **Corrected standard name.** NOAA's `BB_1h` declares
  `sound_intensity_level_in_water`, which CF v94 defines against 6.7e-19 W m-2.
  Its own `long_name` says sound pressure levels, and `OL_1h`, `TOL_1h` and
  `PSD_1h` use `sound_pressure_level_in_water`, referenced to 1 µPa. The store
  writes the pressure name and records the correction in the variable's
  `comment`. The source declares `cell_methods` of `time: sum`, which its
  own `history` contradicts: every product is an hourly median of 1 s values.
  The store writes `time: median (interval: 1 s)`.
- **Taxa and sources.** `ingest.TAXON` assigns a WoRMS AphiaID by scientific
  name to each biological label. A label that is in none of `TAXON`,
  `ANTHROPOGENIC` or `UNKNOWN_SOURCE` raises, because a default would publish
  a guess as a fact.
- **Audio is uncalibrated counts.** NOAA publishes no conversion to pressure.
  The certificate gives the hydrophone sensitivity in volts, or for some
  recorders a full-scale response, and omits the ADC full-scale voltage and the
  gain. `ingest.measure_counts_offset` replicates NOAA's `BB_1h` method on eight
  hours of the store's own audio and differences it from the published hourly
  level, which gives `counts_to_upa_db`. The median of the 1 s values is what
  matches `BB_1h`: on CI01_01 it recovers the offset to within 0.003 dB, where a mean
  is 1 to 4.5 dB off. Each certificate's values are transcribed by hand from a
  PDF with no text layer, keyed by the PDF's SHA-256 so a transcription applies
  only to its own file.
- **Serial numbers.** Where the certificate and the file names disagree on the
  hydrophone serial, both are recorded verbatim in `calibration`, with no claim
  about which identifies which component.

## Problems in the source

None of these raises an error.

1. Seven of eight CI01_01 detection files have an all-zero `time`, a phony
   netCDF dimension. The real times are `start_time` and `end_time`, in
   milliseconds since the epoch. Only `bluewhale` has a real `time` and no
   `start_time`. `write_detections` reads whichever the file has.
2. Variable names do not follow the label: `finwhale_1d` holds
   `finwhale_presence`, `Explosions` holds `explosions_presence`, `sonar` holds
   `mfa_presence`. The ingest matches on the `_presence` and `_effort` suffixes and
   requires exactly one `_presence`.
3. `BB_1h` declares the wrong CF standard name and `cell_methods`, as above.
4. `PSD_1h` is one uncompressed block, as above.
5. `geospatial_bounds` is latitude first, not in WKT order: `POINT (34.0438
   -120.0811)` read as WKT gives a latitude of -120. The ingest takes the
   location from the deployment's metadata JSON.
6. `time_stamp` strings in the products are not ISO 8601 (a colon before the
   milliseconds) and are HDF5 variable-length strings, so they cannot be
   referenced. The ingest does not copy them.
7. Instrument serial and depth are absent from the netCDFs. Depth is in the
   deployment's metadata JSON, which `write_provenance` reads.

## Facts the code depends on

- The `.rsk` temperature record exists for some deployments only. Where there is
  none, `write_temperature` returns `None`. Its timestamps are not corrected for
  the logger's recorded clock drift.
- No sound levels exist for some deployments (OC01_01), so the calibration
  group holds the certificate and no offset.
- Every product file repeats the project-level ACDD attributes, so any one
  supplies them: `BB_1h` where it exists, otherwise a detection file.
- `write_calibration` raises on a certificate whose SHA-256 is not in
  `ingest.CERTIFICATES`, so a new deployment needs its certificate transcribed.
- Arraylake refuses `https://` virtual chunk prefixes, so references use `gs://`
  and the scan reads over HTTPS only to find offsets.
- Frame scanning of one deployment reads every byte. Parsing is not the limit:
  on a laptop, one process scans a local file at about 1.5 GB/s. Threads are:
  several threads scan more slowly in total than one, so the scan uses
  processes. The published scans used four threads on a cloud VM (an AWS
  c6in.2xlarge) and reached about 174 MB/s; eight threads reached about 62 MB/s.
  A home connection reached about 30 MB/s.
