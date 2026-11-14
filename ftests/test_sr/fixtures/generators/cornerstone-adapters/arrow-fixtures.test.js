/*
 * Writes the three ArrowAnnotate fixtures of oak-tree/medical-imaging/sonador-client#39 with the stock
 * @cornerstonejs/adapters writers (4.22.13, the Sonador Viewer's locked version), from synthetic annotation
 * state on the synthetic study the client fixtures use. @cornerstonejs/core is stubbed as in
 * circle-fixtures.test.js: image plane at the origin, rows along +y, columns along +x, 1 mm pixels.
 *
 * - arrow-image and arrow-patient: the Cornerstone3D ArrowAnnotate adapter (TID300 ArrowAnnotate), a NUM
 *   carrier INFERRED FROM a two-point POLYLINE, tip first, labelled by a CORNERSTONEFREETEXT Finding. The
 *   label is given as the tool's `finding` only: with `data.label` set, the stock LabelData wrapper writes the
 *   label as a group-level Comment TEXT (121106, DCM) instead and removes the Finding.
 * - arrow-point: the classic ArrowAnnotate adapter (TID300 Point), the form the Viewer writes today: a NUM
 *   carrier INFERRED FROM a one-point POINT (the tip), labelled from the tool's label.
 *
 * Synthetic data only. Output: out/, copied to ftests/test_sr/fixtures.
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
const MeasurementReport3D = require('./node_modules/@cornerstonejs/adapters/dist/esm/adapters/Cornerstone3D/MeasurementReport.js').default;
require('./node_modules/@cornerstonejs/adapters/dist/esm/adapters/Cornerstone3D/ArrowAnnotate.js');
const MeasurementReportClassic = require('./node_modules/@cornerstonejs/adapters/dist/esm/adapters/Cornerstone/MeasurementReport.js').default;
require('./node_modules/@cornerstonejs/adapters/dist/esm/adapters/Cornerstone/ArrowAnnotate.js');
const { NO_IMAGE_ID } = require('./node_modules/@cornerstonejs/adapters/dist/esm/adapters/Cornerstone3D/constants/index.js');

const DATE = '2026-1114';
const LABEL = 'Synthetic arrow';
const INSTANCE = {
  PatientName: 'Anonymous', PatientID: 'ANONYMOUS', PatientBirthDate: '', PatientSex: '', StudyID: '1',
  StudyInstanceUID: '2.25.1001', StudyDate: '20260101', StudyTime: '090000', AccessionNumber: '', StudyDescription: 'Synthetic study',
  SeriesInstanceUID: '2.25.1002', SeriesNumber: 1, Modality: 'MR', SeriesDescription: 'Synthetic series',
  SOPInstanceUID: '2.25.1003', SOPClassUID: '1.2.840.10008.5.1.4.1.1.4', FrameOfReferenceUID: '2.25.7001',
};
const metadataProvider = {
  get: (module, imageId) => {
    if (module === 'sopCommonModule') return { sopInstanceUID: INSTANCE.SOPInstanceUID, sopClassUID: INSTANCE.SOPClassUID };
    if (module === 'generalSeriesModule') return { studyInstanceUID: INSTANCE.StudyInstanceUID, seriesInstanceUID: INSTANCE.SeriesInstanceUID };
    if (module === 'instance') return INSTANCE;
    if (module === 'frameNumber') return 1;
    return undefined;
  },
};
const FREE_TEXT = { CodeValue: 'CORNERSTONEFREETEXT', CodingSchemeDesignator: 'CST4', CodeMeaning: LABEL };

function save(report, path) {
  report.dataset.SoftwareVersions = '@cornerstonejs/adapters 4.22.13';
  const dict = dcmjs.data.datasetToDict(report.dataset);
  fs.mkdirSync('out', { recursive: true });
  fs.writeFileSync(path, Buffer.from(dict.write()));
  return report.dataset;
}

function write3D(toolState, path) {
  return save(MeasurementReport3D.generateReport(toolState, metadataProvider, { SeriesInstanceUID: '2.25.8100', SOPInstanceUID: '2.25.8121' }), path);
}

test('stock Cornerstone3D ArrowAnnotate writer, image domain', () => {
  const imageId = 'synthetic:1';
  const tool = {
    annotationUID: '2.25.8221',
    finding: FREE_TEXT,
    data: { handles: { arrowFirst: true, points: [[30, 20, 0], [10, 40, 0]] } },
    metadata: { referencedImageId: imageId, FrameOfReferenceUID: INSTANCE.FrameOfReferenceUID, toolName: 'ArrowAnnotate' },
  };
  const ds = write3D({ [imageId]: { ArrowAnnotate: { data: [tool] } } }, 'out/cornerstone-adapters.arrow-image.' + DATE + '.dcm');
  expect(ds.SOPClassUID).toBeDefined();
});

test('stock Cornerstone3D ArrowAnnotate writer, patient domain', () => {
  const tool = {
    annotationUID: '2.25.8222',
    finding: FREE_TEXT,
    data: { handles: { arrowFirst: true, points: [[10, 20, 30], [25, 35, 30]] } },
    metadata: { FrameOfReferenceUID: INSTANCE.FrameOfReferenceUID, toolName: 'ArrowAnnotate', volumeId: 'synthetic-volume' },
  };
  const ds = write3D({ [NO_IMAGE_ID]: { ArrowAnnotate: { data: [tool] } } }, 'out/cornerstone-adapters.arrow-patient.' + DATE + '.dcm');
  expect(ds.SOPClassUID).toBe('1.2.840.10008.5.1.4.1.1.88.34');
});

test('stock classic ArrowAnnotate writer (TID300 Point)', () => {
  const tool = {
    toolType: 'ArrowAnnotate', measurementNumber: 1, label: LABEL,
    handles: { start: { x: 30, y: 20 }, end: { x: 10, y: 40 } },
  };
  const report = MeasurementReportClassic.generateReport({ 'synthetic:1': { ArrowAnnotate: { data: [tool] } } }, metadataProvider,
    { SeriesInstanceUID: '2.25.8100', SOPInstanceUID: '2.25.8123' });
  const ds = save(report, 'out/cornerstone-adapters.arrow-point.' + DATE + '.dcm');
  expect(ds.SOPClassUID).toBeDefined();
});
