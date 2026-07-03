# Worklist Reviewer Values
SONADOR_WORKLIST_STATUS_APPROVED = 'Approved'
SONADOR_WORKLIST_STATUS_REJECTED = 'Rejected'
SONADOR_WORKLIST_STATUS_UNREAD = 'Unread'
SONADOR_WORKLIST_STATUS_REVIEWED = 'Reviewed'

# DICOM Procedure Step Values
SONADOR_WORKLIST_STATUS_SCHEDULED = 'Scheduled'
SONADOR_WORKLIST_STATUS_INPROGRESS = 'In-progress'
SONADOR_WORKLIST_STATUS_COMPLETED = 'Completed'
SONADOR_WORKLIST_STATUS_CANCELLED = 'Cancelled'

# Reserved, server-owned keys inside the worklist item's Meta (orthanc-sonador#54, section 6.1).
# Procedure/history data is written only through the validated `Procedure` block of worklist
# create/update requests; values submitted under these keys via the general Meta channel are
# stripped by the server.
SONADOR_WORKLIST_META_REQUESTED_PROCEDURE = 'RequestedProcedure'
SONADOR_WORKLIST_META_PERFORMED_PROCEDURE = 'PerformedProcedure'
SONADOR_WORKLIST_META_REVIEW_HISTORY = 'ReviewHistory'
SONADOR_WORKLIST_META_RESERVED_KEYS = (
	SONADOR_WORKLIST_META_REQUESTED_PROCEDURE,
	SONADOR_WORKLIST_META_PERFORMED_PROCEDURE,
	SONADOR_WORKLIST_META_REVIEW_HISTORY,
)
