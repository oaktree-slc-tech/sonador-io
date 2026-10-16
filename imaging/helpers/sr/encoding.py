import os, functools, abc, json
from typing import Optional, Sequence, Union

from pydicom.dataset import Dataset
from pydicom.sr.codedict import codes as dcmcodes
from pydicom.uid import generate_uid

from highdicom.sr.templates import Template, Code as DcmCode, CodedConcept as DcmCodedConcept, CodeContentItem, ContentSequence, \
	TrackingIdentifier, ObserverContext, ObservationContext, DeviceObserverIdentifyingAttributes, \
	LanguageOfContentItemAndDescendants, AlgorithmIdentification, RealWorldValueMap, \
	TimePointContext, _MeasurementsAndQualitativeEvaluations as DcmMeasurementAndQualitativeEvaluationsBase, \
	MeasurementReport as SRMeasurementReport, ImageLibrary
from highdicom.sr import EnhancedSR, Scoord3DContentItem, QualitativeEvaluation as DcmQualitativeEvaluation, \
	Measurement as DcmMeasurement
from highdicom.sr.enum import RelationshipTypeValues
from highdicom.sr.value_types import ContainerContentItem, CodeContentItem, TextContentItem, NumContentItem

from ....apisettings.base import DCMSR_SONADOR_SR, DCMSR_GEOMETRIC_PURPOSE
from ....apisettings.sr import srencode_observation_context, srencode_procedure_reported, DCMSR_DEFAULT_LANGUAGE, \
	DCMSR_REPORT_GROUP, DCMSR_REPORT_METADATA, DCMSR_REPORT_VOLUME_MEAUREMENTS


class ReportMetaGroup(Template):
	'''	TID:Sonador-SR-1001

		MetaGroup: provides a general structure for metadata, coded concepts, and 
		data to be added to a report template.

		Inherits from highdicom.sr.templates.Template
	'''
	template_id = '%s.1002' % DCMSR_SONADOR_SR.value

	def __init__(self,
			tracking_identifier:TrackingIdentifier,
			meta:Sequence[Union[TextContentItem,NumContentItem,CodeContentItem]]=None,
			**kwargs):
		'''	Initialize plan data

			@input tracking_identifier (highdicom.templates.TrackingIdentifier): user assigned
				tracking ID for the plan data
		'''
		super().__init__()

		# Measurement group item (provides compatibility with TID-1500 "Measurement Report")
		group_item = ContainerContentItem(name=DCMSR_REPORT_GROUP, 
			relationship_type=RelationshipTypeValues.CONTAINS, template_id=self.template_id)
		content = ContentSequence()

		if not isinstance(tracking_identifier, TrackingIdentifier):
			raise TypeError('`tracking_identifier must be an instance of `TrackingIdentifier')
		if len(tracking_identifier) == 1:
			raise ValueError('`tracking_identifier` must include a human readable tracking identifier and '
				+ 'a tracking UID')

		content.extend(tracking_identifier)

		# Add metadata blocks to the content sequence
		for m in meta:
			if not isinstance(m, (TextContentItem, NumContentItem, CodeContentItem)):
				raise TypeError('Items in `meta` argument must have type TextContentItem, NumContentItem, or CodeContentItem')

			content.append(m)
		
		if len(content) > 0:
			group_item.ContentSequence = content
		self.append(group_item)

	# TODO: Add logic for parsing the metadata items from the content sequence


