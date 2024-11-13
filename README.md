# [Sonador IO](https://sonador.oak-tree.tech/io) Python Library
[Sonador IO](https://sonador.oak-tree.tech/io) is the data management interface of [Sonador](https://sonador.oak-tree.tech) used to securely store medical data so that it can be queried, retrieved, and transferred. It provides a streamlined way to share medical data with across the Internet or other applications within the hospital.

The [Sonador IO Python Library](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-client) provides tools for working with meta and payload (pixel) data stored in [Sonador/Orthanc](https://code.oak-tree.tech/oak-tree/medical-imaging/orthanc-sonador). It builds on top of the [Guru Client Library](https://code.oak-tree.tech/guru-labs/guru-client) and provides data models for medical imaging resources (patient, study, series), Sonador's "DICOM extension models" (which provide specialized features such as interfaces for worklists), resource comments, interfaces for working with structured data (SR documents), and interfaces for managing 3D data. _The 3D capabilities of the Sonador IO client are focused on data modelling and providing a foundation for more advanced capabilities. For tools and libraries to work with imaging volume and spatial data, refer to [Sonador 3D](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador3d)._

Dependencies:

* [Guru Client Library](https://code.oak-tree.tech/guru-labs/guru-client): provides [client models](https://code.oak-tree.tech/django-apps/guru/-/wikis/dev.client-tools) for interacting with Sonador/Orthanc APIs using [REST-like patterns](https://code.oak-tree.tech/django-apps/guru/-/wikis/dev.data-models).
* [`pydicom`](https://pydicom.github.io/): tools for parsing DICOM (Digital Imagingin Medicine) data and interfacing with pixel/payload data via [`numpy`](https://numpy.org/).
* [`highdicom`](https://github.com/ImagingDataCommons/highdicom): high-level DICOM abstractions for the Python programming language. Provides tools for parsing segmentation (`SEG`) and structured reporting (`SR`) documents so that thtey can be consumed from `numpy`.



## Installation
Pre-built binaries of the client library are available from the [Sonador Python package](https://code.oak-tree.tech/oak-tree/medical-imaging/imaging-development-env/-/wikis/deployment.package-repository) repostiory and can be installed via `pip`:

```bash
pip install sonador --extra-index-url https://code.oak-tree.tech/api/v4/projects/335/packages/pypi/simple
```

To install from source, clone this repository and install the dependencies.



## Usage
Examples of how to utilize the features of the client can be found in the [Sonador Examples Repository](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples). _More advanced capabilities highlighting access control, 3D capabilities, and worklists are demonstrated in the [`technical-reference` subfolder](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/tree/master/technical-reference?ref_type=heads)._

* Basic usage of the client is outlined in [this notebook](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/blob/master/sonador-client.image-io.ipynb), and introduces three of the core classes in the library: `sonador.servers.SonadorServer`, `sonador.servers.SonadorImagingServer`, and imaging resouce models.
* An overview of working with structured data (via DICOM-SR documents) can be found [here](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/blob/master/sonador-client02.dicom-sr.ipynb?ref_type=heads).
* Basic AI capbilities and how to interface with AI based tools (via `numpy`) are demonstrated in [this notebook](https://code.oak-tree.tech/oak-tree/medical-imaging/sonador-examples/-/blob/master/sonador-client03.ai-lung.ipynb?ref_type=heads).
