# [Sonador IO](https://sonador.oak-tree.tech/io) Python Library
[Sonador IO](https://sonador.oak-tree.tech/io) is the data management interface of [Sonador](https://sonador.oak-tree.tech) used to securely store medical data so that it can be queried, retrieved, and transferred. It provides a streamlined way to share medical data with across the Internet or other applications within the hospital.

The [Sonador IO Python Library](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-client) provides tools for working with meta and payload (pixel) data stored in [Sonador/Orthanc](https://code.oak-tree.tech/oak-tree/medical-imaging/orthanc-sonador). It builds on top of the [Guru Client Library](https://code.oak-tree.tech/guru-labs/guru-client) and provides data models for medical imaging resources (patient, study, series), Sonador's "DICOM extension models" (which provide specialized features such as interfaces for worklists), resource comments, interfaces for working with structured data (SR documents), and interfaces for managing 3D data. _The 3D capabilities of the Sonador IO client are focused on data modelling and providing a foundation for more advanced capabilities. For tools and libraries to work with imaging volume and spatial data, refer to [Sonador 3D](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador3d)._

Dependencies:

* [Guru Client Library](https://code.oak-tree.tech/guru-labs/guru-client): provides [client models](https://code.oak-tree.tech/django-apps/guru/-/wikis/dev.client-tools) for interacting with Sonador/Orthanc APIs using [REST-like patterns](https://code.oak-tree.tech/django-apps/guru/-/wikis/dev.data-models).
* [`pydicom`](https://pydicom.github.io/): tools for parsing DICOM (Digital Imagingin Medicine) data and interfacing with pixel/payload data via [`numpy`](https://numpy.org/).
* [`highdicom`](https://github.com/ImagingDataCommons/highdicom): high-level DICOM abstractions for the Python programming language. Provides tools for parsing segmentation (`SEG`) and structured reporting (`SR`) documents so that they can be consumed from `numpy`. **The client requires the [Oak-Tree fork of highdicom](https://code.oak-tree.tech/oak-tree/medical-imaging/highdicom)** (published to the Sonador package repository as `highdicom==<upstream version>.postN`), which adds the `record_instances` option used when writing DICOM-SR documents. Importing the SR helpers with a stock highdicom raises an `ImportError` explaining how to install the fork (see `dcmcompat.py`).



## Installation
Pre-built binaries of the client library are available from the [Sonador Python package](https://code.oak-tree.tech/oak-tree/medical-imaging/imaging-development-env/-/wikis/deployment.package-repository) repostiory and can be installed via `pip`:

```bash
pip install sonador --extra-index-url https://code.oak-tree.tech/api/v4/projects/335/packages/pypi/simple
```

To install from source, clone this repository and install the dependencies (`pip install -r requirements.txt`). The `--extra-index-url` in `requirements.txt` points at the Sonador package repository, which provides the Oak-Tree highdicom fork and the Guru client.

Version 0.5 requires Python 3.10+ and pydicom 3 (`pydicom>=3.0.1`, `highdicom==0.28.2.post1`; the fork build is pinned exactly because a compatible-release specifier would also admit stock 0.28.x releases). The 0.4 line remains the pydicom 2 release series.



## Usage
Examples of how to utilize the features of the client can be found in the [Sonador Examples Repository](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples). _More advanced capabilities highlighting access control, 3D capabilities, and worklists are demonstrated in the [`technical-reference` subfolder](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/tree/master/technical-reference?ref_type=heads)._

* Basic usage of the client is outlined in [this notebook](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/blob/master/sonador-client.image-io.ipynb), and introduces three of the core classes in the library: `sonador.servers.SonadorServer`, `sonador.servers.SonadorImagingServer`, and imaging resouce models.
* An overview of working with structured data (via DICOM-SR documents) can be found [here](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/blob/master/sonador-client02.dicom-sr.ipynb?ref_type=heads).
* Basic AI capbilities and how to interface with AI based tools (via `numpy`) are demonstrated in [this notebook](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/blob/master/sonador-client03.ai-lung.ipynb?ref_type=heads).

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