class VolumetricPointCollection(DcmMeasurementAndQualitativeEvaluationsBase):
	'''	TID:Sonador-SR-1001
		
		Volumetric Point Collection: provides a general structure to report collections of points,
		measurements associated with them, and qualitative evaluations. The point collection provides 
		a high-level container similar to TID-1411 "Volumetric ROI Measurements and Qualitative Evaluations", 
		but where the  measurements and findings are specific to the enclosed points.

		Inherits from highdicom.sr.templates._MeasurementsAndQualitativeEvaluations.
	'''
	template_id = '%s.1002' % DCMSR_SONADOR_SR.value

	def __init__(self,
			tracking_identifier:TrackingIdentifier,
			points:Sequence[Scoord3DContentItem],
			referenced_real_world_value_map: Optional[RealWorldValueMap] = None,
			time_point_context: Optional[TimePointContext] = None,
			finding_type:Optional[Union[DcmCode,DcmCodedConcept]]=None,
			method:Optional[Union[DcmCode,DcmCodedConcept]]=None,
			algorithm_id:Optional[AlgorithmIdentification]=None,
			session:Optional[str]=None,
			measurements:Optional[Sequence[DcmMeasurement]]=None,
			qualitative_evaluations:Optional[Sequence[DcmQualitativeEvaluation]]=None,
			geometric_purpose:Optional[Union[DcmCode,DcmCodedConcept]]=None,
			finding_category:Optional[Union[DcmCode,DcmCodedConcept]]=None, **kwargs):
		'''	Initialize point collection

			@input tracking_identifier (highdicom.templates.TrackingIdentifier): user assigned
				tracking ID for the point collection
			@input finding_type (highdicom.Code or highdicom.CodedConcept, default=None): type of observed finding
			@input method (highdicom.Code or highdicom.CodedConcept, default=None): how the point collection was measured
			@input algorithm_id (highdicom.sr.AlgorithmIdentification, default=None): identification of algorithm 
				which was used for acquiring the measurements
			@input session (str, default=None): description of the session
			@input measurements (iterator of highdicom.sr.Measurement, default=None): measurements associated
				with the point collection
			@input qualitative_evaultions (iterator of highdicom.sr.QualitativeEvaluations): qualitative findings
				associated with the 
			@input geometric_purpose (highdicom.Code or highdicom.CodedConcept): geometric interpretation of point
				collection. Refer to DCM-CID 219: Geometry Graphic Representation for options. Example options:
				https://dicom.nema.org/medical/dicom/current/output/chtml/part16/sect_CID_219.html
				* SCT: 75958009 : "Bounded by"
				* DCM: 111010 : Center
				* DCM: 128137 : Geometric Centerpoint
				* DCM: 111041 : Outline
				* DCM: 130490 : Centerline
				* DCM: 128139 : Seed Point
			@input finding_category (highdicom.Code or highdicom.CodedConcept): category of the observed finding
				(eg, anatomic structure or morphologically abnormal structure)
		'''
		super().__init__(tracking_identifier=tracking_identifier, referenced_real_world_value_map=referenced_real_world_value_map,
			time_point_context=time_point_context, finding_type=finding_type, method=method, algorithm_id=algorithm_id,
			session=session, measurements=measurements, qualitative_evaluations=qualitative_evaluations, finding_category=finding_category)

		# Retrieve "group" item
		group_item = self[0]
		content = group_item.ContentSequence

		# Add geometric purpose to content sequence
		if geometric_purpose is not None:
			geometric_purpose_content = CodeContentItem(
				name=DCMSR_GEOMETRIC_PURPOSE, value=geometric_purpose, relationship_type=RelationshipTypeValues.HAS_CONCEPT_MOD)
			content.append(geometric_purpose_content)

		# Add points to content sequence
		for p in points:
			if not isinstance(p, Scoord3DContentItem):
				raise TypeError('Items included in `points` must have type Scoord3DContentItem')
			content.append(p)

		# Update base class template identifier
		group_item.ContentTemplateSequence[0].TemplateIdentifier = self.template_id

	# TODO: Add logic for parsing points from the content sequence


