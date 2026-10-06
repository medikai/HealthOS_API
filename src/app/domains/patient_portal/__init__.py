"""Patient portal domain slices over existing clinical/billing storage.

All access here is gated by the caller holding a verified patient_record_link
for the object's organization/facility. No new clinical stores are created;
these services project and decorate existing rows.
"""
