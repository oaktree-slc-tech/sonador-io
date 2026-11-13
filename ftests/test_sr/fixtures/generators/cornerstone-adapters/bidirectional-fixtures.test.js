/*
 * Writes the two Bidirectional fixtures of oak-tree/medical-imaging/sonador-client#43 with the stock
 * @cornerstonejs/adapters Bidirectional writer (4.22.13, the Sonador Viewer's locked version), from synthetic
 * annotation state on the synthetic study the client fixtures use. @cornerstonejs/core is stubbed as in
 * circle-fixtures.test.js: image plane at the origin, rows along +y, columns along +x, 1 mm pixels. The
 * handles are given short axis first, so that the fixtures also record the stock writer assigning the longer
 * pair to the Long Axis NUM. Synthetic data only. Output: out/, copied to ftests/test_sr/fixtures.
 */
jest.mock('@cornerstonejs/core', () => ({
  utilities: {
    worldToImageCoords: (imageId, point) => [point[0], point[1]],
    imageToWorldCoords: (imageId, point) => [point[0], point[1], 0],
  },
  cache: { getVolume: () => ({ imageIds: ['synthetic:1'] }) },
  Enums: { MetadataModules: { PREDECESSOR_SEQUENCE: 'predecessorSequence' } },
  metaData: { get: () => undefined },
}), { virtual: true });

const fs = require('fs');
const dcmjs = require('dcmjs');
const MeasurementReport = require('./node_modules/@cornerstonejs/adapters/dist/esm/adapters/Cornerstone3D/MeasurementReport.js').default;
require('./node_modules/@cornerstonejs/adapters/dist/esm/adapters/Cornerstone3D/Bidirectional.js');
const { NO_IMAGE_ID } = require('./node_modules/@cornerstonejs/adapters/dist/esm/adapters/Cornerstone3D/constants/index.js');

const INSTANCE = {
  PatientName: 'Anonymous', PatientID: 'ANONYMOUS', PatientBirthDate: '', PatientSex: '', StudyID: '1',
  StudyInstanceUID: '2.25.1001', StudyDate: '20260101', StudyTime: '090000', AccessionNumber: '', StudyDescription: 'Synthetic study',
  SeriesInstanceUID: '2.25.1002', SeriesNumber: 1, Modality: 'MR', SeriesDescription: 'Synthetic series',
  SOPInstanceUID: '2.25.1003', SOPClassUID: '1.2.840.10008.5.1.4.1.1.4', FrameOfReferenceUID: '2.25.7001',
};
const metadataProvider = {
  get: (module, imageId) => {
    if (module === 'sopCommonModule') return { sopInstanceUID: INSTANCE.SOPInstanceUID, sopClassUID: INSTANCE.SOPClassUID };
    if (module === 'instance') return INSTANCE;
    if (module === 'frameNumber') return 1;
    return undefined;
  },
};

function write(toolState, path) {
  const report = MeasurementReport.generateReport(toolState, metadataProvider, { SeriesInstanceUID: '2.25.8100', SOPInstanceUID: '2.25.8111' });
  report.dataset.SoftwareVersions = '@cornerstonejs/adapters 4.22.13';
  const dict = dcmjs.data.datasetToDict(report.dataset);
  fs.mkdirSync('out', { recursive: true });
  fs.writeFileSync(path, Buffer.from(dict.write()));
  return report.dataset;
}

test('stock Bidirectional writer, image domain', () => {
  const imageId = 'synthetic:1';
  const tool = {
    annotationUID: '2.25.8211',
    data: { handles: { points: [[20, 15, 0], [20, 25, 0], [10, 20, 0], [30, 20, 0]] },
      cachedStats: { ['imageId:' + imageId]: { length: 20, width: 10, unit: 'mm', widthUnit: 'mm' } } },
    metadata: { referencedImageId: imageId, FrameOfReferenceUID: INSTANCE.FrameOfReferenceUID, toolName: 'Bidirectional' },
  };
  const ds = write({ [imageId]: { Bidirectional: { data: [tool] } } }, 'out/cornerstone-adapters.bidirectional-image.2026-1113.dcm');
  expect(ds.SOPClassUID).toBeDefined();
});

test('stock Bidirectional writer, patient domain', () => {
  const tool = {
    annotationUID: '2.25.8212',
    data: { handles: { points: [[10, 15, 30], [10, 25, 30], [0, 20, 30], [20, 20, 30]] }, cachedStats: {} },
    metadata: { FrameOfReferenceUID: INSTANCE.FrameOfReferenceUID, toolName: 'Bidirectional', volumeId: 'synthetic-volume' },
  };
  const ds = write({ [NO_IMAGE_ID]: { Bidirectional: { data: [tool] } } }, 'out/cornerstone-adapters.bidirectional-patient.2026-1113.dcm');
  expect(ds.SOPClassUID).toBe('1.2.840.10008.5.1.4.1.1.88.34');
});
