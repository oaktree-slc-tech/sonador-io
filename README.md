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

### Series Tags and DICOM-SR Findings
Series Tags are qualitative findings that apply to a whole imaging series. The Sonador Viewer and
Sonador IO exchange them as TID 1500 DICOM-SR documents under the Sonador-SR 0.3 profile
(`sonador.apisettings.sr.seriestag`): one measurement group per tag, the tag code carried verbatim, and
the per-finding properties (description, text, location, display colours) as children of the Finding.
Readers accept Enhanced SR, Comprehensive SR and Comprehensive 3D SR with root template `1500` or
`Sonador-SR.1500`, including files written by earlier Viewer releases, and record every repair as a
diagnostic (`info` / `warning` / `error`; `strict=True` raises on `warning` and `error`).

```python
from sonador.imaging.helpers.sr import SonadorComprehensiveSR3D, SonadorImagingMeasurementReport
from sonador.apisettings.base import Finding

# Read: one document -> report model; one or more documents -> DcmSRSeries with parsed instances
report = SonadorComprehensiveSR3D.from_sr('report.dcm')
report.series_tag_findings                       # Finding: value, meaning, tracking_uid, reference,
report.diagnostics                               #   description, text, location, display
for report_group in report.groups:               # every group; a group read without a Finding code
	for finding in report_group.findings or ():  #   (kept as a generic group) has findings=None
		...

# Write: a new Imaging Measurement Report for the Viewer (Comprehensive 3D SR, Sonador-SR.1500)
tag = study.pacs.get_tag(group, tag_id)          # or any Code / CodedConcept
model = SonadorImagingMeasurementReport(sonador_ref_series=series, findings=[Finding.series_tag(tag, description='...')],
	report_procedure=procedure, person_observer='Family^Given', record_instances=False)
document = model.create_sr()                     # evidence synthesised from the cached JSON

# Re-save: same template, predecessor linked; Enhanced SR parent -> Enhanced SR (flat layout; upgraded
# to Comprehensive 3D SR with a warning when the content needs it), Comprehensive SR parent ->
# Comprehensive 3D SR, Comprehensive 3D SR parent -> Comprehensive 3D SR
report.create_sr()
```

With `record_instances=False` the evidence is built from the cached patient, study, series and
instance JSON. That makes no request when the instance JSON carries the SOP class and SOP instance
UIDs, which is the case for a study populated with `bulkpopulate_related(child_instances=True)` on
the Sonador Orthanc build; an instance whose cached tags lack them is fetched once. With
`record_instances=True` every instance of the reference series is downloaded and recorded.

Findings are retrieved from an imaging server through the study. After
`study_collection.bulkpopulate_related(child_instances=True)` the first access parses the current
document of every SR series of the study (one `file` request per SR series, none afterwards;
`parse_sr=True` does the same inside the bulk call, `study.parse_sr(all_revisions=True)` also loads
the superseded revisions). The three entry points differ in scope and shape:

```python
study.findings()                                 # {imaging_series_uid: {measurement_type: FindingCollection}}
study.findings(series_uid)                       # {measurement_type: FindingCollection} for one imaging series
study.findings(series_uid, 'series-tag')         # FindingCollection (empty when there is none)
series.findings()                                # {measurement_type: FindingCollection} for this imaging series
series.findings('series-tag')                    # FindingCollection, served through the parent study's cache
sr_series.findings()                             # the study-shaped mapping, from this SR series' current document only
```

Each `Finding` links back to its `ImagingSeries`, `DcmInstance` and `DcmSRInstance`.

### Quantitative Measurements
Length, Angle and Cobb angle measurements are read from and written to DICOM-SR documents
(`sonador.apisettings.sr.measurements`) with the coordinates they were made from: one measurement group
each, in the patient domain (millimetres, in the imaging series' Frame of Reference) or the image domain.

```python
from sonador.apisettings.base import LengthMeasurement, CobbAngleMeasurement, ImageReference
from sonador.apisettings.sr.measurements import image_frame_geometry

# A new measurement computes its value when it is written
length = LengthMeasurement([(0, 0, 0), (3, 4, 0)], frame_of_reference_uid=frame_uid, description='...', views=['coronal'])
cobb = CobbAngleMeasurement(line1, line2, frame_of_reference_uid=frame_uid)

# Image domain: (column, row) points, with the calibration of the referenced frame
image_length = LengthMeasurement([(10, 10), (14, 13)], reference=ImageReference(sop_class, sop_instance, None, series_uid),
	calibration=image_frame_geometry(header))

model = SonadorImagingMeasurementReport(sonador_ref_series=series, measurements=[length, cobb, image_length], report_procedure=code)
doc = model.create_sr()

study.measurements()                                  # {series_uid: {measurement_type: MeasurementCollection}}
series.measurements('length')                         # measurements on this imaging series' images
study.measurements(frame_of_reference_uid=frame_uid)  # the same mapping, narrowed to one Frame of Reference
series.measurements('length', frame_of_reference_uid=frame_uid)   # also finds patient-domain measurements
```

An image-domain measurement is keyed by the series of its image. A patient-domain measurement has no
image reference and is keyed by the series of the SR document, so from an imaging series it is reached by
Frame of Reference. Loading a report costs one `file` request per SR series on first access, shared with
`findings`; narrowing what is already loaded adds none.

The stored value of a document is authoritative. A re-save keeps the stored value of a measurement whose
points and referenced image are unchanged; the properties are written in the current layout, so legacy
group-level properties and foreign labels are normalised. A measurement whose points or referenced image
changed recomputes at write. Coincident points and values that are not finite are refused at write.

A new or edited image-domain length in millimetres needs `calibration`, and is refused without one. Given
the `ImageFrameGeometry` of a frame without Pixel Spacing, a length is written in pixels, never millimetres;
a parsed length stored in pixels and then edited recomputes in pixels without a calibration. An unusable
calibration (for example zero spacing) is refused. A parsed length stored in millimetres and left unchanged
keeps that value even when no calibration is available. Points given in another frame are
converted at construction with `transform`, a rigid `ImageTransformMatrix`.

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
