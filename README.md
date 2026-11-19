# [Sonador IO](https://sonador.oak-tree.tech/io) Python Library
[Sonador IO](https://sonador.oak-tree.tech/io) is the data management interface of [Sonador](https://sonador.oak-tree.tech) used to securely store medical data so that it can be queried, retrieved, and transferred. It provides a streamlined way to share medical data across the Internet or with other applications within the hospital.

The [Sonador IO Python Library](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-client) provides tools for working with meta and payload (pixel) data stored in [Sonador/Orthanc](https://code.oak-tree.tech/oak-tree/medical-imaging/orthanc-sonador). It builds on top of the [Guru Client Library](https://code.oak-tree.tech/guru-labs/guru-client) and provides data models for medical imaging resources (patient, study, series), Sonador's "DICOM extension models" (which provide specialized features such as interfaces for worklists), resource comments, interfaces for working with structured data (SR documents), and interfaces for managing 3D data. _The 3D capabilities of the Sonador IO client are focused on data modelling and providing a foundation for more advanced capabilities. For tools and libraries to work with imaging volume and spatial data, refer to [Sonador 3D](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador3d)._

Dependencies:

* [Guru Client Library](https://code.oak-tree.tech/guru-labs/guru-client): provides [client models](https://code.oak-tree.tech/django-apps/guru/-/wikis/dev.client-tools) for interacting with Sonador/Orthanc APIs using [REST-like patterns](https://code.oak-tree.tech/django-apps/guru/-/wikis/dev.data-models).
* [`pydicom`](https://pydicom.github.io/): tools for parsing DICOM (Digital Imaging and Communications in Medicine) data and interfacing with pixel/payload data via [`numpy`](https://numpy.org/).
* [`highdicom`](https://github.com/ImagingDataCommons/highdicom): high-level DICOM abstractions for the Python programming language. Provides tools for parsing segmentation (`SEG`) and structured reporting (`SR`) documents so that they can be consumed from `numpy`. **The client requires the [Oak-Tree fork of highdicom](https://code.oak-tree.tech/oak-tree/medical-imaging/highdicom)** (published to the Sonador package repository as `highdicom==<upstream version>.postN`), which adds the `record_instances` option used when writing DICOM-SR documents. Importing the SR helpers with a stock highdicom raises an `ImportError` explaining how to install the fork (see `dcmcompat.py`).



## Installation
Pre-built packages of the client library are available from the [Sonador package repository](https://code.oak-tree.tech/oak-tree/medical-imaging/imaging-development-env/-/wikis/deployment.package-repository) and can be installed with `pip`:

```bash
pip install sonador --extra-index-url https://code.oak-tree.tech/api/v4/projects/335/packages/pypi/simple
```

The package declares its runtime dependencies, which `pip` resolves during installation.

### Installing from source
The Sonador project separates development from release through a packaging repository. For development, a clean copy of this repository can be checked out and used directly within a project, allowing updates to be made as part of a clean development loop. The [packaging repository](https://code.oak-tree.tech/oak-tree/medical-imaging/packages/sonador) contains the dependencies, the elements needed for extended testing, and the runtime configuration used to create `pip` or wheel packages. It includes this repository as its `sonador` submodule, and its `setup.py` is where the dependency versions are declared.

To install from source together with its dependencies, clone the packaging repository and install it:

```bash
git clone --recurse-submodules https://code.oak-tree.tech/oak-tree/medical-imaging/packages/sonador.git sonador-package
cd sonador-package
pip install . --extra-index-url https://code.oak-tree.tech/api/v4/projects/335/packages/pypi/simple
```

## Usage
Examples of how to utilize the features of the client can be found in the [Sonador Examples Repository](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples). _More advanced capabilities highlighting access control, 3D capabilities, and worklists are demonstrated in the [`technical-reference` subfolder](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/tree/master/technical-reference?ref_type=heads)._

* Basic usage of the client is outlined in [this notebook](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/blob/master/sonador-client.image-io.ipynb), and introduces three of the core classes in the library: `sonador.servers.SonadorServer`, `sonador.servers.SonadorImagingServer`, and imaging resource models.
* An overview of working with structured data (via DICOM-SR documents) can be found [here](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/blob/master/sonador-client02.dicom-sr.ipynb?ref_type=heads).
* Basic AI capabilities and how to interface with AI based tools (via `numpy`) are demonstrated in [this notebook](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/blob/master/sonador-client03.ai-lung.ipynb?ref_type=heads).

### DICOM-SR Annotations
An annotation is something a person or a program records about an image: a label on a series, the distance
between two points, an arrow with a note beside it. DICOM stores annotations in Structured Report (DICOM-SR)
documents, which are saved in the study beside the images they describe.

Sonador IO and the Sonador Viewer read and write the same form of these documents. An annotation drawn in
the Viewer can be read in Python, one written from Python can be opened in the Viewer, and the client can
change a document and save it again as a new version. That is the DICOM-SR round trip.

The client supports these annotations:

| Annotation | What it is | Class |
| --- | --- | --- |
| Series Tag | A coded label that applies to a whole imaging series | `Finding` |
| Length | The distance between two points | `LengthMeasurement` |
| Angle | The angle at a vertex, from three points | `AngleMeasurement` |
| Cobb angle | The angle between two separate lines | `CobbAngleMeasurement` |
| Circle | A circle with its radius, area and perimeter | `CircleMeasurement` |
| Bidirectional | A long axis and a short axis, two line segments | `BidirectionalMeasurement` |
| Arrow | A text note anchored on an image by an arrow | `ArrowAnnotation` |
| Landmark set | A named, ordered group of points, each with a stable identifier | `LandmarkSet` |

Measurements, arrows and landmarks are stored either on one image, as pixel positions, or in patient space,
as millimetres in a reference frame. A report can also declare frames of its own, such as the coordinate
system of a planning tool, and record how each relates to the images.

Writing a Series Tag and reading it back:

```python
from sonador.imaging.helpers.sr import SonadorComprehensiveSR3D, SonadorImagingMeasurementReport
from sonador.apisettings.base import Finding

# Write: tag an imaging series
tag = study.pacs.get_tag(group, tag_id)
model = SonadorImagingMeasurementReport(sonador_ref_series=series, findings=[Finding.series_tag(tag, description='...')],
	report_procedure=procedure, person_observer='Family^Given', record_instances=False)
document = model.create_sr()

# Read: from a document, or from the documents stored for a series
report = SonadorComprehensiveSR3D.from_sr('report.dcm')
report.series_tag_findings
series.findings('series-tag')
```

The other annotations follow the same pattern: build the object, pass it to the report model, and read it
back through `findings`, `measurements` or `landmarks`.

A document can belong to a timepoint, a bucket that groups the series of one study. Pass
`timepoint=Timepoint.create('Post-operative review', ts)` to the report model, list the timepoints of a study
with `study.timepoints()`, and narrow `findings`, `measurements` and `landmarks` with `timepoint_uid`.

The details are in the Sonador documentation:

* [Annotations in DICOM-SR Documents](https://code.oak-tree.tech/oak-tree/medical-imaging/imaging-development-env/-/wikis/integrations/sonador-io/dicom-sr-annotations): Series Tags, measurements, arrows and
  points, with the document form of each and the diagnostics a reader reports.
* [Reference Frames in DICOM-SR Documents](https://code.oak-tree.tech/oak-tree/medical-imaging/imaging-development-env/-/wikis/integrations/sonador-io/reference-frames): frames, the transforms between them, and
  which geometry can be shown on the images.
* [Landmark Sets in DICOM-SR Documents](https://code.oak-tree.tech/oak-tree/medical-imaging/imaging-development-env/-/wikis/integrations/sonador-io/landmark-sets): building, reading and editing sets of named points.

### DICOM Dates and Times
`sonador.serialization` parses DICOM date and time values: `dcm_str2date` (DA), `dcm_str2time` (TM, with
the trailing components optional and a fraction of up to six digits), `dcm_str2datetime` (DT) and
`dcm_str2utcoffset` (Timezone Offset From UTC, `&ZZXX` from `-1200` to `+1400`). `DicomDatetimePair`
(`sonador.apisettings`) combines a date and a time value into a timestamp: `ts` is the naive local
timestamp of the dataset, and `ts_utc` applies the dataset's offset so that documents written in
different time zones can be ordered.

```python
from sonador.apisettings import DicomDatetimePair

pair = DicomDatetimePair(ds.ContentDate, ds.ContentTime, offset_value=ds.get('TimezoneOffsetFromUTC'))
pair.ts        # datetime.datetime(2026, 1, 2, 12, 0)   (as written)
pair.ts_utc    # datetime.datetime(2026, 1, 2, 11, 0)   (TimezoneOffsetFromUTC '+0100' applied; UTC when absent)
```

A malformed value raises `ValueError`; an offset applied at the limits of the datetime range raises
`OverflowError`.

### Coordinate System and Units of M3D Models
Encapsulated STL instances (M3D modality) carry a Frame of Reference UID (the origin and axes implicit
in the model data) and a Measurement Units Code Sequence (units of distance of the model coordinates,
CID 7063). `sonador.imaging.helpers.m3d` resolves both for a model instance: the model headers first,
with a fallback to the frame of reference of the source series the model references
(`ReferencedSeriesSequence`), in which case the model lies in the DICOM patient coordinate system
(LPS, millimetres); millimetres are assumed when nothing is declared.

```python
from sonador.imaging.helpers.m3d import dcm_m3d_frame

frame = dcm_m3d_frame(instance)                    # any instance of an M3D series
frame = dcm_m3d_frame(instance, headers=instance.dcmfile())   # headers from a retrieved file, no tags request
frame.frame_of_reference_uid, frame.units, frame.scale_to('m'), frame.frame_source, frame.describe()
```

`DcmM3DInstance.frame` / `resolve_frame()` and `DcmM3DSeries.frame` expose the same resolution on
the M3D model classes. The STL encoders (`sonador.tasks.media.dcm_encode_stl`,
`sonador.tasks.m3d.dcm_encode_m3d_models`) record the units of the encoded coordinates
(`stl_units`, millimetres by default) and copy the frame of reference of the source series.
