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

# Re-save: same template, predecessor linked, always written as Comprehensive 3D SR with the nested
# layout; an Enhanced SR parent is upgraded with an `info` diagnostic (`document-class-upgraded`)
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
Length, Angle, Cobb angle, Circle and Bidirectional measurements are read from and written to DICOM-SR documents
(`sonador.apisettings.sr.measurements`) with the coordinates they were made from: one measurement group
each, in the patient domain (millimetres, in the imaging series' Frame of Reference) or the image domain.

```python
from sonador.apisettings.base import LengthMeasurement, CobbAngleMeasurement, ImageReference
from sonador.apisettings.sr.measurements import image_frame_geometry

# A new measurement computes its value when it is written. A patient-domain measurement can record the
# imaging series it was made on (`series_uid`) and the orientation of the view it was drawn in
# (`drawing_plane`: width direction, then height direction, in patient coordinates)
length = LengthMeasurement([(0, 0, 0), (0, 3, -4)], frame_of_reference_uid=frame_uid, description='...',
	views=['sagittal'], series_uid=series.series_uid, drawing_plane=((0, 1, 0), (0, 0, -1)))
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

Circles are built from a center and radius, a center and a circumference point, or three points on the
circumference. In the patient domain the plane comes from `normal`, else from the drawing plane; the
three-point form keeps its points and writes them with the circle.

```python
from sonador.apisettings.base import CircleMeasurement

circle = CircleMeasurement.from_center_radius(center, 5.0, normal=(0, 0, 1), frame_of_reference_uid=frame_uid)
circle = CircleMeasurement.from_center_point(center, point, drawing_plane=((0, 1, 0), (0, 0, -1)), frame_of_reference_uid=frame_uid)
circle = CircleMeasurement.from_points(p1, p2, p3, frame_of_reference_uid=frame_uid)
circle.radius, circle.area, circle.perimeter, circle.normal   # mm, mm2, mm, unit normal (None in the image domain)
```

A circle is written as Area, Perimeter and Radius NUM items (`CIRCLE` geometry on an image, `ELLIPSE` in
patient coordinates); its stored values are kept while the geometry is unchanged. A stock Cornerstone3D
patient-domain circle, which has no plane, is read with a warning and written again only once `normal`
is set.

A bidirectional measurement (the Cornerstone `Bidirectional` tool, shown as a Target in the Viewer) is a long
axis and a short axis, two line segments of two points each, built from the axes as drawn:

```python
from sonador.apisettings.base import BidirectionalMeasurement

target = BidirectionalMeasurement.from_axes(((-10, 20, 30), (10, 20, 30)), ((0, 15, 30), (0, 25, 30)),
	frame_of_reference_uid=frame_uid, series_uid=series.series_uid, drawing_plane=((1, 0, 0), (0, 1, 0)))
target.length, target.width               # 20.0, 10.0 (mm); `value` and `unit` are the long axis
target.short_axis = ((0, 10, 30), (0, 30, 30))   # recomputes the width only; the stored length is kept

image_target = BidirectionalMeasurement.from_axes(long_axis, short_axis, reference=image_reference,
	calibration=image_frame_geometry(header))   # (column, row) points; mm with Pixel Spacing, pixels without
```

| Model | DICOM-SR |
| --- | --- |
| `long_axis`, `length` (`value`, `unit`) | NUM `(103339001, SCT, "Long Axis")`, `INFERRED FROM` a two-point `POLYLINE` |
| `short_axis`, `width` (`width_unit`, also in `values`) | NUM `(103340004, SCT, "Short Axis")`, `INFERRED FROM` its own two-point `POLYLINE` |
| description, text, location, views, `series_uid`, `drawing_plane`, `display`, `extra_items` | children of the Long Axis NUM |
| `short_axis_extra_items` | the Short Axis NUM's own unmodelled children |
| `tracking_id`, `tracking_uid` | `cornerstoneTools@^4.0.0:Bidirectional` and the group's UID (`Cornerstone3DTools@^0.1.0:Bidirectional` is read) |

Each `POLYLINE` is a SCOORD `SELECTED FROM` the image in the image domain, or a SCOORD3D with the Frame of
Reference in the patient domain. A length computed by this client is in `mm`, or in the pixel unit on a frame
without Pixel Spacing (with the `info` `measurement-uncalibrated`); a stored length keeps the unit it was read
in. The SRT codes `G-A185` and `G-A186` are read too, as is a NUM matched by its meaning only, in either order;
a measurement read with SRT codes keeps them. The typed properties are read from the first of the two NUM
items in document order that carries any and written under the Long Axis NUM; every other child stays under
the NUM it was read from.

The axes are kept as drawn: the client never swaps them, and applies no perpendicularity or intersection
constraint. A short axis longer than the long axis is read and written as labelled, with the `info`
`axes-not-ordered`. Each stored length is authoritative; one stored in the unit the client computes is checked
against its axis within 0.01 mm (0.01 px uncalibrated), one in another unit is reported, not converted. An
unchanged axis re-saves its stored value verbatim, an edited one is recomputed, and assigning `value` replaces
the stored long axis length. Lengths stored in different units are read as stored, with the `warning`
`axes-units-differ`, and written back as read; editing one axis can also leave the two in different units.
Without a calibration, an edited image-domain axis is recomputed only when its own stored length is in
pixels; otherwise the write is refused (`measurement-not-computable`). A drawing plane must hold all four points within 1 mm; two axes in no common plane are a legal document
without one. A frame conversion must be rigid and moves both axes and the plane. An axis shorter than 0.01 mm
is a `warning` on read and refused on write. A group with one axis only, a NUM item without its own
geometry, or axes on different images or Frames of Reference reads as a generic `Measurement`, with the
`warning` `measurement-read-as-generic`.

Display colours have six roles: `Background`, `Border`, `Value` and `Meaning` for Series Tags, `Line` and
`Text` for a measurement's drawing and label. Any role may be set on any finding or measurement; a role
that is not set is not written.

View values are registered in `sonador.apisettings.sr.measurements` (`SONADOR_MEASUREMENT_VIEWS`: `axial`,
`sagittal`, `coronal`, `oblique`, `3d`, each with a meaning and a code in `DCMSR_MEASUREMENT_VIEW_CODES`). A
value outside the registry is written as given with a `warning`, and refused with `strict=True`; values read
from a document are kept as they are. View and drawing plane are independent: neither is derived from the
other.

For a patient-domain measurement, `series_uid` is written as `Sonador-Measurement.Source-Series` and the
first instance of that series is recorded in the evidence. The series must be one the report can resolve
without a request (a reference series, a cached series of their study, or a series the evidence records);
any other is refused. `drawing_plane` is written as `Sonador-Measurement.Drawing-Plane` (six decimals,
`0,1,0,0,0,-1` for a sagittal view) and is checked first: unit directions, perpendicular, every point within
1 mm of the plane through the first point; a failure is refused with an error naming the check. Neither
property is written for image-domain geometry. A drawing plane read from a document is written back as
read; one that is malformed reads as `None` with a `warning` and is still written back unchanged.

An image-domain measurement is keyed by the series of its image. A patient-domain measurement is keyed by
its source series when it names one the study holds, otherwise by the series of the SR document (with a
`warning` when the source series is not in the study); from an imaging series every patient-domain
measurement is also reached by Frame of Reference. Loading a report costs one `file` request per SR series
on first access, shared with `findings`; narrowing what is already loaded adds none.

Every document is written as Comprehensive 3D SR. Lengths are written with the SCT concept
`(410668003, SCT, "Length")`, and the SRT form is read as well.

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

### Points
Two classes hold coordinates. Neither carries a unit: the API that returns a value states its role and unit.

| Class | Values | Returned for |
|---|---|---|
| `PixelCoord(column, row)` | a 2D position on an image grid | image-domain measurement points, in pixels: the SCOORD origin is the outer corner of the first pixel, so pixel centres are at `.5` |
| `ImageCoord(x, y, z)` | a 3D triple | patient-domain measurement points, in millimetres; unitless directions (`normal`, `u`, `plane_normal`, `drawing_plane`, Image Orientation); frame conversions (`convert` returns millimetres, `invert` the transform's `source_unit`) |

The measurement properties (`points`, `p1`, `p2`, `vertex`, `p3`, `line1`, `line2`, `long_axis`, `short_axis`,
`center`, `edge`, `construction_points`) return them, and every constructor and setter accepts tuples, lists,
numpy arrays or the point classes. `array` on a measurement or a `Geometry` is a new float array of shape
`(N, 2)` or `(N, 3)`.

```python
import numpy as np
from sonador.apisettings.base import PixelCoord, ImageCoord

image_length.p1                       # PixelCoord(column=10.0, row=10.0)
image_length.p1 == (10, 10)           # True: compares and hashes as the tuple of its values
pixels[image_length.p1.array_index(pixels.shape)]   # numpy (row, column) index of the pixel holding the point

length.array                          # (2, 3) float array, the same as np.asarray(length.points)
offset = np.asarray(length.p2) - length.p1          # arithmetic is done in numpy
```

* Equality is exact on the stored floats; compare within a tolerance with `numpy.allclose`. A point written as
  float32 GraphicData and read back may differ from the original: `0.1` reads back as `0.10000000149011612`,
  while a value exactly representable in float32, such as `0.5`, is unchanged.
* Comparing a point with a numpy array is element-wise, as numpy does, so `if point == array:` raises numpy's
  ambiguous-truth error.
* Neither class has arithmetic of its own. With a numpy array or a numpy scalar the result is a numpy array;
  any other operand (a point, a tuple, a Python number) raises `TypeError`, so a point is never concatenated
  or repeated like a tuple.
* Both keep iteration, unpacking, `len`, indexing, slicing, equality and hashing with tuples, and pickling.
  `PixelCoord` is a tuple (with `index` and `count`); `ImageCoord` is not, and it also carries the attributes
  of a point-collection point (`reference_frame`, `name`, `point_type`), which take no part in equality.
* `array_index` takes the floor of each value and raises `IndexError` for a negative index, which numpy would
  otherwise count from the opposite edge, or one outside the given shape.
* An `ImageCoord` returned as a patient position (the patient-domain points and `patient_point`) is in patient
  space, not a voxel index: SimpleITK indices are `(i, j, k)` and its numpy arrays are indexed `(k, j, i)`. The
  roles of other `ImageCoord` values are those of the table above: unitless directions, frame-conversion points
  in the transform's units, and the legacy SCOORD points of `sr2points` below.
* `sr2points` reads SCOORD points as `ImageCoord` with `z = 0`, as before.

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