class SonadorSRMeasurementReport(SRMeasurementReport):
	'''	Measurement report template (derived from TID 1500 "Measurement Report") which provides
		support for Sonador meta groups and volumetric point collections.

		Unlike the upstream `MeasurementReport`, a Sonador report does not have to contain imaging
		measurement groups: reports consisting only of meta groups and/or volumetric point collections
		are valid. At least one group of any kind is required.
	'''
	template_id = '%s.1500' % DCMSR_SONADOR_SR.value

	def __init__(self,
			observation_context:ObservationContext,
			procedure_reported:Union[DcmCodedConcept, DcmCode, Sequence[Union[DcmCodedConcept, DcmCode]]],
			imaging_measurements:Optional[Sequence[DcmMeasurementAndQualitativeEvaluationsBase]]=None,
			title:Optional[Union[DcmCodedConcept, DcmCode]]=None,
			language_of_content_item_and_descendants:Optional[LanguageOfContentItemAndDescendants]=None,
			referenced_images:Optional[Sequence[Dataset]]=None, *,
			meta_groups:Optional[Sequence[ReportMetaGroup]]=None,
			volume_measurements:Optional[Sequence[VolumetricPointCollection]]=None):
		'''	Initialize the report. The positional/keyword arguments mirror `highdicom.sr.MeasurementReport`;
			`meta_groups` and `volume_measurements` are Sonador extensions.

			@input imaging_measurements (sequence of highdicom measurement groups, default=None): TID 1500
				"Imaging Measurements" groups. May be empty when meta groups or volume measurements are provided.
			@input meta_groups (sequence of ReportMetaGroup, default=None): metadata groups
			@input volume_measurements (sequence of VolumetricPointCollection, default=None): point collections
		'''
		imaging_measurements = list(imaging_measurements or [])
		meta_groups = list(meta_groups or [])
		volume_measurements = list(volume_measurements or [])

		if not (imaging_measurements or meta_groups or volume_measurements):
			raise ValueError('A Sonador measurement report requires at least one imaging measurement group, '
				+ 'meta group, or volumetric point collection.')

		if imaging_measurements:
			super().__init__(observation_context=observation_context, procedure_reported=procedure_reported,
				imaging_measurements=imaging_measurements, title=title,
				language_of_content_item_and_descendants=language_of_content_item_and_descendants,
				referenced_images=referenced_images)

		else:
			# highdicom >= 0.23 rejects an empty `imaging_measurements` list. Build the TID 1500 root
			# container (title, language, observation context, procedure reported, image library) without
			# the "Imaging Measurements" container; the Sonador groups are appended below.
			Template.__init__(self, [self._report_root_item(
				observation_context=observation_context, procedure_reported=procedure_reported, title=title,
				language_of_content_item_and_descendants=language_of_content_item_and_descendants,
				referenced_images=referenced_images)], is_root=True)

		# Retrieve root item
		report_item = self[0]
		content = report_item.ContentSequence

		# Add meta groups to the content sequence
		if meta_groups:

			# Create container item
			meta_container_item = ContainerContentItem(
				name=DCMSR_REPORT_METADATA, relationship_type=RelationshipTypeValues.CONTAINS)
			meta_container_item.ContentSequence = ContentSequence()

			for m in meta_groups:
				if not isinstance(m, ReportMetaGroup):
					raise TypeError(('Invalid type "%s". Items included in `meta_groups` '
						+ ' must have type ReportMetaGroup') % type(m))

				meta_container_item.ContentSequence.extend(m)

			content.append(meta_container_item)

		# Add volume groups to the content sequence
		if volume_measurements:

			# Create container item for volume measurements
			volume_measurements_container_item = ContainerContentItem(
				name=DCMSR_REPORT_VOLUME_MEAUREMENTS, relationship_type=RelationshipTypeValues.CONTAINS)
			volume_measurements_container_item.ContentSequence = ContentSequence()

			for v in volume_measurements:
				if not isinstance(v, VolumetricPointCollection):
					raise TypeError(('Invalid type "%s". Items included in `volume_measurements` '
						+ ' must have type VolumetricPointCollection') % type(v))

				volume_measurements_container_item.ContentSequence.extend(v)

			content.append(volume_measurements_container_item)

		if hasattr(report_item, 'ContentTemplateSequence'):			
			report_item.ContentTemplateSequence[0].TemplateIdentifier = self.template_id

	@staticmethod
	def _report_root_item(observation_context, procedure_reported, title=None,
			language_of_content_item_and_descendants=None, referenced_images=None):
		'''	Create the TID 1500 root container item without imaging measurements. Mirrors the
			construction performed by `highdicom.sr.MeasurementReport.__init__`.
		'''
		if title is None:
			title = dcmcodes.cid7021.ImagingMeasurementReport
		if not isinstance(title, (DcmCodedConcept, DcmCode)):
			raise TypeError('Argument "title" must have type CodedConcept or Code.')
		if not isinstance(observation_context, ObservationContext):
			raise TypeError('Argument "observation_context" must have type ObservationContext.')

		item = ContainerContentItem(name=title, template_id='1500')
		item.ContentSequence = ContentSequence()

		if language_of_content_item_and_descendants is None:
			language_of_content_item_and_descendants = LanguageOfContentItemAndDescendants(DCMSR_DEFAULT_LANGUAGE)
		item.ContentSequence.extend(language_of_content_item_and_descendants)
		item.ContentSequence.extend(observation_context)

		if isinstance(procedure_reported, (DcmCodedConcept, DcmCode)):
			procedure_reported = [procedure_reported]
		for procedure in procedure_reported:
			item.ContentSequence.append(CodeContentItem(
				name=dcmcodes.DCM.ProcedureReported, value=procedure,
				relationship_type=RelationshipTypeValues.HAS_CONCEPT_MOD))

		if referenced_images:
			item.ContentSequence.extend(ImageLibrary(referenced_images))

		return item

	# TODO: add support for parsing meta and volume measurements from content sequence
