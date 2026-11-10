# 10x Atera WTA: how the bundles map onto the stores

This file grows with the ingest. The first section holds what the 10x dataset
pages state, copied by hand on 2026-11-10 from saved copies of the pages,
because the pages block automated reads. A person checks each row against the
page before the ingest writes it.

## Attribution

10x's citation format for a dataset, from its citation guidelines page:

> *Dataset title*, *Product* Dataset by *Software* v*x.y.z*, 10x Genomics,
> (*YYYY, Month D*).

The pages state the software version with three parts. `experiment.spatial`
states four, in `analysis_sw_version`. The citation uses the page's form, and
the root attributes keep both.

| Repo | Dataset title | Page | Published | Software |
|---|---|---|---|---|
| `atera-kidney` | Atera In Situ Gene Expression, FFPE Human Kidney | https://www.10xgenomics.com/datasets/atera-wta-ffpe-human-nd-kidney | 2026-10-15 | Atera Onboard Analysis v1.0.4 (`atera-1.0.4.1`) |
| `atera-brain` | Atera In Situ Gene Expression, Fresh Frozen Human Brain | https://www.10xgenomics.com/datasets/atera-wta-ff-human-nd-brain | 2026-11-03 | Atera Onboard Analysis v1.1.1 (`atera-1.1.1.0`) |
| `atera-colorectal` | Atera In Situ Gene Expression, Fresh Frozen Human Colorectal Cancer | https://www.10xgenomics.com/datasets/atera-wta-ff-human-crc | 2026-11-05 | Atera Onboard Analysis v1.1.1 (`atera-1.1.1.0`) |
| `atera-breast` | Atera In Situ Gene Expression, Fresh Frozen Human Breast Cancer | https://www.10xgenomics.com/datasets/atera-wta-ff-human-breast-cancer | 2026-11-05 | Atera Onboard Analysis v1.1.1 (`atera-1.1.1.0`) |

The rendered citations:

- Atera In Situ Gene Expression, FFPE Human Kidney, Atera In Situ Dataset by
  Atera Onboard Analysis v1.0.4, 10x Genomics, (2026, October 15).
- Atera In Situ Gene Expression, Fresh Frozen Human Brain, Atera In Situ
  Dataset by Atera Onboard Analysis v1.1.1, 10x Genomics, (2026, November 3).
- Atera In Situ Gene Expression, Fresh Frozen Human Colorectal Cancer, Atera
  In Situ Dataset by Atera Onboard Analysis v1.1.1, 10x Genomics,
  (2026, November 5).
- Atera In Situ Gene Expression, Fresh Frozen Human Breast Cancer, Atera In
  Situ Dataset by Atera Onboard Analysis v1.1.1, 10x Genomics,
  (2026, November 5).

Every page states: "This dataset is licensed under the Creative Commons
Attribution 4.0 International (CC BY 4.0) license", with a link to 10x's
citation guidelines. The guidelines list no publication for Atera, so the
citation has no DOI. The pages' publication dates for the colorectal and
breast samples, 2026-11-05, are one day after their objects' last-modified
dates, 2026-11-04. The citation uses the page's date.

### What each page states about the sample

| Repo | Biomaterials | Preparation | Instrument and analysis |
|---|---|---|---|
| `atera-kidney` | A non-diseased human kidney FFPE tissue block from Donor Network West. | Atera Gene Expression - FFPE Tissue Preparation Handbook (CG001671); Atera Gene Expression User Guide (CG001672); Post-Atera H&E Staining Demonstrated Protocol (CG001722). | Atera Instrument User Guide (CG001673); Atera Onboard Analysis v1.0.4. |
| `atera-brain` | Fresh frozen, non-diseased human brain tissue (hippocampus region) from ProteoGenex, pre-embedded in OCT. | Atera Gene Expression - Fresh Frozen Tissue Preparation Handbook (CG001670); CG001672; CG001722. | CG001673; Atera Onboard Analysis v1.1.1. |
| `atera-colorectal` | Fresh frozen human colon sample (colorectal adenocarcinoma) from Discovery Life Sciences, snap-frozen by the vendor and embedded in OCT in-house. | CG001670; CG001672; CG001722. | CG001673; Atera Onboard Analysis v1.1.1. |
| `atera-breast` | Fresh frozen human breast sample (infiltrating ductal carcinoma) from ProteoGenex, pre-embedded in OCT. | CG001670; CG001672; CG001722. | CG001673; Atera Onboard Analysis v1.1.1. |

The pages' metric tables agree with `metrics_summary.csv` in each bundle:
number of cells detected, median transcripts and genes per cell, total high
quality decoded transcripts, and region area. The page labels the species
Human, the anatomical entity (Kidney, Brain, Colon, Breast), the preservation
method, the disease state (Healthy, Healthy, Colorectal cancer, Breast cancer)
and "Cells" for cells or nuclei. The root attributes carry these under
`sample`.

