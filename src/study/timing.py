from __future__ import annotations
from datetime import date
EXPOSURES = [('parent', 'study_type_changed', 'Study type changed', 'PARENT', 'STUDY_TYPE_CHANGED', ''), ('parent', 'allocation_changed', 'Allocation changed', 'PARENT', 'ALLOCATION_CHANGED', ''), ('parent', 'intervention_model_changed', 'Intervention model changed', 'PARENT', 'INTERVENTION_MODEL_CHANGED', ''), ('parent', 'primary_purpose_changed', 'Primary purpose changed', 'PARENT', 'PRIMARY_PURPOSE_CHANGED', ''), ('parent', 'masking_changed', 'Masking changed', 'PARENT', 'MASKING_CHANGED', ''), ('parent', 'number_of_arms_changed', 'Number of arms changed', 'PARENT', 'NUMBER_OF_ARMS_CHANGED', ''), ('parent', 'planned_enrollment_changed', 'Planned enrollment changed', 'PARENT', 'PLANNED_ENROLLMENT_CHANGED', ''), ('parent', 'primary_outcome_changed', 'Primary outcome changed', 'PARENT', 'PRIMARY_OUTCOME_CHANGED', ''), ('parent', 'arms_interventions_changed', 'Arms or interventions changed', 'PARENT', 'ARMS_INTERVENTIONS_CHANGED', ''), ('parent', 'start_date_crossed_submission', 'Study start crossed first submission', 'PARENT', 'START_DATE_CROSSED_FIRST_SUBMISSION_BOUNDARY', ''), ('arms_subcategory', 'arm_added', 'Arm added', 'SUBTYPE', 'ARMS_INTERVENTIONS_CHANGED', 'ARM_ADDED'), ('arms_subcategory', 'arm_removed', 'Arm removed', 'SUBTYPE', 'ARMS_INTERVENTIONS_CHANGED', 'ARM_REMOVED'), ('arms_subcategory', 'arm_type_changed', 'Arm type changed', 'SUBTYPE', 'ARMS_INTERVENTIONS_CHANGED', 'ARM_TYPE_CHANGED'), ('arms_subcategory', 'intervention_added', 'Intervention added', 'SUBTYPE', 'ARMS_INTERVENTIONS_CHANGED', 'INTERVENTION_ADDED'), ('arms_subcategory', 'intervention_removed', 'Intervention removed', 'SUBTYPE', 'ARMS_INTERVENTIONS_CHANGED', 'INTERVENTION_REMOVED'), ('arms_subcategory', 'intervention_arm_assignment_changed', 'Intervention-arm assignment changed', 'SUBTYPE', 'ARMS_INTERVENTIONS_CHANGED', 'INTERVENTION_ARM_ASSIGNMENT_CHANGED')]
CATEGORY_ORDER = ('NO_CHANGE', 'EARLY_ONLY', 'LATE_ONLY', 'BOTH_PERIODS', 'TIMING_INDETERMINATE')
EXPECTED = {'study_start': {'study_type_changed': (1395, 2, 3, 0, 0), 'allocation_changed': (1383, 4, 13, 0, 0), 'intervention_model_changed': (1356, 4, 39, 0, 1), 'primary_purpose_changed': (1369, 4, 25, 1, 1), 'masking_changed': (1318, 5, 75, 1, 1), 'number_of_arms_changed': (1400, 0, 0, 0, 0), 'planned_enrollment_changed': (976, 13, 397, 9, 5), 'primary_outcome_changed': (459, 10, 888, 26, 17), 'arms_interventions_changed': (813, 20, 531, 22, 14), 'start_date_crossed_submission': (1337, 0, 63, 0, 0), 'arm_added': (1019, 12, 351, 12, 6), 'arm_removed': (1033, 12, 340, 11, 4), 'arm_type_changed': (1369, 1, 27, 0, 3), 'intervention_added': (1034, 20, 333, 6, 7), 'intervention_removed': (1088, 15, 288, 5, 4), 'intervention_arm_assignment_changed': (1366, 1, 32, 0, 1)}, 'primary_completion': {'study_type_changed': (1311, 4, 1, 0, 0), 'allocation_changed': (1301, 12, 2, 1, 0), 'intervention_model_changed': (1273, 31, 12, 0, 0), 'primary_purpose_changed': (1285, 26, 5, 0, 0), 'masking_changed': (1243, 53, 19, 1, 0), 'number_of_arms_changed': (1316, 0, 0, 0, 0), 'planned_enrollment_changed': (924, 359, 20, 13, 0), 'primary_outcome_changed': (409, 181, 434, 291, 1), 'arms_interventions_changed': (760, 246, 193, 116, 1), 'start_date_crossed_submission': (1258, 31, 23, 4, 0), 'arm_added': (953, 143, 168, 51, 1), 'arm_removed': (967, 134, 163, 51, 1), 'arm_type_changed': (1287, 16, 13, 0, 0), 'intervention_added': (973, 196, 107, 39, 1), 'intervention_removed': (1024, 164, 93, 34, 1), 'intervention_arm_assignment_changed': (1283, 21, 12, 0, 0)}}

def classify(dates: list[date], lower: date, upper: date) -> tuple[str, str, int, int, int]:
    early = sum((item <= lower for item in dates))
    late = sum((item > upper for item in dates))
    ambiguous = sum((lower < item <= upper for item in dates))
    if early and late:
        four = 'BOTH_PERIODS'
    elif ambiguous:
        four = 'TIMING_INDETERMINATE'
    elif early:
        four = 'EARLY_ONLY'
    elif late:
        four = 'LATE_ONLY'
    else:
        four = 'NO_CHANGE'
    collapsed = {'NO_CHANGE': 'NO_CHANGE', 'EARLY_ONLY': 'EARLY_ONLY', 'LATE_ONLY': 'ANY_AFTER', 'BOTH_PERIODS': 'ANY_AFTER', 'TIMING_INDETERMINATE': 'TIMING_INDETERMINATE'}[four]
    return (four, collapsed, early, late, ambiguous)
