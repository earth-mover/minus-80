"""The four Atera samples, with the facts 10x publishes for each one."""

from __future__ import annotations

from typing import NamedTuple

BASE = "https://s3-us-west-2.amazonaws.com/10x.files/samples/atera"
#: The same objects as an S3 URL. obstore cannot parse the path-style HTTPS form, and
#: refuses the CDN's answer to a range read of a bundle, so reads use this one.
S3 = "s3://10x.files/samples/atera"


class Sample(NamedTuple):
    repo: str
    name: str
    version: str
    title: str
    page: str
    published: str
    software: str

    def url(self, suffix: str, base: str = BASE) -> str:
        """The URL of the sample's object whose name ends in ``suffix``, under ``base``."""
        return f"{base}/{self.version}/{self.name}/{self.name}{suffix}"


_PAGES = "https://www.10xgenomics.com/datasets"
_TITLE = "Atera In Situ Gene Expression"

SAMPLES = {
    "kidney": Sample(
        "atera-kidney", "Atera_v1_WTA_Kidney_non_diseased_FFPE", "1.0.4",
        f"{_TITLE}, FFPE Human Kidney", f"{_PAGES}/atera-wta-ffpe-human-nd-kidney",
        "2026-10-15", "Atera Onboard Analysis v1.0.4",
    ),
    "brain": Sample(
        "atera-brain", "Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF", "1.1.1",
        f"{_TITLE}, Fresh Frozen Human Brain", f"{_PAGES}/atera-wta-ff-human-nd-brain",
        "2026-11-03", "Atera Onboard Analysis v1.1.1",
    ),
    "colorectal": Sample(
        "atera-colorectal", "Atera_v1_WTA_S4B_Colorectal_Cancer_FF", "1.1.1",
        f"{_TITLE}, Fresh Frozen Human Colorectal Cancer", f"{_PAGES}/atera-wta-ff-human-crc",
        "2026-11-05", "Atera Onboard Analysis v1.1.1",
    ),
    "breast": Sample(
        "atera-breast", "Atera_v1_WTA_S4C_Breast_Cancer_FF", "1.1.1",
        f"{_TITLE}, Fresh Frozen Human Breast Cancer", f"{_PAGES}/atera-wta-ff-human-breast-cancer",
        "2026-11-05", "Atera Onboard Analysis v1.1.1",
    ),
}