### The objects

All under `https://s3-us-west-2.amazonaws.com/10x.files/samples/atera/<version>/<sample>/`,
also at the matching key on `https://cf.10xgenomics.com/`. Sizes and ETags
from HEAD requests on 2026-11-10.

| Repo | Object | Size | ETag | Last modified |
|---|---|---|---|---|
| `atera-kidney` | `Atera_v1_WTA_Kidney_non_diseased_FFPE_outs.zip` | 45,720,445,144 | `8c1c07343dbe1b946f5f90a21130782f-5451` | 2026-10-15 |
| | `Atera_v1_WTA_Kidney_non_diseased_FFPE_he_image.ome.tif` | 2,798,438,508 | `b87fddd2e5d93223bb40758c6bfb1366-334` | 2026-10-15 |
| | `Atera_v1_WTA_Kidney_non_diseased_FFPE_he_imagealignment.csv` | 123 | `db6cd13df295292000e42656df8bbc9b` | 2026-10-15 |
| | `Atera_v1_WTA_Kidney_non_diseased_FFPE_experiment.spatial` | 2,896 | `ed70b43593bb552e16bf35fbc278e222` | 2026-10-15 |
| `atera-brain` | `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF_outs.zip` | 48,858,124,574 | `359ca38cb4670d0549d251af698f070a-5825` | 2026-11-03 |
| | `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF_he_image.ome.tif` | 6,519,718,813 | `14e2d77d6e10d60d538c5a2ab7236309-778` | 2026-11-03 |
| | `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF_he_imagealignment.csv` | 125 | `85d2276848374482766afad40ad2c55e` | 2026-11-03 |
| | `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF_experiment.spatial` | 2,930 | `d88d03847832624857e2ebbc1349cf38` | 2026-11-03 |
| `atera-colorectal` | `Atera_v1_WTA_S4B_Colorectal_Cancer_FF_outs.zip` | 75,341,543,536 | `ce1dcb9e9ec1d44552586a2f7e30024a-8982` | 2026-11-04 |
| | `Atera_v1_WTA_S4B_Colorectal_Cancer_FF_he_image.ome.tif` | 5,067,976,962 | `226e11c71dcbf38b661cb2fbba09e035-605` | 2026-11-04 |
| | `Atera_v1_WTA_S4B_Colorectal_Cancer_FF_he_imagealignment.csv` | 126 | `53df92f8420d6e0934a2b22642d7baec` | 2026-11-04 |
| | `Atera_v1_WTA_S4B_Colorectal_Cancer_FF_experiment.spatial` | 2,927 | `2c402d96e0d2744898d25b0b1397e701` | 2026-11-04 |
| `atera-breast` | `Atera_v1_WTA_S4C_Breast_Cancer_FF_outs.zip` | 89,305,623,909 | `be63268c25017a86b1112d65c9263903-5324` | 2026-11-04 |
| | `Atera_v1_WTA_S4C_Breast_Cancer_FF_he_image.ome.tif` | 7,318,928,225 | `71258bd03ebd740984a3ad26cbc78580-873` | 2026-11-04 |
| | `Atera_v1_WTA_S4C_Breast_Cancer_FF_he_imagealignment.csv` | 128 | `617f03a26f8ba95f7886afdc99f325d7` | 2026-11-04 |
| | `Atera_v1_WTA_S4C_Breast_Cancer_FF_experiment.spatial` | 2,922 | `e8fff076611d29149606bd40dd79989a` | 2026-11-04 |

Each page also links `<sample>_analysis_summary.html`, a fifth object beside
the bundle, on the CDN and in the bucket. It is the report the bundle holds
as a member. The ingest records it with the other objects and reads the
report from the bundle.

| Repo | Object | Size | ETag | Last modified |
|---|---|---|---|---|
| `atera-kidney` | `Atera_v1_WTA_Kidney_non_diseased_FFPE_analysis_summary.html` | 9,114,279 | `86c92f59b074ac6d1931a060a26a4eca-2` | 2026-10-15 |
| `atera-brain` | `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF_analysis_summary.html` | 9,387,928 | `3bc5393fa0247f3ac2881bcffdec91d8-2` | 2026-11-03 |
| `atera-colorectal` | `Atera_v1_WTA_S4B_Colorectal_Cancer_FF_analysis_summary.html` | 9,227,516 | `2b1cbe0fc7dc0450bd5af267c99c1f56-2` | 2026-11-04 |
| `atera-breast` | `Atera_v1_WTA_S4C_Breast_Cancer_FF_analysis_summary.html` | 9,225,158 | `75da29c5332603ba398a1ba22bd87da8-2` | 2026-11-04 |
